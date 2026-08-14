"""Selection helpers for the unlabeled real-PR context benchmark."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Iterable, List


_SOURCE_SUFFIXES = (
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java",
    ".js", ".jsx", ".kt", ".kts", ".php", ".py", ".rb", ".rs",
    ".scala", ".sql", ".ts", ".tsx",
)
_IGNORED_PARTS = (
    "/build/", "/dist/", "/generated/", "/node_modules/", "/vendor/",
)
_IGNORED_NAMES = (
    "cargo.lock", "composer.lock", "package-lock.json", "pnpm-lock.yaml",
    "poetry.lock", "uv.lock", "yarn.lock",
)
_DEPENDENCY_TITLE = re.compile(
    r"\b(bump|dependabot|dependency|dependencies|renovate)\b", re.I
)


@dataclass
class PullRequestCandidate:
    repository: str
    pull_number: int
    url: str
    base_sha: str
    head_sha: str
    merged_at: str
    language: str
    license: str
    title: str
    author: str
    diff: str
    estimated_diff_tokens: int
    subset: str = ""
    size_tier: str = ""

    @property
    def identity(self):
        return self.repository, self.pull_number


def estimate_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def changed_paths(diff: str) -> List[str]:
    paths = []
    for line in diff.splitlines():
        if not line.startswith("diff --git a/"):
            continue
        remainder = line[len("diff --git a/"):]
        marker = " b/"
        if marker not in remainder:
            continue
        _old_path, new_path = remainder.split(marker, 1)
        paths.append(new_path.strip())
    return paths


def _is_source_path(path: str) -> bool:
    normalized = "/" + path.replace("\\", "/").lower().lstrip("/")
    name = normalized.rsplit("/", 1)[-1]
    if name in _IGNORED_NAMES or name.endswith((".min.js", ".min.css")):
        return False
    if any(part in normalized for part in _IGNORED_PARTS):
        return False
    return name.endswith(_SOURCE_SUFFIXES)


def is_eligible_candidate(candidate: PullRequestCandidate) -> bool:
    author = candidate.author.lower()
    if author.endswith("[bot]") or author in {"dependabot", "renovate"}:
        return False
    if _DEPENDENCY_TITLE.search(candidate.title):
        return False
    paths = changed_paths(candidate.diff)
    return bool(paths) and any(_is_source_path(path) for path in paths)


def candidate_pr_numbers(
    nodes: Iterable[dict], recent_count: int = 8,
    stress_candidates_per_tier: int = 5,
) -> List[int]:
    eligible = []
    for node in nodes:
        author = str((node.get("author") or {}).get("login") or "").lower()
        title = str(node.get("title") or "")
        if author.endswith("[bot]") or _DEPENDENCY_TITLE.search(title):
            continue
        eligible.append(node)

    ordered = [int(item["number"]) for item in eligible[:recent_count]]
    seen = set(ordered)
    for target_tokens in (8_000, 16_000, 32_000):
        target_changes = target_tokens / 12.0
        ranked = sorted(eligible, key=lambda item: (
            abs(
                int(item.get("additions") or 0)
                + int(item.get("deletions") or 0)
                - target_changes
            ),
            int(item["number"]),
        ))
        added = 0
        for item in ranked:
            number = int(item["number"])
            if number in seen:
                continue
            ordered.append(number)
            seen.add(number)
            added += 1
            if added >= stress_candidates_per_tier:
                break
    return ordered


def size_tier(tokens: int) -> str:
    if 6_000 <= tokens < 12_000:
        return "8k"
    if 12_000 <= tokens < 24_000:
        return "16k"
    if 24_000 <= tokens <= 48_000:
        return "32k"
    return ""


def _balanced_tier_sample(
    candidates: Iterable[PullRequestCandidate], tier: str, count: int,
) -> List[PullRequestCandidate]:
    target = {"8k": 8_000, "16k": 16_000, "32k": 32_000}[tier]
    by_repository = {}
    for item in candidates:
        if size_tier(item.estimated_diff_tokens) != tier:
            continue
        by_repository.setdefault(item.repository, []).append(item)
    for items in by_repository.values():
        items.sort(key=lambda item: (
            abs(item.estimated_diff_tokens - target), item.pull_number
        ))

    selected = []
    repositories = sorted(by_repository)
    while len(selected) < count and repositories:
        remaining = []
        for repository in repositories:
            items = by_repository[repository]
            if items and len(selected) < count:
                selected.append(items.pop(0))
            if items:
                remaining.append(repository)
        repositories = remaining
    if len(selected) != count:
        raise ValueError(
            "%s stress tier has %d eligible PRs; %d required"
            % (tier, len(selected), count)
        )
    return selected


def select_context_dataset(
    candidates: Iterable[PullRequestCandidate],
    natural_per_repository: int = 3,
    stress_per_tier: int = 10,
) -> List[PullRequestCandidate]:
    eligible = [item for item in candidates if is_eligible_candidate(item)]
    natural = []
    counts = {}
    for item in eligible:
        count = counts.get(item.repository, 0)
        if count >= natural_per_repository:
            continue
        natural.append(replace(item, subset="natural", size_tier="natural"))
        counts[item.repository] = count + 1

    natural_ids = {item.identity for item in natural}
    remaining = [item for item in eligible if item.identity not in natural_ids]
    stress = []
    for tier in ("8k", "16k", "32k"):
        chosen = _balanced_tier_sample(remaining, tier, stress_per_tier)
        chosen_ids = {item.identity for item in chosen}
        stress.extend(
            replace(item, subset="stress", size_tier=tier)
            for item in chosen
        )
        remaining = [item for item in remaining if item.identity not in chosen_ids]
    return natural + stress


def write_context_dataset(
    selected: Iterable[PullRequestCandidate], output_directory: Path,
) -> Path:
    output_directory = Path(output_directory)
    diff_directory = output_directory / "diffs"
    diff_directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in selected:
        filename = "%s--%d.diff" % (
            item.repository.replace("/", "--"), item.pull_number
        )
        relative_diff = Path("diffs") / filename
        with (output_directory / relative_diff).open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            handle.write(item.diff)
        rows.append({
            "schema_version": 1,
            "id": "github-%s-%d" % (
                item.repository.replace("/", "-"), item.pull_number
            ),
            "repository": item.repository,
            "pull_number": item.pull_number,
            "url": item.url,
            "base_sha": item.base_sha,
            "head_sha": item.head_sha,
            "merged_at": item.merged_at,
            "language": item.language,
            "license": item.license,
            "subset": item.subset,
            "size_tier": item.size_tier,
            "diff_path": relative_diff.as_posix(),
        })
    manifest_path = output_directory / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return manifest_path
