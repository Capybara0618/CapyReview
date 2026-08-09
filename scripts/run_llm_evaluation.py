"""Run one checkpointed evaluation of the production CapyReview workflow."""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from capyreview.config import Settings  # noqa: E402
from capyreview.evaluation_harness import load_jsonl  # noqa: E402
from capyreview.llm_evaluation import (  # noqa: E402
    build_evaluation_metadata,
    build_llm_evaluation_reviewer,
    run_evaluation_checkpointed,
    write_evaluation_report,
)


def source_fingerprint() -> str:
    paths = []
    for directory, _subdirs, files in os.walk(os.path.join(ROOT, "capyreview")):
        for filename in files:
            if filename.endswith(".py"):
                paths.append(os.path.join(directory, filename))
    paths.append(os.path.abspath(__file__))
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = os.path.relpath(path, ROOT).replace("\\", "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\n")
    return digest.hexdigest()


def select_smoke_cases(cases):
    risk = next(case for case in cases if case["expected_findings"])
    clean = next(case for case in cases if not case["expected_findings"])
    return [risk, clean]


def default_output_dir(smoke: bool, model: str) -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "-", model).strip("-") or "model"
    root = os.path.join(ROOT, "tmp" if smoke else "output")
    category = "llm-evaluation-smoke" if smoke else "llm-evaluation"
    return os.path.join(root, category, "%s-%s" % (timestamp, safe_model))


def load_or_create_manifest(output_dir, metadata, resume):
    path = os.path.join(output_dir, "experiment.json")
    if os.path.exists(path):
        if not resume:
            raise FileExistsError("experiment already exists: %s" % output_dir)
        with open(path, "r", encoding="utf-8") as handle:
            existing = json.load(handle)
        for field in (
            "provider", "model", "dataset_sha256", "source_sha256",
            "configuration_sha256",
        ):
            if existing.get(field) != metadata.get(field):
                raise ValueError("cannot resume: experiment %s changed" % field)
        return existing
    if resume:
        raise FileNotFoundError("resume requested but experiment manifest is missing")
    os.makedirs(output_dir, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return metadata


def progress_label():
    def show(index, total, result):
        status = "ok" if result.get("execution_success") else "failed"
        print(
            "[%d/%d] %s %s" % (index, total, result.get("id"), status),
            flush=True,
        )
    return show


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", default=os.path.join(ROOT, "evaluation_data", "pr_diff_100.jsonl")
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env()
    config = settings.resolved_llm()
    cases = load_jsonl(args.dataset)
    if args.smoke:
        cases = select_smoke_cases(cases)
    else:
        risk_cases = sum(bool(case["expected_findings"]) for case in cases)
        if len(cases) != 100 or risk_cases != 40:
            raise SystemExit("formal dataset must contain exactly 100 cases and 40 risks")

    output_dir = os.path.abspath(
        args.output_dir or default_output_dir(args.smoke, str(config["model"]))
    )
    metadata = build_evaluation_metadata(config, cases, source_fingerprint())
    metadata["mode"] = "smoke" if args.smoke else "formal"
    metadata = load_or_create_manifest(output_dir, metadata, args.resume)
    print("output:", output_dir, flush=True)
    print("provider:", metadata["provider"], flush=True)
    print("model:", metadata["model"], flush=True)
    print("dataset_sha256:", metadata["dataset_sha256"], flush=True)

    reviewer = build_llm_evaluation_reviewer(
        config,
        timeout=settings.timeout_seconds,
        agent_retries=settings.agent_retries,
        agent_loop_max_steps=settings.agent_loop_max_steps,
        agent_loop_timeout_seconds=settings.agent_loop_timeout_seconds,
    )
    result = run_evaluation_checkpointed(
        reviewer, cases, os.path.join(output_dir, "case-results.jsonl"),
        resume=args.resume, on_case=progress_label(),
    )
    paths = write_evaluation_report(output_dir, metadata, result)
    print("json:", paths["json"], flush=True)
    print("markdown:", paths["markdown"], flush=True)
    print(
        "F1=%.1f%%; high-risk recall=%.1f%%; "
        "clean accuracy=%.1f%%; evidence validity=%.1f%%" % (
            result["metrics"]["f1"] * 100,
            result["metrics"]["high_risk_recall"] * 100,
            result["metrics"]["clean_accuracy"] * 100,
            result["metrics"]["evidence_valid_rate"] * 100,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
