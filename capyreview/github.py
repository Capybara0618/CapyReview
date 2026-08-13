import hashlib
import hmac
import json
import base64
import re
import urllib.error
import urllib.request
import urllib.parse
import random
import time
from typing import Dict


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubClient:
    def __init__(self, token: str, timeout: int = 30, max_attempts: int = 4):
        self.token = token
        self.timeout = timeout
        self.max_attempts = max_attempts

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        headers = {"Accept": accept, "User-Agent": "CapyReview/0.1", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def fetch_diff(self, url: str) -> str:
        body = self._request(
            "GET", url, accept="application/vnd.github.v3.diff", raw=True
        )
        return body.decode("utf-8", errors="replace")

    def upsert_comment(self, api_url: str, markdown: str, marker: str) -> None:
        """Update this service's existing review comment instead of creating duplicates."""
        comments_url = api_url.rstrip("/") + "/comments"
        comments = self._json("GET", comments_url + "?per_page=100")
        body = marker + "\n" + markdown
        for comment in comments:
            if marker in str(comment.get("body", "")):
                self._json("PATCH", comment["url"], {"body": body})
                return
        self._json("POST", comments_url, {"body": body})

    def read_file_context(
        self, repository: str, path: str, ref: str,
        line: int, radius: int = 20,
    ) -> dict:
        """Read a small source window from one explicit Git commit or ref."""
        repository = str(repository).strip()
        path = str(path).strip()
        ref = str(ref).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be owner/name")
        components = path.split("/")
        if (
            not path or path.startswith("/") or "\\" in path
            or any(item in {"", ".", ".."} for item in components)
        ):
            raise ValueError("path must be a repository-relative file path")
        if not ref:
            raise ValueError("read_file_context requires an explicit commit or ref")
        line = int(line)
        radius = int(radius)
        if line < 1:
            raise ValueError("line must be at least 1")
        if radius < 0 or radius > 50:
            raise ValueError("radius must be between 0 and 50")
        url = "https://api.github.com/repos/%s/contents/%s?%s" % (
            repository,
            urllib.parse.quote(path, safe="/"),
            urllib.parse.urlencode({"ref": ref}),
        )
        value = self._json("GET", url)
        if value.get("type") != "file" or value.get("encoding") != "base64":
            raise ValueError("GitHub path is not a base64 encoded file")
        try:
            raw = base64.b64decode(str(value.get("content", "")), validate=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("GitHub returned invalid file content") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("repository file exceeds the 2 MiB context limit")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, line - radius)
        end = min(len(lines), line + radius)
        content = "\n".join(lines[start - 1:end])[:12000]
        return {
            "path": path, "ref": ref,
            "start_line": start, "end_line": end,
            "content": content,
        }

    def _json(self, method: str, url: str, payload=None):
        return self._request(method, url, payload)

    def _request(
        self, method: str, url: str, payload=None,
        accept: str = "application/vnd.github+json", raw: bool = False,
    ):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                url, data=data,
                headers=dict(self._headers(accept), **{"Content-Type": "application/json"}),
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    if raw:
                        return body
                    return json.loads(body.decode("utf-8")) if body else {}
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {429, 500, 502, 503, 504}
                if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
                    retryable = True
                if not retryable or attempt >= self.max_attempts:
                    detail = exc.read(1000).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        "GitHub API %s %s returned HTTP %d: %s"
                        % (method, url, exc.code, detail)
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                reset = exc.headers.get("X-RateLimit-Reset")
                if retry_after:
                    delay = float(retry_after)
                elif reset:
                    delay = max(0.0, float(reset) - time.time())
                else:
                    delay = min(2 ** (attempt - 1) + random.random(), 10)
                time.sleep(min(delay, 30))
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= self.max_attempts:
                    raise RuntimeError("GitHub API request failed: %s" % exc) from exc
                time.sleep(min(2 ** (attempt - 1) + random.random(), 10))

    def get_repository(self, repository: str) -> dict:
        return self._json("GET", "https://api.github.com/repos/%s" % repository)

    def ensure_repository_access(self, repository: str) -> None:
        result = self.get_repository(repository)
        if str(result.get("full_name", "")).lower() != repository.lower():
            raise PermissionError("GitHub installation is not authorized for this repository")
