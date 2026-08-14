"""Offline context-budget benchmark over pinned public GitHub PR diffs."""

from collections import Counter
import json
from pathlib import Path
from statistics import mean, median
from typing import Dict, Iterable, List, Tuple

from .context_manager import ContextManager


SYSTEM_PROMPT = (
    "You are a bounded pull-request reviewer. Inspect only introduced changes, "
    "use read-only tools when evidence is missing, and return exact changed-line "
    "evidence in structured JSON."
)
SKILLS = [{
    "name": "review-reliability", "version": 1,
    "body": "Check correctness and reliability at changed-line boundaries.",
}]
TOOLS = [
    {"name": name, "description": description, "parameters": schema}
    for name, description, schema in (
        (
            "read_code_context", "Read source near a changed line.",
            {"type": "object", "properties": {
                "path": {"type": "string"}, "line": {"type": "integer"},
            }, "required": ["path", "line"]},
        ),
        (
            "search_repository", "Search code in the current repository.",
            {"type": "object", "properties": {
                "query": {"type": "string"}, "path": {"type": "string"},
            }, "required": ["query"]},
        ),
        (
            "read_file_history", "Read recent history for a changed file.",
            {"type": "object", "properties": {
                "path": {"type": "string"},
            }, "required": ["path"]},
        ),
        (
            "read_code_scanning_findings", "Read code-scanning alerts.",
            {"type": "object", "properties": {
                "severity": {"type": "string"},
            }},
        ),
        (
            "read_ci_failures", "Read failing CI checks.",
            {"type": "object", "properties": {}},
        ),
    )
]
MEMORIES = [{
    "scope": "semantic", "kind": "review_feedback",
    "content": "Require exact evidence for every reported changed line.",
    "recall_score": 0.9,
}]
OBSERVATIONS = [{
    "id": "O1", "step": 1, "tool": "read_code_context", "ok": True,
    "result": {"path": "assigned-file", "content": "bounded source context"},
}]


def _changed_lines(diff: str) -> Counter:
    result = Counter()
    path = "unknown"
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            in_hunk = False
            continue
        if line.startswith("@@ "):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result[(path, "+", line[1:])] += 1
        elif line.startswith("-") and not line.startswith("---"):
            result[(path, "-", line[1:])] += 1
    return result


def _coverage(expected: Counter, actual: Counter) -> Tuple[float, int]:
    total = sum(expected.values())
    covered = sum(min(count, actual[key]) for key, count in expected.items())
    duplicates = sum(max(0, count - expected[key]) for key, count in actual.items())
    return (covered / total if total else 1.0), duplicates


def _aggregate(items: Iterable[dict]) -> dict:
    items = list(items)
    if not items:
        return {"cases": 0}
    return {
        "cases": len(items),
        "compression_rate": round(mean(item["compressed"] for item in items), 4),
        "batch_rate": round(mean(item["batch_count"] > 1 for item in items), 4),
        "median_batch_count": median(item["batch_count"] for item in items),
        "max_batch_count": max(item["batch_count"] for item in items),
        "average_cumulative_token_ratio": round(mean(
            item["cumulative_token_ratio"] for item in items
        ), 4),
    }


def run_real_pr_context_benchmark(
    dataset_directory: Path, max_tokens: int = 12_000,
    reserved_tokens: int = 2_500,
) -> Dict[str, object]:
    dataset_directory = Path(dataset_directory)
    rows = [
        json.loads(line)
        for line in (dataset_directory / "manifest.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    manager = ContextManager(max_tokens=max_tokens, reserved_tokens=reserved_tokens)
    results: List[dict] = []
    for row in rows:
        diff = (dataset_directory / row["diff_path"]).read_text(encoding="utf-8")
        assignment = {
            "agent": "correctness-reviewer",
            "objective": "review introduced correctness and reliability risks",
            "files": [], "risk_domains": ["correctness", "reliability"],
        }
        fixed_tokens = (
            manager.contract_tokens(SYSTEM_PROMPT, assignment, SKILLS, TOOLS)
            + manager.runtime_tokens(
                observations=OBSERVATIONS, memories=MEMORIES
            )
            + manager.estimate_tokens("DIFF_CONTEXT:\n")
        )
        bundles = manager.build_batches(
            diff, assignment, fixed_tokens=fixed_tokens
        )
        managed = [
            manager.compose(
                bundle, assignment, system_prompt=SYSTEM_PROMPT,
                skills=SKILLS, tools=TOOLS, observations=OBSERVATIONS,
                memories=MEMORIES,
            )
            for bundle in bundles
        ]
        expected = _changed_lines(diff)
        actual = Counter()
        for bundle in bundles:
            actual.update(_changed_lines(bundle.text))
        coverage, duplicates = _coverage(expected, actual)
        original_tokens = fixed_tokens + manager.estimate_tokens(diff)
        total_batch_tokens = sum(item.estimated_tokens for item in managed)
        results.append({
            "id": row["id"],
            "repository": row["repository"],
            "pull_number": row["pull_number"],
            "url": row["url"],
            "language": row["language"],
            "subset": row["subset"],
            "size_tier": row["size_tier"],
            "original_complete_tokens": original_tokens,
            "compressed": any(bundle.compressed for bundle in bundles),
            "batch_count": len(bundles),
            "strategies": sorted({bundle.strategy for bundle in bundles}),
            "max_batch_tokens": max(item.estimated_tokens for item in managed),
            "total_batch_tokens": total_batch_tokens,
            "cumulative_token_ratio": round(
                total_batch_tokens / original_tokens, 4
            ),
            "within_budget": all(
                item.estimated_tokens <= max_tokens for item in managed
            ),
            "changed_line_coverage": round(coverage, 4),
            "duplicate_changed_lines": duplicates,
            "changed_lines": sum(expected.values()),
        })
    subsets = {
        subset: _aggregate(item for item in results if item["subset"] == subset)
        for subset in ("natural", "stress")
    }
    return {
        "schema_version": 1,
        "dataset": "github-public-merged-pr-context",
        "cases": len(results),
        "context_max_tokens": max_tokens,
        "reserved_tokens": reserved_tokens,
        "contract_tokens": (
            results[0]["original_complete_tokens"]
            - manager.estimate_tokens(
                (dataset_directory / rows[0]["diff_path"]).read_text(
                    encoding="utf-8"
                )
            ) if results else 0
        ),
        "budget_compliance_rate": round(mean(
            item["within_budget"] for item in results
        ), 4) if results else 1.0,
        "changed_line_coverage_rate": round(mean(
            item["changed_line_coverage"] for item in results
        ), 4) if results else 1.0,
        "duplicate_changed_lines": sum(
            item["duplicate_changed_lines"] for item in results
        ),
        "subsets": subsets,
        "size_tiers": {
            tier: _aggregate(item for item in results if item["size_tier"] == tier)
            for tier in ("8k", "16k", "32k")
        },
        "case_results": results,
    }
