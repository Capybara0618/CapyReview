from types import SimpleNamespace
import unittest

from capyreview.mcp import (
    GitHubMcpClient, GitHubMcpToolError, GitHubMcpToolProvider,
    ReviewToolContext,
)


class FakeMcpClient:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        response = self.responses.get(name, {})
        return response() if callable(response) else response


class GitHubMcpToolProviderTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeMcpClient()
        self.provider = GitHubMcpToolProvider(self.client)

    def context(self, domains):
        return ReviewToolContext(
            repository="org/repo",
            head_commit="abc123",
            pull_request=7,
            files=("src/app.py",),
            domains=tuple(domains),
        )

    def test_security_profile_contains_only_four_read_only_evidence_tools(self):
        registry = self.provider.registry(self.context(("security",)))

        self.assertEqual([
            "read_code_context",
            "read_code_scanning_findings",
            "read_file_history",
            "search_repository",
        ], registry.names())

    def test_correctness_profile_contains_ci_instead_of_security_scanning(self):
        registry = self.provider.registry(
            self.context(("correctness", "reliability", "regression"))
        )

        self.assertEqual([
            "read_ci_failure",
            "read_code_context",
            "read_file_history",
            "search_repository",
        ], registry.names())

    def test_memory_and_legacy_helpers_are_not_agent_tools(self):
        names = set(self.provider.registry(self.context(("security",))).names())

        self.assertTrue({
            "recall_memory", "search_diff", "changed_line", "list_changed_files",
        }.isdisjoint(names))

    def test_model_facing_arguments_are_minimal(self):
        catalog = {
            item["name"]: set(item["parameters"]["properties"])
            for item in self.provider.registry(self.context(("security",))).catalog()
        }

        self.assertEqual({"path", "line"}, catalog["read_code_context"])
        self.assertEqual({"query", "path"}, catalog["search_repository"])
        self.assertEqual({"path"}, catalog["read_file_history"])
        self.assertEqual({"severity"}, catalog["read_code_scanning_findings"])

    def test_repository_identity_and_commit_are_injected_by_the_host(self):
        self.client.responses["get_file_contents"] = {
            "content": "\n".join("line %d" % index for index in range(1, 61))
        }

        result = self.provider.registry(self.context(("security",))).invoke(
            "read_code_context", {"path": "src/app.py", "line": 30}
        )

        self.assertEqual((
            "get_file_contents",
            {"owner": "org", "repo": "repo", "path": "src/app.py", "sha": "abc123"},
        ), self.client.calls[-1])
        self.assertEqual(10, result["start_line"])
        self.assertEqual(50, result["end_line"])
        self.assertIn("30: line 30", result["content"])

    def test_file_tools_reject_unassigned_or_unsafe_paths(self):
        registry = self.provider.registry(self.context(("security",)))

        with self.assertRaisesRegex(ValueError, "assigned file"):
            registry.invoke("read_code_context", {"path": "src/other.py", "line": 1})
        with self.assertRaisesRegex(ValueError, "safe repository-relative"):
            registry.invoke("read_file_history", {"path": "../secret"})

        self.assertEqual([], self.client.calls)

    def test_search_is_forced_into_the_current_repository(self):
        registry = self.provider.registry(self.context(("correctness",)))

        registry.invoke("search_repository", {"query": "parse_config", "path": "src"})

        self.assertEqual((
            "search_code",
            {
                "query": "parse_config repo:org/repo path:src",
                "perPage": 10,
                "fields": ["path", "sha", "html_url"],
            },
        ), self.client.calls[-1])
        with self.assertRaisesRegex(ValueError, "scope qualifiers"):
            registry.invoke("search_repository", {"query": "token repo:someone/else"})

    def test_history_is_pinned_to_the_review_head(self):
        registry = self.provider.registry(self.context(("correctness",)))

        registry.invoke("read_file_history", {"path": "src/app.py"})

        self.assertEqual((
            "list_commits",
            {
                "owner": "org", "repo": "repo", "path": "src/app.py",
                "sha": "abc123", "perPage": 5,
            },
        ), self.client.calls[-1])

    def test_ci_tool_reads_only_failed_run_logs(self):
        self.client.responses["actions_list"] = {
            "workflow_runs": [
                {"id": 10, "name": "lint", "conclusion": "failure"},
                {"id": 11, "name": "unit", "conclusion": "failure"},
            ]
        }
        self.client.responses["get_job_logs"] = {"logs": "failed assertion"}
        registry = self.provider.registry(self.context(("correctness",)))

        result = registry.invoke("read_ci_failure", {"check_name": "unit"})

        self.assertEqual("unit", result["run"]["name"])
        self.assertEqual((
            "get_job_logs",
            {
                "owner": "org", "repo": "repo", "run_id": 11,
                "failed_only": True, "return_content": True, "tail_lines": 80,
            },
        ), self.client.calls[-1])

    def test_security_findings_are_pinned_to_the_review_head(self):
        self.client.responses["list_code_scanning_alerts"] = {
            "alerts": [
                {"number": 1, "most_recent_instance": {"commit_sha": "old"}},
                {"number": 2, "most_recent_instance": {"commit_sha": "abc123"}},
            ]
        }
        registry = self.provider.registry(self.context(("security",)))

        result = registry.invoke(
            "read_code_scanning_findings", {"severity": "high"}
        )

        self.assertEqual((
            "list_code_scanning_alerts",
            {
                "owner": "org", "repo": "repo", "state": "open",
                "severity": "high", "perPage": 10,
            },
        ), self.client.calls[-1])
        self.assertEqual([2], [item["number"] for item in result["alerts"]])


class GitHubMcpClientTests(unittest.TestCase):
    def test_requires_the_existing_github_token(self):
        with self.assertRaisesRegex(ValueError, "GitHub token"):
            GitHubMcpClient("").call_tool("search_code", {"query": "x"})

    def test_prefers_structured_mcp_output(self):
        result = SimpleNamespace(
            is_error=False,
            structured_content={"items": [{"path": "src/app.py"}]},
            content=[],
        )

        self.assertEqual(
            {"items": [{"path": "src/app.py"}]},
            GitHubMcpClient._normalize_result("search_code", result),
        )

    def test_turns_mcp_tool_error_into_agent_observation_error(self):
        result = SimpleNamespace(
            is_error=True, structured_content=None,
            content=[SimpleNamespace(text="permission denied")],
        )

        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            GitHubMcpClient._normalize_result("search_code", result)

    def test_retries_one_transport_failure_but_not_a_tool_error(self):
        class FlakyClient(GitHubMcpClient):
            def __init__(self):
                super().__init__("token")
                self.calls = 0

            async def _call_tool(self, name, arguments):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("temporary disconnect")
                return {"ok": True}

        flaky = FlakyClient()
        self.assertEqual(
            {"ok": True}, flaky.call_tool("search_code", {"query": "x"})
        )
        self.assertEqual(2, flaky.calls)

        class RejectedClient(FlakyClient):
            async def _call_tool(self, name, arguments):
                self.calls += 1
                raise GitHubMcpToolError("permission denied")

        rejected = RejectedClient()
        with self.assertRaisesRegex(GitHubMcpToolError, "permission denied"):
            rejected.call_tool("search_code", {"query": "x"})
        self.assertEqual(1, rejected.calls)


if __name__ == "__main__":
    unittest.main()
