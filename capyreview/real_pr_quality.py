"""Small real-PR quality benchmark for the production review workflow."""
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Iterable, List, Mapping

from .diff_parser import parse_unified_diff
from .reviewer import MAX_STRUCTURED_OUTPUT_TOKENS


CORE_CATEGORIES = frozenset({
    "bug", "security", "concurrency", "data", "api",
})
HIGH_SEVERITIES = frozenset({"high", "critical"})


def _all_comments(comments: Iterable[dict]) -> List[dict]:
    selected = []
    for value in comments:
        category = str(value.get("category", "")).strip().lower()
        comment = str(value.get("comment", "")).strip()
        severity = str(value.get("severity", "")).strip().lower()
        if not category or not comment:
            raise ValueError("golden comment requires category and text")
        if severity not in {"low", "medium", "high", "critical"}:
            raise ValueError("golden comment has an invalid severity")
        selected.append({
            "comment": comment,
            "severity": severity,
            "category": category,
        })
    return selected


def _core_comments(comments: Iterable[dict]) -> List[dict]:
    return [
        value for value in _all_comments(comments)
        if value["category"] in CORE_CATEGORIES
    ]


def _pull_request_number(url: str) -> int:
    try:
        return int(str(url).rstrip("/").rsplit("/", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError("golden comment URL must end in a pull request number") from exc


def select_quality_cases(
    records_by_repository: Mapping[str, Iterable[dict]],
    development_per_repository: int = 2,
    test_per_repository: int = 8,
) -> List[dict]:
    """Create the deterministic repository-balanced official benchmark split."""
    required = development_per_repository + test_per_repository
    selected = []
    for source_repository in sorted(records_by_repository):
        eligible = []
        for record in records_by_repository[source_repository]:
            golden_comments = _all_comments(record.get("comments") or [])
            if not golden_comments:
                continue
            scored_golden_comments = [
                value for value in golden_comments
                if value["category"] in CORE_CATEGORIES
            ]
            url = str(record.get("url", "")).strip()
            eligible.append({
                "source_repository": str(source_repository),
                "pr_title": str(record.get("pr_title", "")).strip(),
                "url": url,
                "pull_request": _pull_request_number(url),
                "golden_comments": golden_comments,
                "scored_golden_comments": scored_golden_comments,
                "negative_control": not scored_golden_comments,
            })
        eligible.sort(key=lambda item: (item["pull_request"], item["url"]))
        if len(eligible) < required:
            raise ValueError(
                "%s requires %d eligible PRs but only %d were available"
                % (source_repository, required, len(eligible))
            )
        for index, case in enumerate(eligible[:required]):
            selected.append({
                **case,
                "split": (
                    "development"
                    if index < development_per_repository else "test"
                ),
            })
    return selected


def load_quality_dataset(path: str, split: str = "") -> tuple[List[dict], dict]:
    """Load a pinned manifest and its cached PR diffs."""
    manifest_path = Path(path).resolve()
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") not in {1, 2} or not isinstance(
        manifest.get("cases"), list
    ):
        raise ValueError("unsupported real PR quality dataset manifest")
    if split and split not in {"development", "test"}:
        raise ValueError("quality dataset split must be development or test")
    root = manifest_path.parent
    cases = []
    seen = set()
    for raw in manifest["cases"]:
        if not isinstance(raw, dict):
            raise ValueError("quality dataset case must be an object")
        if split and raw.get("split") != split:
            continue
        case_id = str(raw.get("id", "")).strip()
        case_split = str(raw.get("split", "")).strip()
        repository = str(raw.get("repository", "")).strip()
        if not case_id or case_id in seen:
            raise ValueError("quality dataset case ids must be unique and non-empty")
        if case_split not in {"development", "test"}:
            raise ValueError("quality dataset case has an invalid split")
        if repository.count("/") != 1:
            raise ValueError("quality dataset repository must be owner/repo")
        diff_file = str(raw.get("diff_file", "")).replace("\\", "/").strip()
        diff_path = (root / diff_file).resolve()
        if root != diff_path.parent and root not in diff_path.parents:
            raise ValueError("quality dataset diff must stay inside the dataset")
        try:
            diff = diff_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("quality dataset diff is unavailable: %s" % diff_file) from exc
        parsed = parse_unified_diff(diff)
        if not parsed.files or not parsed.added_lines:
            raise ValueError("quality dataset diff is not a scoreable unified diff")
        all_golden_comments = _all_comments(raw.get("golden_comments") or [])
        if not all_golden_comments:
            raise ValueError("quality dataset case requires a human golden comment")
        golden_comments = [
            value for value in all_golden_comments
            if value["category"] in CORE_CATEGORIES
        ]
        seen.add(case_id)
        cases.append({
            **raw,
            "id": case_id,
            "repository": repository,
            "pull_request": int(raw.get("pull_request")),
            "split": case_split,
            "head_commit": str(raw.get("head_commit", "")).strip(),
            "all_golden_comments": all_golden_comments,
            "golden_comments": golden_comments,
            "negative_control": not golden_comments,
            "diff": diff,
        })
    return cases, dict(manifest.get("source") or {})


def normalize_match_decisions(
    response: Mapping[str, Any], golden_count: int, candidate_count: int,
) -> List[dict]:
    """Validate the semantic judge output as a one-to-one issue matching."""
    raw_matches = response.get("matches")
    if not isinstance(raw_matches, list):
        raise ValueError("semantic judge response must contain a matches array")
    proposed = []
    for raw in raw_matches:
        if not isinstance(raw, dict) or not bool(raw.get("same_issue", False)):
            continue
        try:
            golden_index = int(raw["golden_index"])
            candidate_index = int(raw["candidate_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("semantic match indices must be integers") from exc
        if not 0 <= golden_index < golden_count:
            raise ValueError("semantic match golden index is out of range")
        if not 0 <= candidate_index < candidate_count:
            raise ValueError("semantic match candidate index is out of range")
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
        proposed.append({
            "golden_index": golden_index,
            "candidate_index": candidate_index,
            "confidence": confidence,
            "reason": str(raw.get("reason", "")).strip()[:1000],
        })
    accepted = []
    used_golden = set()
    used_candidates = set()
    for match in sorted(
        proposed,
        key=lambda item: (
            -item["confidence"], item["golden_index"], item["candidate_index"]
        ),
    ):
        if (
            match["golden_index"] in used_golden
            or match["candidate_index"] in used_candidates
        ):
            continue
        used_golden.add(match["golden_index"])
        used_candidates.add(match["candidate_index"])
        accepted.append(match)
    return sorted(accepted, key=lambda item: (
        item["golden_index"], item["candidate_index"]
    ))


class SemanticIssueJudge:
    """Match final Findings to human golden issues without judging code itself."""

    name = "real-pr-semantic-match-judge"
    SYSTEM_PROMPT = """You evaluate code-review results. Match a candidate Finding
to a golden issue only when both describe the same underlying defect and failure
mechanism. Different wording is allowed. Related topics, shared files, or similar
severity are not enough. Return a one-to-one matches array. Include unmatched pairs
nowhere. For every proposed pair return golden_index, candidate_index, same_issue,
confidence, and a brief reason. Return JSON only as {"matches": [...]} ."""

    def __init__(self, request_json: Any, consume_usage: Any = None, model: str = ""):
        self.request_json = request_json
        self.consume_usage = consume_usage
        self.model = str(model)

    def match(
        self, title: str, golden_comments: List[dict], candidates: List[dict],
    ) -> dict:
        if not candidates:
            return {"matches": [], "usage": {}}
        compact_candidates = [{
            key: candidate.get(key)
            for key in (
                "rule_id", "severity", "title", "explanation",
                "path", "line", "evidence",
            )
        } for candidate in candidates]
        content = json.dumps({
            "pull_request_title": title,
            "golden_issues": golden_comments,
            "candidate_findings": compact_candidates,
        }, ensure_ascii=False, separators=(",", ":"))
        response = self.request_json({
            "model": self.model,
            "temperature": 0,
            "max_tokens": MAX_STRUCTURED_OUTPUT_TOKENS,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        })
        matches = normalize_match_decisions(
            response, len(golden_comments), len(candidates)
        )
        usage = self.consume_usage() if callable(self.consume_usage) else {}
        return {"matches": matches, "usage": dict(usage or {})}


def build_real_pr_quality_reviewer(settings: Any) -> Any:
    """Build the production review shape with cold-start repository memory."""
    from .agents import MultiAgentCoordinator
    from .context_manager import ContextManager
    from .mcp import GitHubMcpClient, GitHubMcpToolProvider
    from .reviewer import OpenAICompatibleJudge, OpenAICompatibleReviewer
    from .review_skills import ReviewSkillRegistry, ReviewSkillSelector
    from .service import CORRECTNESS_REVIEW_PROMPT, SECURITY_REVIEW_PROMPT

    config = settings.resolved_llm()

    def specialist(name: str, domains: tuple, prompt: str) -> Any:
        value = OpenAICompatibleReviewer(
            str(config["base_url"]), str(config["api_key"]), str(config["model"]),
            timeout=settings.timeout_seconds, system_prompt=prompt,
            provider="deepseek",
        )
        value.name = name
        value.domains = domains
        return value

    judge = OpenAICompatibleJudge(
        str(config["base_url"]), str(config["api_key"]), str(config["model"]),
        timeout=settings.timeout_seconds, provider="deepseek",
    )
    tool_provider = None
    if settings.github_token.strip():
        tool_provider = GitHubMcpToolProvider(
            GitHubMcpClient(settings.github_token, settings.timeout_seconds)
        )
    return MultiAgentCoordinator(
        [
            specialist(
                "llm-security-specialist", ("security",), SECURITY_REVIEW_PROMPT,
            ),
            specialist(
                "llm-correctness-specialist",
                ("correctness", "reliability", "regression"),
                CORRECTNESS_REVIEW_PROMPT,
            ),
        ],
        max_workers=settings.agent_max_workers,
        agent_retries=settings.agent_retries,
        context_manager=ContextManager(
            settings.context_max_tokens, settings.context_reserved_tokens
        ),
        agent_loop_max_steps=settings.agent_loop_max_steps,
        agent_loop_timeout_seconds=settings.timeout_seconds * 2,
        runtime_timeout_seconds=settings.timeout_seconds * 3,
        judge=judge,
        tool_provider=tool_provider,
        skill_registry=ReviewSkillRegistry(Path(__file__).resolve().parents[1] / "skills"),
        skill_selector=ReviewSkillSelector(),
    )


def build_semantic_issue_judge(settings: Any) -> SemanticIssueJudge:
    from .reviewer import OpenAICompatibleReviewer

    config = settings.resolved_llm()
    client = OpenAICompatibleReviewer(
        str(config["base_url"]), str(config["api_key"]), str(config["model"]),
        timeout=settings.timeout_seconds, provider="deepseek",
    )
    return SemanticIssueJudge(
        client.request_json, client.consume_usage, str(config["model"])
    )


def score_quality_results(results: Iterable[dict]) -> Dict[str, Any]:
    """Aggregate absolute review quality without a comparison or ablation arm."""
    values = list(results)
    tp = fp = fn = high_total = high_hits = successes = 0
    for result in values:
        golden = list(result.get("golden_comments") or [])
        matches = list(result.get("matches") or [])
        candidate_count = max(0, int(result.get("candidate_count", 0)))
        matched_golden = {int(item["golden_index"]) for item in matches}
        matched_candidates = {int(item["candidate_index"]) for item in matches}
        tp += len(matches)
        fp += max(0, candidate_count - len(matched_candidates))
        fn += max(0, len(golden) - len(matched_golden))
        successes += int(bool(result.get("execution_success")))
        for index, comment in enumerate(golden):
            if str(comment.get("severity", "")).lower() in HIGH_SEVERITIES:
                high_total += 1
                high_hits += int(index in matched_golden)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    count = len(values)
    return {
        "cases": count,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "high_severity_total": high_total,
        "high_severity_hits": high_hits,
        "high_severity_recall": high_hits / high_total if high_total else 0.0,
        "execution_success_rate": successes / count if count else 0.0,
        "false_positives_per_pr": fp / count if count else 0.0,
    }


def _finding_dict(finding: Any) -> dict:
    to_dict = getattr(finding, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    raise ValueError("reviewer output must contain Finding objects")


def evaluate_quality_case(reviewer: Any, semantic_judge: Any, case: dict) -> dict:
    """Run the final production Findings through an independent issue matcher."""
    result = {
        "id": str(case["id"]),
        "repository": str(case["repository"]),
        "pull_request": int(case["pull_request"]),
        "split": str(case["split"]),
        "title": str(case.get("title", "")),
        "golden_comments": list(case["golden_comments"]),
        "candidate_count": 0,
        "findings": [],
        "matches": [],
        "execution_success": False,
        "review_usage": {},
        "matcher_usage": {},
        "collaboration": {},
        "duration_seconds": 0.0,
        "error": None,
    }
    started = time.monotonic()
    try:
        parsed = parse_unified_diff(str(case["diff"]))
        contextual_review = getattr(reviewer, "review_with_context", None)
        if callable(contextual_review):
            findings = contextual_review(
                "", str(case["diff"]), parsed,
                repository=str(case["repository"]),
                head_commit=str(case.get("head_commit", "")),
                pull_request=int(case["pull_request"]),
            )
        else:
            findings = reviewer.review(str(case["diff"]), parsed)
        candidates = [_finding_dict(finding) for finding in findings]
        if case["golden_comments"]:
            match_result = semantic_judge.match(
                str(case.get("title", "")),
                list(case["golden_comments"]),
                candidates,
            )
        else:
            match_result = {"matches": [], "usage": {}}
        matches = list(match_result.get("matches") or [])
        # The judge contract is normalized before this point; validate cardinality.
        used_golden = {int(item["golden_index"]) for item in matches}
        used_candidates = {int(item["candidate_index"]) for item in matches}
        if len(used_golden) != len(matches) or len(used_candidates) != len(matches):
            raise ValueError("semantic matches must be one-to-one")
        summary_reader = getattr(reviewer, "collaboration_summary", None)
        collaboration = summary_reader("") if callable(summary_reader) else {}
        result.update({
            "candidate_count": len(candidates),
            "findings": candidates,
            "matches": matches,
            "execution_success": True,
            "review_usage": dict((collaboration or {}).get("usage") or {}),
            "matcher_usage": dict(match_result.get("usage") or {}),
            "collaboration": dict(collaboration or {}),
        })
    except Exception as exc:
        result["error"] = str(exc)[:2000]
    result["duration_seconds"] = round(time.monotonic() - started, 4)
    return result


def _sum_usage(results: Iterable[dict], field: str) -> dict:
    totals: Dict[str, int] = {}
    for result in results:
        for key, value in dict(result.get(field) or {}).items():
            totals[str(key)] = totals.get(str(key), 0) + int(value)
    return totals


def _quality_summary(results: List[dict], duration_seconds: float) -> dict:
    return {
        "schema_version": 1,
        "metrics": score_quality_results(results),
        "by_split": {
            split: score_quality_results([
                result for result in results if result.get("split") == split
            ])
            for split in ("development", "test")
        },
        "review_usage": _sum_usage(results, "review_usage"),
        "matcher_usage": _sum_usage(results, "matcher_usage"),
        "duration_seconds": round(duration_seconds, 4),
        "case_results": results,
    }


def run_quality_evaluation_checkpointed(
    reviewer: Any, semantic_judge: Any, cases: List[dict], progress_path: str,
    resume: bool = False, on_case: Any = None,
) -> dict:
    """Evaluate every PR once and persist each completed result immediately."""
    path = Path(progress_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        if not resume:
            raise FileExistsError("quality evaluation progress already exists")
        with path.open("r", encoding="utf-8") as handle:
            existing = [json.loads(line) for line in handle if line.strip()]
    completed = {str(result["id"]): result for result in existing}
    if len(completed) != len(existing):
        raise ValueError("quality evaluation progress contains duplicate ids")
    case_ids = {str(case["id"]) for case in cases}
    if set(completed) - case_ids:
        raise ValueError("quality evaluation progress does not match the dataset")

    started = time.monotonic()
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for index, case in enumerate(cases, 1):
            case_id = str(case["id"])
            if case_id not in completed:
                completed[case_id] = evaluate_quality_case(
                    reviewer, semantic_judge, case
                )
                handle.write(json.dumps(
                    completed[case_id], ensure_ascii=False, sort_keys=True
                ) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if on_case is not None:
                on_case(index, len(cases), completed[case_id])
    ordered = [completed[str(case["id"])] for case in cases]
    return _quality_summary(ordered, time.monotonic() - started)
