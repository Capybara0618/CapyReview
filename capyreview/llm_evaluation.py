"""Checkpointed evaluation for the production CapyReview LLM workflow."""
import datetime
import hashlib
import json
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from .agents import MultiAgentCoordinator
from .evaluation_harness import EndToEndEvaluationHarness, dataset_fingerprint
from .reviewer import OpenAICompatibleJudge, OpenAICompatibleReviewer, Reviewer


SECURITY_PROMPT = """You are the security specialist in a bounded PR review team.
Trace attacker-controlled data and report exploitable security defects introduced by
added lines. Return the closest CWE identifier in rule_id and copy the exact changed
line into evidence. Emit at most one primary root-cause finding per added line. Do not
report reliability or style-only concerns."""

CORRECTNESS_PROMPT = """You are the correctness and reliability specialist in a
bounded PR review team. Report concrete runtime, error-handling, data-integrity and
regression defects introduced by added lines. Return the closest CWE identifier in
rule_id and copy the exact changed line into evidence. Leave security-dominant defects
to the security specialist and do not report style concerns."""


class EvaluationLLMReviewer(OpenAICompatibleReviewer):
    def __init__(
        self, config: Dict[str, object], name: str, domains: Iterable[str],
        system_prompt: str, timeout: int,
    ):
        super().__init__(
            str(config["base_url"]),
            str(config["api_key"]),
            str(config["model"]),
            timeout=timeout,
            system_prompt=system_prompt,
            provider=str(config.get("provider") or "deepseek"),
            extra_headers=dict(config.get("headers") or {}),
        )
        self.name = name
        self.domains = tuple(domains)


def build_llm_evaluation_reviewer(
    config: Dict[str, object], timeout: int = 60, agent_retries: int = 1,
    agent_loop_max_steps: int = 4, agent_loop_timeout_seconds: int = 240,
) -> MultiAgentCoordinator:
    """Build the same risk-routed LLM shape used by the application."""
    security = EvaluationLLMReviewer(
        config,
        "llm-security-specialist",
        ("security",),
        SECURITY_PROMPT,
        timeout,
    )
    correctness = EvaluationLLMReviewer(
        config,
        "llm-correctness-specialist",
        ("correctness", "reliability", "regression"),
        CORRECTNESS_PROMPT,
        timeout,
    )
    judge = OpenAICompatibleJudge(
        str(config["base_url"]),
        str(config["api_key"]),
        str(config["model"]),
        timeout=timeout,
        provider=str(config.get("provider") or "deepseek"),
        extra_headers=dict(config.get("headers") or {}),
    )
    reviewer = MultiAgentCoordinator(
        [security, correctness],
        max_workers=2,
        agent_retries=agent_retries,
        agent_loop_max_steps=agent_loop_max_steps,
        agent_loop_timeout_seconds=agent_loop_timeout_seconds,
        judge=judge,
    )
    reviewer.name = "capyreview-llm-workflow"
    return reviewer


def _evaluation_summary(
    reviewer: Reviewer, cases: List[dict], case_results: List[dict],
    duration_seconds: float,
) -> Dict[str, Any]:
    harness = EndToEndEvaluationHarness()
    totals = harness._empty_totals()
    for result in case_results:
        harness._accumulate(totals, result)
    by_split = {}
    for split in ("validation", "holdout"):
        split_totals = harness._empty_totals()
        for result in case_results:
            if result["split"] == split:
                harness._accumulate(split_totals, result)
        by_split[split] = harness._metrics(split_totals)
    return {
        "schema_version": 2,
        "name": reviewer.name,
        "reviewer": reviewer.name,
        "dataset": {
            "cases": len(cases),
            "repositories": len({case["repository"] for case in cases}),
            "risk_cases": sum(bool(case["expected_findings"]) for case in cases),
            "clean_cases": sum(not case["expected_findings"] for case in cases),
            "source_kinds": sorted({
                str((case.get("source") or {}).get("kind", "unknown"))
                for case in cases
            }),
            "sha256": dataset_fingerprint(cases),
        },
        "metrics": harness._metrics(totals),
        "by_split": by_split,
        "duration_seconds": round(duration_seconds, 4),
        "case_results": case_results,
    }


def run_evaluation_checkpointed(
    reviewer: Reviewer, cases: List[dict], progress_path: str,
    resume: bool = False,
    on_case: Optional[Callable[[int, int, dict], None]] = None,
) -> Dict[str, Any]:
    """Run each case once and persist it before moving to the next case."""
    os.makedirs(os.path.dirname(os.path.abspath(progress_path)), exist_ok=True)
    existing = []
    if os.path.exists(progress_path):
        if not resume:
            raise FileExistsError("progress artifact already exists: %s" % progress_path)
        with open(progress_path, "r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    existing.append(json.loads(raw))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "invalid progress JSON on line %d" % line_number
                    ) from exc
    completed = {str(item["id"]): item for item in existing}
    if len(completed) != len(existing):
        raise ValueError("progress artifact contains duplicate case ids")
    valid_ids = {str(case["id"]) for case in cases}
    if set(completed) - valid_ids:
        raise ValueError("progress artifact does not match the current dataset")

    harness = EndToEndEvaluationHarness()
    started = time.monotonic()
    with open(progress_path, "a", encoding="utf-8", newline="\n") as handle:
        for index, case in enumerate(cases, 1):
            case_id = str(case["id"])
            if case_id in completed:
                if on_case is not None:
                    on_case(index, len(cases), completed[case_id])
                continue
            result = harness._run_case(reviewer, case)
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            completed[case_id] = result
            if on_case is not None:
                on_case(index, len(cases), result)
    ordered = [completed[str(case["id"])] for case in cases]
    return _evaluation_summary(
        reviewer, cases, ordered, time.monotonic() - started
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_evaluation_metadata(
    config: Dict[str, object], cases: List[dict], source_sha256: str,
) -> Dict[str, Any]:
    safe_config = {
        "provider": str(config.get("provider") or "deepseek"),
        "base_url": str(config["base_url"]),
        "model": str(config["model"]),
    }
    prompt_hashes = {
        "security": _sha256_text(SECURITY_PROMPT),
        "correctness": _sha256_text(CORRECTNESS_PROMPT),
        "judge": _sha256_text(OpenAICompatibleJudge.SYSTEM_PROMPT),
    }
    return {
        **safe_config,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dataset_sha256": dataset_fingerprint(cases),
        "dataset_cases": len(cases),
        "risk_cases": sum(bool(case["expected_findings"]) for case in cases),
        "clean_cases": sum(not case["expected_findings"] for case in cases),
        "source_sha256": source_sha256,
        "prompt_sha256": prompt_hashes,
        "configuration_sha256": _sha256_text(json.dumps(
            {**safe_config, "prompt_sha256": prompt_hashes}, sort_keys=True
        )),
        "dataset_kind": "synthetic-controlled",
    }


def _percent(value: float) -> str:
    return "%.1f%%" % (100 * value)


def write_evaluation_report(
    output_dir: str, metadata: Dict[str, Any], result: Dict[str, Any],
) -> Dict[str, str]:
    """Write one immutable report for CapyReview itself, without a comparison arm."""
    json_path = os.path.join(output_dir, "llm-evaluation-report.json")
    markdown_path = os.path.join(output_dir, "llm-evaluation-report.md")
    for path in (json_path, markdown_path):
        if os.path.exists(path):
            raise FileExistsError("evaluation report already exists: %s" % path)
    if metadata["dataset_sha256"] != result["dataset"]["sha256"]:
        raise ValueError("evaluation metadata and result dataset fingerprints differ")
    report = {
        "schema_version": 2,
        "evaluation": metadata,
        "result": result,
        "limitations": [
            "The benchmark contains controlled synthetic diffs, not production PR traffic.",
            "The workflow was run once; the report does not estimate model variance.",
            "Failed API or runtime cases remain in the scored denominator.",
        ],
    }
    os.makedirs(output_dir, exist_ok=True)
    with open(json_path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    metrics = result["metrics"]
    lines = [
        "# CapyReview LLM Evaluation",
        "",
        "- Model: `%s`" % metadata["model"],
        "- Dataset: %d controlled synthetic diffs (%d risk, %d clean)" % (
            metadata["dataset_cases"], metadata["risk_cases"], metadata["clean_cases"]
        ),
        "- Dataset SHA-256: `%s`" % metadata["dataset_sha256"],
        "",
        "| Metric | Result |",
        "|---|---:|",
        "| Precision | %s |" % _percent(metrics["precision"]),
        "| Recall | %s |" % _percent(metrics["recall"]),
        "| F1 | %s |" % _percent(metrics["f1"]),
        "| High-risk recall | %s |" % _percent(metrics["high_risk_recall"]),
        "| Clean-PR accuracy | %s |" % _percent(metrics["clean_accuracy"]),
        "| Evidence validity | %s |" % _percent(metrics["evidence_valid_rate"]),
        "| Execution success | %s |" % _percent(metrics["execution_success_rate"]),
        "",
        "Controlled synthetic data; do not present as production PR performance.",
        "",
    ]
    with open(markdown_path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return {"json": json_path, "markdown": markdown_path}
