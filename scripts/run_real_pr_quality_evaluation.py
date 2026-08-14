"""Run CapyReview once on the small human-labelled real-PR benchmark."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capyreview.config import Settings  # noqa: E402
from capyreview.real_pr_quality import (  # noqa: E402
    build_real_pr_quality_reviewer,
    build_semantic_issue_judge,
    load_quality_dataset,
    run_quality_evaluation_checkpointed,
)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "model"


def _git_revision() -> dict:
    def run(*args):
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, check=True,
            capture_output=True, text=True, timeout=10,
        )
        return completed.stdout.strip()

    try:
        return {
            "commit": run("rev-parse", "HEAD"),
            "dirty": bool(run("status", "--porcelain")),
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "dirty": True}


def _default_output(split: str, model: str) -> Path:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    return ROOT / "output" / "real-pr-quality" / (
        "%s-%s-%s" % (timestamp, split, _safe_name(model))
    )


def _write_experiment(path: Path, metadata: dict, resume: bool) -> dict:
    if path.exists():
        if not resume:
            raise FileExistsError("experiment already exists: %s" % path.parent)
        existing = json.loads(path.read_text(encoding="utf-8"))
        for field in ("model", "split", "dataset_source_commit", "code_commit"):
            if existing.get(field) != metadata.get(field):
                raise ValueError("cannot resume after %s changed" % field)
        return existing
    if resume:
        raise FileNotFoundError("resume requested but experiment metadata is missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return metadata


def _percent(value: float) -> str:
    return "%.1f%%" % (100 * value)


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _write_report(output: Path, metadata: dict, result: dict) -> tuple[Path, Path]:
    json_path = output / "real-pr-quality-report.json"
    markdown_path = output / "real-pr-quality-report.md"
    if json_path.exists() or markdown_path.exists():
        raise FileExistsError("quality report already exists")
    cases = result["case_results"]
    durations = [case["duration_seconds"] for case in cases]
    report = {
        "schema_version": 1,
        "evaluation": metadata,
        "result": result,
        "latency_seconds": {
            "median": round(statistics.median(durations), 3) if durations else 0.0,
            "p95": _percentile(durations, 0.95),
        },
        "limitations": [
            "The static public PRs may have appeared in model training data.",
            "The workflow and semantic matcher were each run once per PR.",
            "Semantic matching can vary by judge model; the model is recorded.",
            "Repository memory starts empty for every benchmark repository.",
        ],
    }
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    metrics = result["metrics"]
    review_usage = result["review_usage"]
    matcher_usage = result["matcher_usage"]
    lines = [
        "# CapyReview Real PR Quality Evaluation",
        "",
        "- Split: `%s`" % metadata["split"],
        "- Model and semantic matcher: `%s`" % metadata["model"],
        "- Cases: %d real pull requests" % metrics["cases"],
        "- Human golden issues: %d" % (metrics["tp"] + metrics["fn"]),
        "- Code revision: `%s`" % metadata["code_commit"],
        "",
        "| Metric | Result |",
        "|---|---:|",
        "| Precision | %s |" % _percent(metrics["precision"]),
        "| Recall | %s |" % _percent(metrics["recall"]),
        "| F1 | %s |" % _percent(metrics["f1"]),
        "| High-severity recall | %s |" % _percent(
            metrics["high_severity_recall"]
        ),
        "| False positives / PR | %.2f |" % metrics["false_positives_per_pr"],
        "| Execution success | %s |" % _percent(
            metrics["execution_success_rate"]
        ),
        "| Median latency | %.3fs |" % report["latency_seconds"]["median"],
        "| P95 latency | %.3fs |" % report["latency_seconds"]["p95"],
        "",
        "- Review LLM calls: %d; prompt tokens: %d; completion tokens: %d"
        % (
            review_usage.get("llm_calls", 0),
            review_usage.get("prompt_tokens", 0),
            review_usage.get("completion_tokens", 0),
        ),
        "- Semantic matcher calls: %d; prompt tokens: %d; completion tokens: %d"
        % (
            matcher_usage.get("llm_calls", 0),
            matcher_usage.get("prompt_tokens", 0),
            matcher_usage.get("completion_tokens", 0),
        ),
        "",
        "Static real-PR benchmark with human-verified golden issues; not production traffic.",
        "",
    ]
    markdown_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return json_path, markdown_path


def _progress(index, total, result):
    status = "ok" if result.get("execution_success") else "failed"
    print(
        "[%d/%d] %s %s candidates=%d matches=%d" % (
            index, total, result.get("id"), status,
            result.get("candidate_count", 0), len(result.get("matches") or []),
        ),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, choices=("development", "test"))
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "evaluation_data" / "real_pr_quality" / "manifest.json"),
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env()
    config = settings.resolved_llm()
    cases, source = load_quality_dataset(args.dataset, args.split)
    expected = 10 if args.split == "development" else 20
    if len(cases) != expected:
        raise SystemExit("%s split must contain exactly %d cases" % (args.split, expected))
    revision = _git_revision()
    output = Path(args.output_dir).resolve() if args.output_dir else _default_output(
        args.split, str(config["model"])
    )
    metadata = _write_experiment(output / "experiment.json", {
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "provider": "deepseek",
        "model": str(config["model"]),
        "split": args.split,
        "cases": len(cases),
        "golden_issues": sum(len(case["golden_comments"]) for case in cases),
        "dataset_source": str(source.get("name", "")),
        "dataset_source_commit": str(source.get("commit", "")),
        "code_commit": revision["commit"],
        "code_dirty": revision["dirty"],
        "context_max_tokens": settings.context_max_tokens,
        "context_reserved_tokens": settings.context_reserved_tokens,
        "agent_loop_max_steps": settings.agent_loop_max_steps,
        "github_mcp_enabled": bool(settings.github_token.strip()),
        "memory_mode": "cold-start",
    }, args.resume)
    print("output:", output, flush=True)
    print("model:", metadata["model"], flush=True)
    print("split:", metadata["split"], flush=True)

    reviewer = build_real_pr_quality_reviewer(settings)
    matcher = build_semantic_issue_judge(settings)
    result = run_quality_evaluation_checkpointed(
        reviewer, matcher, cases, str(output / "case-results.jsonl"),
        resume=args.resume, on_case=_progress,
    )
    json_path, markdown_path = _write_report(output, metadata, result)
    print("json:", json_path, flush=True)
    print("markdown:", markdown_path, flush=True)
    print(
        "precision=%s recall=%s F1=%s high=%s" % (
            _percent(result["metrics"]["precision"]),
            _percent(result["metrics"]["recall"]),
            _percent(result["metrics"]["f1"]),
            _percent(result["metrics"]["high_severity_recall"]),
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
