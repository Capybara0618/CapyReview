import os
import unittest

from capyreview.mcp import GitHubMcpClient, GitHubMcpToolProvider, ReviewToolContext


class RemoteGitHubMcpContractTests(unittest.TestCase):
    def test_reads_a_commit_pinned_file_window(self):
        token = os.getenv("CAPYREVIEW_TEST_GITHUB_TOKEN", "").strip()
        repository = os.getenv("CAPYREVIEW_TEST_GITHUB_REPOSITORY", "").strip()
        head = os.getenv("CAPYREVIEW_TEST_GITHUB_HEAD_COMMIT", "").strip()
        path = os.getenv("CAPYREVIEW_TEST_GITHUB_FILE", "").strip()
        if not all((token, repository, head, path)):
            self.skipTest("remote GitHub MCP test credentials are not configured")

        registry = GitHubMcpToolProvider(GitHubMcpClient(token)).registry(
            ReviewToolContext(
                repository=repository,
                head_commit=head,
                pull_request=None,
                files=(path,),
                domains=("correctness",),
            )
        )

        result = registry.invoke("read_code_context", {"path": path, "line": 1})

        self.assertEqual(path, result["path"])
        self.assertEqual(1, result["start_line"])
        self.assertTrue(result["content"].strip())


if __name__ == "__main__":
    unittest.main()
