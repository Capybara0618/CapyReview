"""Prepare the pinned 50-PR dataset shared by context and quality evaluation."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capyreview.diff_parser import parse_unified_diff  # noqa: E402
from capyreview.real_pr_quality import CORE_CATEGORIES, select_quality_cases  # noqa: E402


UPSTREAM_COMMIT = "fbc5425c5eec52932aa1303708873d341968fa1c"
UPSTREAM_REPOSITORY = "https://github.com/withmartian/code-review-benchmark"
GOLDEN_FILES = (
    "cal_dot_com.json", "discourse.json", "grafana.json",
    "keycloak.json", "sentry.json",
)
LANGUAGES = {
    "cal_dot_com": "TypeScript",
    "discourse": "Ruby",
    "grafana": "Go",
    "keycloak": "Java",
    "sentry": "Python",
}
PR_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")


def _download(url: str) -> bytes:
    request = Request(url, headers={
        "User-Agent": "CapyReview-unified-real-pr-dataset",
    })
    with urlopen(request, timeout=60) as response:
        return response.read()


def _github_api(owner: str, repository: str, number: str, diff: bool = False) -> bytes:
    endpoint = "repos/%s/%s/pulls/%s" % (owner, repository, number)
    token = os.getenv("GITHUB_TOKEN", "").strip()
    accept = "application/vnd.github.v3.diff" if diff else "application/vnd.github+json"
    if token:
        request = Request(
            "https://api.github.com/%s" % endpoint,
            headers={
                "Accept": accept,
                "Authorization": "Bearer %s" % token,
                "User-Agent": "CapyReview-unified-real-pr-dataset",
            },
        )
        with urlopen(request, timeout=60) as response:
            return response.read()
    try:
        completed = subprocess.run(
            ["gh", "api", endpoint, "-H", "Accept: %s" % accept],
            check=True, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "GitHub access requires GITHUB_TOKEN or an authenticated gh CLI"
        ) from exc
    return completed.stdout


def _load_golden_records(golden_dir: str = "") -> dict:
    records = {}
    for filename in GOLDEN_FILES:
        source_repository = filename[:-5]
        if golden_dir:
            raw = (Path(golden_dir) / filename).read_bytes()
        else:
            raw = _download(
                "https://raw.githubusercontent.com/withmartian/"
                "code-review-benchmark/%s/offline/golden_comments/%s"
                % (UPSTREAM_COMMIT, filename)
            )
        values = json.loads(raw.decode("utf-8"))
        if not isinstance(values, list):
            raise ValueError("upstream golden comment file must contain an array")
        records[source_repository] = values
    return records


def prepare_dataset(output_dir: str, golden_dir: str = "") -> Path:
    root = Path(output_dir).resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        raise FileExistsError("unified dataset already exists: %s" % root)
    diffs = root / "diffs"
    diffs.mkdir(parents=True, exist_ok=True)

    selected = select_quality_cases(_load_golden_records(golden_dir))
    cases = []
    for index, selected_case in enumerate(selected, 1):
        match = PR_URL.fullmatch(selected_case["url"])
        if not match:
            raise ValueError("unsupported GitHub pull request URL")
        owner, repository, number = match.groups()
        metadata = json.loads(_github_api(owner, repository, number).decode("utf-8"))
        filename = "%s--%s--%s.diff" % (owner, repository, number)
        relative = "diffs/%s" % filename
        diff_path = root / relative
        if diff_path.exists():
            diff = diff_path.read_text(encoding="utf-8")
        else:
            diff = _github_api(owner, repository, number, diff=True).decode(
                "utf-8", errors="replace"
            )
        parsed = parse_unified_diff(diff)
        if not parsed.files or not parsed.added_lines:
            raise ValueError("downloaded PR does not contain a scoreable diff")
        diff_path.write_text(diff, encoding="utf-8", newline="\n")
        cases.append({
            "id": "%s--%s--%s" % (owner, repository, number),
            "source_repository": selected_case["source_repository"],
            "repository": "%s/%s" % (owner, repository),
            "pull_request": int(number),
            "split": selected_case["split"],
            "title": selected_case["pr_title"],
            "url": selected_case["url"],
            "language": LANGUAGES[selected_case["source_repository"]],
            "base_commit": str((metadata.get("base") or {}).get("sha", "")),
            "head_commit": str((metadata.get("head") or {}).get("sha", "")),
            "diff_file": relative,
            "diff_bytes": len(diff.encode("utf-8")),
            "golden_comments": selected_case["golden_comments"],
            "negative_control": bool(selected_case["negative_control"]),
        })
        print("[%d/%d] %s" % (index, len(selected), selected_case["url"]), flush=True)

    all_comments = [comment for case in cases for comment in case["golden_comments"]]
    scored_comments = [
        comment for comment in all_comments
        if comment["category"] in CORE_CATEGORIES
    ]
    manifest = {
        "schema_version": 2,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": {
            "name": "Code Review Bench",
            "url": UPSTREAM_REPOSITORY,
            "commit": UPSTREAM_COMMIT,
            "license": "MIT",
            "methodology": "human-verified golden comments",
        },
        "selection": {
            "development_cases": sum(case["split"] == "development" for case in cases),
            "test_cases": sum(case["split"] == "test" for case in cases),
            "development_per_repository": 2,
            "test_per_repository": 8,
            "score_categories": sorted(CORE_CATEGORIES),
            "policy": "all ten official PRs per source repository",
        },
        "statistics": {
            "cases": len(cases),
            "repositories": len({case["source_repository"] for case in cases}),
            "human_golden_issues": len(all_comments),
            "scored_core_issues": len(scored_comments),
            "negative_controls": sum(case["negative_control"] for case in cases),
        },
        "cases": cases,
    }
    expected = {
        "cases": 50, "repositories": 5, "human_golden_issues": 173,
        "scored_core_issues": 139, "negative_controls": 3,
    }
    if manifest["statistics"] != expected:
        raise ValueError("unified dataset statistics do not match the pinned contract")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "evaluation_data" / "real_pr_evaluation"),
    )
    parser.add_argument(
        "--golden-dir", default="",
        help="Optional local Code Review Bench offline/golden_comments directory.",
    )
    args = parser.parse_args()
    print("manifest:", prepare_dataset(args.output_dir, args.golden_dir))


if __name__ == "__main__":
    main()
