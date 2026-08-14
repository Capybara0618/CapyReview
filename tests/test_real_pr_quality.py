import json
import tempfile
import unittest
from pathlib import Path


from capyreview.real_pr_quality import (
    CORE_CATEGORIES,
    SemanticIssueJudge,
    evaluate_quality_case,
    load_quality_dataset,
    normalize_match_decisions,
    run_quality_evaluation_checkpointed,
    score_quality_results,
    select_quality_cases,
)
from capyreview.models import Finding, Severity


class RealPRQualityDatasetTests(unittest.TestCase):
    def test_selects_two_development_and_four_test_cases_per_repository(self):
        records = {}
        for repository in ("alpha", "bravo", "charlie", "delta", "echo"):
            records[repository] = [
                {
                    "pr_title": "PR %d" % number,
                    "url": "https://github.com/example/%s/pull/%d"
                    % (repository, number),
                    "comments": [
                        {
                            "comment": "Concrete defect %d" % number,
                            "severity": "High",
                            "category": "bug",
                        },
                        {
                            "comment": "Formatting preference",
                            "severity": "Low",
                            "category": "style",
                        },
                    ],
                }
                for number in range(1, 8)
            ]

        selected = select_quality_cases(records)

        self.assertEqual(30, len(selected))
        for repository in records:
            repository_cases = [
                case for case in selected if case["source_repository"] == repository
            ]
            self.assertEqual(
                ["development", "development", "test", "test", "test", "test"],
                [case["split"] for case in repository_cases],
            )
            self.assertTrue(all(
                len(case["golden_comments"]) == 1
                and case["golden_comments"][0]["category"] in CORE_CATEGORIES
                for case in repository_cases
            ))

    def test_skips_prs_without_core_defect_comments(self):
        records = {
            "alpha": [
                {
                    "pr_title": "style only",
                    "url": "https://github.com/example/alpha/pull/1",
                    "comments": [{
                        "comment": "Rename this variable",
                        "severity": "Low",
                        "category": "style",
                    }],
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "six eligible PRs"):
            select_quality_cases(records)

    def test_loads_cached_diff_and_filters_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "diffs").mkdir()
            (root / "diffs" / "sample.diff").write_text(
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n+++ b/app.py\n"
                "@@ -0,0 +1 @@\n+print('changed')\n",
                encoding="utf-8",
            )
            manifest = {
                "schema_version": 1,
                "source": {"name": "Code Review Bench", "commit": "pinned"},
                "cases": [{
                    "id": "example-repo-1",
                    "source_repository": "example",
                    "repository": "example/repo",
                    "pull_request": 1,
                    "split": "development",
                    "title": "Fix defect",
                    "url": "https://github.com/example/repo/pull/1",
                    "base_commit": "base",
                    "head_commit": "head",
                    "diff_file": "diffs/sample.diff",
                    "golden_comments": [{
                        "comment": "The new line crashes.",
                        "severity": "high",
                        "category": "bug",
                    }],
                }],
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            cases, source = load_quality_dataset(
                str(root / "manifest.json"), split="development"
            )

            self.assertEqual("pinned", source["commit"])
            self.assertEqual(1, len(cases))
            self.assertIn("+print('changed')", cases[0]["diff"])

    def test_rejects_diff_paths_outside_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "schema_version": 1,
                "source": {},
                "cases": [{
                    "id": "unsafe",
                    "repository": "example/repo",
                    "pull_request": 1,
                    "split": "test",
                    "title": "Unsafe path",
                    "url": "https://github.com/example/repo/pull/1",
                    "head_commit": "head",
                    "diff_file": "../outside.diff",
                    "golden_comments": [{
                        "comment": "Defect",
                        "severity": "high",
                        "category": "bug",
                    }],
                }],
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "inside the dataset"):
                load_quality_dataset(str(root / "manifest.json"))


class RealPRQualityMatchingTests(unittest.TestCase):
    def test_normalizes_one_to_one_semantic_matches(self):
        decisions = normalize_match_decisions(
            {
                "matches": [
                    {
                        "golden_index": 0,
                        "candidate_index": 1,
                        "same_issue": True,
                        "confidence": 0.92,
                        "reason": "Both describe the same missing null check.",
                    },
                    {
                        "golden_index": 1,
                        "candidate_index": 0,
                        "same_issue": False,
                        "confidence": 0.80,
                        "reason": "Different failure mode.",
                    },
                ]
            },
            golden_count=2,
            candidate_count=2,
        )

        self.assertEqual(1, len(decisions))
        self.assertEqual(0, decisions[0]["golden_index"])
        self.assertEqual(1, decisions[0]["candidate_index"])

    def test_rejects_duplicate_candidate_matches(self):
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            normalize_match_decisions(
                {
                    "matches": [
                        {
                            "golden_index": 0,
                            "candidate_index": 0,
                            "same_issue": True,
                        },
                        {
                            "golden_index": 1,
                            "candidate_index": 0,
                            "same_issue": True,
                        },
                    ]
                },
                golden_count=2,
                candidate_count=1,
            )

    def test_scores_absolute_quality_and_high_severity_recall(self):
        results = [
            {
                "split": "test",
                "execution_success": True,
                "golden_comments": [
                    {"severity": "Critical"},
                    {"severity": "Medium"},
                ],
                "candidate_count": 3,
                "matches": [
                    {"golden_index": 0, "candidate_index": 1},
                ],
            },
            {
                "split": "test",
                "execution_success": True,
                "golden_comments": [{"severity": "High"}],
                "candidate_count": 1,
                "matches": [
                    {"golden_index": 0, "candidate_index": 0},
                ],
            },
            {
                "split": "test",
                "execution_success": False,
                "golden_comments": [{"severity": "High"}],
                "candidate_count": 0,
                "matches": [],
            },
        ]

        metrics = score_quality_results(results)

        self.assertEqual(2, metrics["tp"])
        self.assertEqual(2, metrics["fp"])
        self.assertEqual(2, metrics["fn"])
        self.assertEqual(0.5, metrics["precision"])
        self.assertEqual(0.5, metrics["recall"])
        self.assertEqual(0.5, metrics["f1"])
        self.assertEqual(2 / 3, metrics["high_severity_recall"])
        self.assertEqual(2 / 3, metrics["execution_success_rate"])
        self.assertEqual(2 / 3, metrics["false_positives_per_pr"])

    def test_semantic_judge_sends_only_issue_descriptions_and_records_usage(self):
        requests = []

        def request_json(payload):
            requests.append(payload)
            return {"matches": [{
                "golden_index": 0,
                "candidate_index": 0,
                "same_issue": True,
                "confidence": 0.9,
                "reason": "Same defect.",
            }]}

        judge = SemanticIssueJudge(
            request_json, lambda: {"llm_calls": 1, "prompt_tokens": 25},
            model="judge-model",
        )
        result = judge.match(
            "Fix null handling",
            [{"comment": "Null dereference", "severity": "high", "category": "bug"}],
            [{
                "title": "Missing null check", "explanation": "May crash",
                "path": "app.py", "line": 4, "evidence": "value.use()",
                "severity": "high", "rule_id": "CWE-476",
            }],
        )

        self.assertEqual("judge-model", requests[0]["model"])
        self.assertNotIn("diff", requests[0]["messages"][1]["content"].lower())
        self.assertEqual(1, len(result["matches"]))
        self.assertEqual(25, result["usage"]["prompt_tokens"])


class _FakeQualityReviewer:
    name = "fake-production-reviewer"

    def __init__(self):
        self.calls = []

    def review_with_context(
        self, task_id, diff, parsed, repository="", head_commit="",
        pull_request=None,
    ):
        self.calls.append((repository, head_commit, pull_request))
        return [
            Finding(
                "CWE-476", Severity.HIGH, "Missing null check",
                "The added dereference can fail.", "app.py", 1,
                "value.use()", "Check value before use.", "Add a null test.", 0.9,
            ),
            Finding(
                "CWE-400", Severity.MEDIUM, "Unbounded work",
                "The loop has no limit.", "app.py", 1,
                "value.use()", "Bound the work.", "Add a stress test.", 0.7,
            ),
        ]

    def collaboration_summary(self, task_id):
        return {
            "route": "high-risk",
            "usage": {"llm_calls": 3, "prompt_tokens": 100},
            "review_funnel": {"reviewer_candidates": 3, "final_findings": 2},
        }


class _FakeSemanticJudge:
    name = "fake-semantic-judge"

    def __init__(self):
        self.calls = 0

    def match(self, title, golden_comments, candidates):
        self.calls += 1
        return {
            "matches": [{
                "golden_index": 0,
                "candidate_index": 0,
                "confidence": 0.95,
                "reason": "Same null-dereference defect.",
            }],
            "usage": {"llm_calls": 1, "prompt_tokens": 40},
        }


class RealPRQualityRunnerTests(unittest.TestCase):
    @staticmethod
    def _case(case_id="case-1"):
        return {
            "id": case_id,
            "repository": "example/repo",
            "pull_request": 7,
            "split": "test",
            "title": "Fix null handling",
            "head_commit": "abc123",
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n+++ b/app.py\n"
                "@@ -0,0 +1 @@\n+value.use()\n"
            ),
            "golden_comments": [{
                "comment": "The value may be null before this dereference.",
                "severity": "high",
                "category": "bug",
            }],
        }

    def test_evaluates_final_findings_with_independent_semantic_matching(self):
        reviewer = _FakeQualityReviewer()
        judge = _FakeSemanticJudge()

        result = evaluate_quality_case(reviewer, judge, self._case())

        self.assertTrue(result["execution_success"])
        self.assertEqual(2, result["candidate_count"])
        self.assertEqual(1, len(result["matches"]))
        self.assertEqual(("example/repo", "abc123", 7), reviewer.calls[0])
        self.assertEqual(3, result["review_usage"]["llm_calls"])
        self.assertEqual(1, result["matcher_usage"]["llm_calls"])

    def test_checkpoint_resume_does_not_repeat_completed_prs(self):
        reviewer = _FakeQualityReviewer()
        judge = _FakeSemanticJudge()
        cases = [self._case("case-1"), self._case("case-2")]
        with tempfile.TemporaryDirectory() as directory:
            progress = str(Path(directory) / "case-results.jsonl")

            first = run_quality_evaluation_checkpointed(
                reviewer, judge, cases, progress
            )
            resumed = run_quality_evaluation_checkpointed(
                reviewer, judge, cases, progress, resume=True
            )

        self.assertEqual(2, len(reviewer.calls))
        self.assertEqual(2, judge.calls)
        self.assertEqual(first["metrics"], resumed["metrics"])
        self.assertEqual(0.5, first["metrics"]["precision"])
        self.assertEqual(1.0, first["metrics"]["recall"])


if __name__ == "__main__":
    unittest.main()
