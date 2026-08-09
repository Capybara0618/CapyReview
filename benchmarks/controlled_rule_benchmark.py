"""Historical deterministic rule benchmark, isolated from the LLM-only product path.

The rule sets are restored from historical upstream commit
e26148cb0af84b1803177fd2eb8ce968cd3c831a. They exist only to reproduce the
controlled 100-case benchmark; production modules must never import this package.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable, List, Pattern, Sequence

from capyreview.diff_parser import ParsedDiff
from capyreview.evaluation_harness import EndToEndEvaluationHarness, dataset_fingerprint
from capyreview.models import Finding, Severity
from capyreview.reviewer import Reviewer


UPSTREAM_COMMIT = "e26148cb0af84b1803177fd2eb8ce968cd3c831a"
FROZEN_DATASET_SHA256 = "aea871d1319177c603d2cc261c452b092c07e66e3c8210c84ee8c8b6612ef8e9"


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    severity: Severity
    pattern: Pattern[str]
    title: str


class _RegexRuleReviewer(Reviewer):
    rules: Sequence[_Rule] = ()

    def review(self, _diff: str, parsed: ParsedDiff) -> List[Finding]:
        findings = []
        seen = set()
        for line in parsed.added_lines:
            if line.path.endswith((".lock", ".min.js", ".map")):
                continue
            for rule in self.rules:
                key = (rule.rule_id, line.path, line.line)
                if key in seen or not rule.pattern.search(line.content):
                    continue
                seen.add(key)
                findings.append(Finding(
                    rule_id=rule.rule_id,
                    severity=rule.severity,
                    title=rule.title,
                    explanation="The added line matches a controlled benchmark rule.",
                    path=line.path,
                    line=line.line,
                    evidence=line.content.strip()[:240],
                    fix="Replace the unsafe operation with a constrained alternative.",
                    test="Add a focused regression test for the risky input.",
                    confidence=0.9,
                ))
        return findings


class LocalRuleReviewer(_RegexRuleReviewer):
    """Six core rules used by the historical baseline."""

    name = "controlled-core-rules"
    rules = (
        _Rule("SEC-EVAL", Severity.CRITICAL, re.compile(r"\b(eval|exec)\s*\("), "Dynamic code execution"),
        _Rule("SEC-SUBPROCESS-SHELL", Severity.HIGH, re.compile(r"\bshell\s*=\s*True\b"), "Shell command injection"),
        _Rule(
            "SEC-HARDCODED-SECRET", Severity.HIGH,
            re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b\s*=\s*['\"][^'\"]{4,}['\"]"),
            "Hard-coded credential",
        ),
        _Rule(
            "SEC-SQL-CONCAT", Severity.HIGH,
            re.compile(r"(?i)(execute|query)\s*\(\s*(f['\"]|['\"].*(\+|%))"),
            "Dynamic SQL concatenation",
        ),
        _Rule(
            "REL-EMPTY-EXCEPT", Severity.MEDIUM,
            re.compile(r"^\s*except\s*(Exception\s*)?:\s*(pass)?\s*$"),
            "Broad exception handling",
        ),
        _Rule("REL-DEBUG-PRINT", Severity.LOW, re.compile(r"\b(print\s*\(|console\.log\s*\()"), "Debug output"),
    )


class ContextRuleReviewer(_RegexRuleReviewer):
    """Eight additional rules used by the historical candidate."""

    name = "controlled-context-rules"
    rules = (
        _Rule("SEC-PATH-TRAVERSAL", Severity.HIGH, re.compile(r"open\(base\s*/\s*user_path\)"), "Path traversal"),
        _Rule("SEC-YAML-LOAD", Severity.HIGH, re.compile(r"\byaml\.load\s*\("), "Unsafe YAML loading"),
        _Rule("SEC-WEAK-HASH", Severity.MEDIUM, re.compile(r"\bhashlib\.md5\s*\("), "Weak hash"),
        _Rule("SEC-INSECURE-TEMPFILE", Severity.MEDIUM, re.compile(r"\btempfile\.mktemp\s*\("), "Insecure temporary file"),
        _Rule("SEC-WEAK-RANDOM", Severity.MEDIUM, re.compile(r"\brandom\.random\s*\("), "Weak randomness"),
        _Rule("REL-UNBOUNDED-RETRY", Severity.MEDIUM, re.compile(r"^\s*while\s+True\s*:"), "Unbounded retry"),
        _Rule("SEC-ASSERT-AUTH", Severity.MEDIUM, re.compile(r"^\s*assert\s+user\.is_admin"), "Assertion-based authorization"),
        _Rule(
            "SEC-INSECURE-COOKIE", Severity.MEDIUM,
            re.compile(r"set_cookie\(.+secure\s*=\s*False"),
            "Insecure cookie",
        ),
    )


class RuleEnsembleReviewer(Reviewer):
    """Small benchmark-only merger; it is not a production Agent coordinator."""

    name = "controlled-core-plus-context-rules"

    def __init__(self, reviewers: Iterable[Reviewer]):
        self.reviewers = tuple(reviewers)

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        merged = {}
        for reviewer in self.reviewers:
            for finding in reviewer.review(diff, parsed):
                merged[(finding.path, finding.line, finding.rule_id)] = finding
        severity_order = {
            Severity.CRITICAL: 0, Severity.HIGH: 1,
            Severity.MEDIUM: 2, Severity.LOW: 3,
        }
        return sorted(
            merged.values(),
            key=lambda item: (severity_order[item.severity], item.path, item.line),
        )


def run_controlled_rule_benchmark(cases: List[dict]) -> dict:
    """Run the historical baseline and candidate on the frozen controlled corpus."""

    risk_cases = sum(bool(case.get("expected_findings")) for case in cases)
    clean_cases = len(cases) - risk_cases
    repositories = {case.get("repository") for case in cases}
    if len(cases) != 100 or risk_cases != 40 or clean_cases != 60:
        raise ValueError(
            "controlled rule benchmark requires exactly 100 cases "
            "(40 risk and 60 clean)"
        )
    if len(repositories) != 10 or dataset_fingerprint(cases) != FROZEN_DATASET_SHA256:
        raise ValueError("controlled rule benchmark dataset fingerprint does not match")

    harness = EndToEndEvaluationHarness()
    baseline = harness.run(LocalRuleReviewer(), cases, "controlled-core-rules")
    candidate = harness.run(
        RuleEnsembleReviewer((LocalRuleReviewer(), ContextRuleReviewer())),
        cases,
        "controlled-core-plus-context-rules",
    )
    metric_names = ("precision", "recall", "f1", "high_risk_recall", "clean_accuracy")
    return {
        "schema_version": 1,
        "protocol": "historical-controlled-rule-benchmark-v1",
        "provenance": {
            "upstream_commit": UPSTREAM_COMMIT,
            "upstream_owner": "God1007",
        },
        "boundary": (
            "Offline deterministic rule-coverage benchmark only; not part of the "
            "DeepSeek runtime and not evidence of LLM or Multi-Agent improvement."
        ),
        "dataset": candidate["dataset"],
        "baseline": baseline,
        "candidate": candidate,
        "deltas": {
            name: round(candidate["metrics"][name] - baseline["metrics"][name], 4)
            for name in metric_names
        },
    }


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def controlled_rule_markdown(report: dict) -> str:
    baseline = report["baseline"]["metrics"]
    candidate = report["candidate"]["metrics"]
    dataset = report["dataset"]
    rows = (
        ("Precision", "precision"),
        ("Recall", "recall"),
        ("F1", "f1"),
        ("High-risk recall", "high_risk_recall"),
        ("Clean-PR accuracy", "clean_accuracy"),
    )
    lines = [
        "# Historical Controlled Rule Benchmark",
        "",
        f"> Boundary: {report['boundary']}",
        "",
        "## Dataset",
        "",
        f"- Cases: {dataset['cases']} ({dataset['risk_cases']} risk, {dataset['clean_cases']} clean)",
        f"- Repositories: {dataset['repositories']}",
        f"- Canonical SHA-256: `{dataset['sha256']}`",
        f"- Upstream commit: `{report['provenance']['upstream_commit']}`",
        f"- Upstream owner: `{report['provenance']['upstream_owner']}`",
        "",
        "## Detection results",
        "",
        "| Metric | Core rules | Core + context rules | Delta |",
        "|---|---:|---:|---:|",
    ]
    for label, key in rows:
        lines.append(
            f"| {label} | {_percent(baseline[key])} | {_percent(candidate[key])} "
            f"| {report['deltas'][key] * 100:+.1f} pp |"
        )
    lines.extend([
        "",
        "Counts: baseline TP/FP/FN = "
        f"{baseline['tp']}/{baseline['fp']}/{baseline['fn']}; "
        "candidate TP/FP/FN = "
        f"{candidate['tp']}/{candidate['fp']}/{candidate['fn']}.",
        "",
        "The candidate adds eight deterministic context-sensitive rules to the six "
        "core rules. The result measures rule coverage on a synthetic-controlled "
        "corpus; it does not measure the current DeepSeek review chain.",
        "",
    ])
    return "\n".join(lines)


def write_controlled_rule_report(output_dir: str, report: dict):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "controlled-rule-benchmark-report.json"
    markdown_path = directory / "controlled-rule-benchmark-report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(controlled_rule_markdown(report), encoding="utf-8")
    return json_path, markdown_path
