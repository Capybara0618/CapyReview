#!/usr/bin/env python
"""Collect a reproducible, unlabeled context benchmark from public GitHub PRs."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capyreview.pr_dataset import (  # noqa: E402
    PullRequestCandidate,
    candidate_pr_numbers,
    estimate_tokens,
    is_eligible_candidate,
    select_context_dataset,
    write_context_dataset,
)


REPOSITORIES = (
    "fastapi/fastapi",
    "pydantic/pydantic",
    "encode/httpx",
    "pallets/flask",
    "redis/redis-py",
    "psf/requests",
    "spring-projects/spring-framework",
    "spring-projects/spring-boot",
    "microsoft/TypeScript",
    "nestjs/nest",
)
ALLOWED_LICENSES = {"MIT", "Apache-2.0", "BSD-3-Clause"}
GRAPHQL = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    licenseInfo { spdxId }
    primaryLanguage { name }
    pullRequests(
      first:100, states:MERGED,
      orderBy:{field:UPDATED_AT,direction:DESC}
    ) {
      nodes {
        number url title mergedAt baseRefOid headRefOid
        additions deletions changedFiles
        author { login }
      }
    }
  }
}
"""


def is_skippable_diff_status(status_code: int) -> bool:
    return status_code in {401, 404, 406, 422}


def save_candidate_cache(path: Path, candidates: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for candidate in candidates:
            handle.write(json.dumps(asdict(candidate), ensure_ascii=False))
            handle.write("\n")


def load_candidate_cache(path: Path) -> list:
    return [
        PullRequestCandidate(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class GitHubSource:
    def __init__(self, token: str, delay_seconds: float = 0.1):
        self.delay_seconds = max(0.0, delay_seconds)
        self.client = httpx.Client(
            base_url="https://api.github.com",
            timeout=45.0,
            headers={
                "Authorization": "Bearer %s" % token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "CapyReview-context-dataset",
            },
        )

    def close(self):
        self.client.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(4):
            response = self.client.request(method, url, **kwargs)
            if response.status_code not in {403, 429, 500, 502, 503, 504}:
                response.raise_for_status()
                if self.delay_seconds:
                    time.sleep(self.delay_seconds)
                return response
            if attempt == 3:
                response.raise_for_status()
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after else 2 ** attempt
            time.sleep(max(1.0, min(delay, 30.0)))
        raise RuntimeError("unreachable GitHub retry state")

    def repository_candidates(self, repository: str) -> list:
        owner, name = repository.split("/", 1)
        response = self._request(
            "POST", "/graphql",
            json={"query": GRAPHQL, "variables": {"owner": owner, "name": name}},
        )
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("GitHub GraphQL error: %s" % payload["errors"])
        info = ((payload.get("data") or {}).get("repository") or {})
        license_name = str((info.get("licenseInfo") or {}).get("spdxId") or "")
        if license_name not in ALLOWED_LICENSES:
            raise ValueError(
                "%s uses unsupported or unknown license %r"
                % (repository, license_name)
            )
        language = str((info.get("primaryLanguage") or {}).get("name") or "")
        nodes = list((info.get("pullRequests") or {}).get("nodes") or [])
        by_number = {int(item["number"]): item for item in nodes}
        numbers = candidate_pr_numbers(nodes)
        candidates = []
        for number in numbers:
            node = by_number[number]
            try:
                diff_response = self._request(
                    "GET", "/repos/%s/pulls/%d" % (repository, number),
                    headers={"Accept": "application/vnd.github.diff"},
                )
            except httpx.HTTPStatusError as exc:
                if is_skippable_diff_status(exc.response.status_code):
                    continue
                raise
            diff = diff_response.text
            if not diff or len(diff.encode("utf-8")) > 1024 * 1024:
                continue
            candidate = PullRequestCandidate(
                repository=repository,
                pull_number=number,
                url=str(node.get("url") or ""),
                base_sha=str(node.get("baseRefOid") or ""),
                head_sha=str(node.get("headRefOid") or ""),
                merged_at=str(node.get("mergedAt") or ""),
                language=language,
                license=license_name,
                title=str(node.get("title") or ""),
                author=str((node.get("author") or {}).get("login") or ""),
                diff=diff,
                estimated_diff_tokens=estimate_tokens(diff),
            )
            if is_eligible_candidate(candidate):
                candidates.append(candidate)
        return candidates


def write_readme(output: Path, selected: list) -> None:
    natural = sum(item.subset == "natural" for item in selected)
    stress = sum(item.subset == "stress" for item in selected)
    text = """# GitHub PR Context Dataset

This is an unlabeled context-engineering dataset collected from public, merged
pull requests in repositories with MIT, Apache-2.0, or BSD-3-Clause licenses.
It is not ground truth for review precision, recall, or F1.

- Natural samples: {natural}
- Size-stratified stress samples: {stress}
- Stress tiers: 8K, 16K, and 32K estimated Diff tokens
- Token estimate: UTF-8 bytes divided by four

`manifest.jsonl` pins repository, pull request, base/head commits, source URL,
language, license, subset, and the corresponding file under `diffs/`.
""".format(natural=natural, stress=stress)
    (output / "README.md").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(ROOT / "evaluation_data" / "github_pr_context"),
    )
    parser.add_argument("--delay-seconds", type=float, default=0.1)
    parser.add_argument("--refresh-cache", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        parser.error("GITHUB_TOKEN is required; do not pass tokens on the command line")

    source = GitHubSource(token, args.delay_seconds)
    candidates = []
    cache_directory = ROOT / "tmp" / "github-pr-context-candidates"
    try:
        for repository in REPOSITORIES:
            cache_path = cache_directory / (
                repository.replace("/", "--") + ".jsonl"
            )
            if cache_path.exists() and not args.refresh_cache:
                items = load_candidate_cache(cache_path)
            else:
                items = source.repository_candidates(repository)
                save_candidate_cache(cache_path, items)
            candidates.extend(items)
            print("%-42s %3d eligible candidates" % (repository, len(items)))
    finally:
        source.close()

    selected = select_context_dataset(candidates)
    output = Path(args.output)
    manifest = write_context_dataset(selected, output)
    write_readme(output, selected)
    print("wrote %d PRs to %s" % (len(selected), manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
