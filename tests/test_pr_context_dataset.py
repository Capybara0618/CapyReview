import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from capyreview.pr_dataset import (
    PullRequestCandidate,
    candidate_pr_numbers,
    is_eligible_candidate,
    select_context_dataset,
    write_context_dataset,
)
from scripts.collect_real_pr_contexts import (
    is_skippable_diff_status,
    load_candidate_cache,
    save_candidate_cache,
)


def candidate(repository, number, tokens, diff=None, title="Fix request handling"):
    return PullRequestCandidate(
        repository=repository,
        pull_number=number,
        url="https://github.com/%s/pull/%d" % (repository, number),
        base_sha="base-%d" % number,
        head_sha="head-%d" % number,
        merged_at="2026-01-%02dT00:00:00Z" % min(number, 28),
        language="Python",
        license="MIT",
        title=title,
        author="contributor",
        diff=diff or (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1 +1 @@\n-old = 1\n+new = 2\n"
        ),
        estimated_diff_tokens=tokens,
    )


class PullRequestDatasetTests(unittest.TestCase):
    def test_unavailable_public_pr_diff_is_skipped_but_server_errors_are_not(self):
        self.assertTrue(is_skippable_diff_status(401))
        self.assertTrue(is_skippable_diff_status(404))
        self.assertTrue(is_skippable_diff_status(406))
        self.assertFalse(is_skippable_diff_status(500))

    def test_candidate_cache_round_trips_downloaded_diff(self):
        original = candidate("org/project", 7, 8_100)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.jsonl"
            save_candidate_cache(path, [original])
            restored = load_candidate_cache(path)
        self.assertEqual(1, len(restored))
        self.assertEqual(original.identity, restored[0].identity)
        self.assertEqual(original.diff, restored[0].diff)

    def test_graphql_prefilter_keeps_recent_prs_and_size_candidates(self):
        nodes = [
            {
                "number": number,
                "title": "Fix code path",
                "author": {"login": "developer"},
                "additions": changes,
                "deletions": 0,
            }
            for number, changes in (
                (1, 10), (2, 20), (3, 660), (4, 1_330), (5, 2_660)
            )
        ]
        numbers = candidate_pr_numbers(
            nodes, recent_count=2, stress_candidates_per_tier=1
        )
        self.assertEqual([1, 2], numbers[:2])
        self.assertEqual({1, 2, 3, 4, 5}, set(numbers))

    def test_eligibility_rejects_bots_and_non_source_only_changes(self):
        source = candidate("org/project", 1, 100)
        self.assertTrue(is_eligible_candidate(source))

        bot = candidate("org/project", 2, 100)
        bot.author = "dependabot[bot]"
        self.assertFalse(is_eligible_candidate(bot))

        docs = candidate(
            "org/project", 3, 100,
            diff=(
                "diff --git a/README.md b/README.md\n"
                "--- a/README.md\n+++ b/README.md\n"
                "@@ -1 +1 @@\n-old\n+new\n"
            ),
        )
        self.assertFalse(is_eligible_candidate(docs))

        lockfile = candidate(
            "org/project", 4, 100,
            diff=(
                "diff --git a/package-lock.json b/package-lock.json\n"
                "--- a/package-lock.json\n+++ b/package-lock.json\n"
                "@@ -1 +1 @@\n-{}\n+{\"lockfileVersion\": 3}\n"
            ),
        )
        self.assertFalse(is_eligible_candidate(lockfile))

    def test_selection_keeps_recent_natural_samples_and_balanced_size_tiers(self):
        candidates = []
        for repo_index, repository in enumerate(("org/a", "org/b"), 1):
            candidates.extend([
                candidate(repository, repo_index * 10 + 1, 500),
                candidate(repository, repo_index * 10 + 2, 700),
                candidate(repository, repo_index * 10 + 3, 8_100),
                candidate(repository, repo_index * 10 + 4, 16_100),
                candidate(repository, repo_index * 10 + 5, 32_100),
            ])

        selected = select_context_dataset(
            candidates,
            natural_per_repository=1,
            stress_per_tier=2,
        )

        natural = [item for item in selected if item.subset == "natural"]
        stress = [item for item in selected if item.subset == "stress"]
        self.assertEqual(2, len(natural))
        self.assertEqual(6, len(stress))
        self.assertEqual(
            {"8k": 2, "16k": 2, "32k": 2},
            {
                tier: sum(item.size_tier == tier for item in stress)
                for tier in ("8k", "16k", "32k")
            },
        )
        self.assertEqual(
            len(selected),
            len({(item.repository, item.pull_number) for item in selected}),
        )

    def test_selection_fails_when_a_stress_tier_is_missing(self):
        candidates = [
            candidate("org/a", 1, 500),
            candidate("org/a", 2, 8_000),
            candidate("org/a", 3, 16_000),
        ]
        with self.assertRaisesRegex(ValueError, "32k"):
            select_context_dataset(
                candidates,
                natural_per_repository=1,
                stress_per_tier=1,
            )

    def test_writer_keeps_provenance_in_manifest_and_diff_in_separate_file(self):
        selected = [
            candidate("org/project", 42, 8_100),
        ]
        selected[0].subset = "stress"
        selected[0].size_tier = "8k"

        with TemporaryDirectory() as directory:
            manifest_path = write_context_dataset(selected, Path(directory))
            rows = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(1, len(rows))
            self.assertNotIn("diff", rows[0])
            self.assertEqual("org/project", rows[0]["repository"])
            self.assertEqual("stress", rows[0]["subset"])
            self.assertEqual("8k", rows[0]["size_tier"])
            diff_path = Path(directory) / rows[0]["diff_path"]
            self.assertEqual(selected[0].diff, diff_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
