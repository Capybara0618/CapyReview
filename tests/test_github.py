import hashlib
import hmac
import base64
import unittest
from unittest.mock import patch

import capyreview.github as github_module
from capyreview.github import GitHubClient, verify_signature


class GitHubSignatureTests(unittest.TestCase):
    def test_signature_verification(self):
        body = b'{"ok":true}'
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("secret", body, signature))
        self.assertFalse(verify_signature("wrong", body, signature))

    def test_client_surface_is_limited_to_review_integration(self):
        client = GitHubClient("token")
        self.assertFalse(hasattr(github_module, "GitHubAppAuthenticator"))
        for obsolete in (
            "create_branch", "commit_file", "create_atomic_commit",
            "create_draft_pull_request", "download_archive", "get_file",
        ):
            self.assertFalse(hasattr(client, obsolete), obsolete)

    def test_upsert_comment_updates_an_existing_marker(self):
        client = GitHubClient("token")
        with patch.object(client, "_json") as request:
            request.side_effect = [
                [{"url": "https://api.github.test/comments/1", "body": "<!-- marker -->\nold"}],
                {},
            ]
            client.upsert_comment(
                "https://api.github.test/issues/1", "new", "<!-- marker -->"
            )
        self.assertEqual(
            ("PATCH", "https://api.github.test/comments/1", {"body": "<!-- marker -->\nnew"}),
            request.call_args_list[1].args,
        )

    def test_read_file_context_is_pinned_to_commit_and_bounded_to_line_window(self):
        client = GitHubClient("token")
        source = "first\nsecond\nthird\nfourth\nfifth\n"
        with patch.object(client, "_json") as request:
            request.return_value = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(source.encode("utf-8")).decode("ascii"),
            }
            context = client.read_file_context(
                "org/repo", "src/app.py", "abc123", line=3, radius=1
            )

        self.assertEqual(
            ("GET", "https://api.github.com/repos/org/repo/contents/src/app.py?ref=abc123"),
            request.call_args.args,
        )
        self.assertEqual(2, context["start_line"])
        self.assertEqual(4, context["end_line"])
        self.assertEqual("second\nthird\nfourth", context["content"])

    def test_read_file_context_rejects_unpinned_or_unsafe_paths(self):
        client = GitHubClient("token")
        for path, ref in (("../secret", "abc123"), ("/etc/passwd", "abc123"), ("app.py", "")):
            with self.subTest(path=path, ref=ref):
                with self.assertRaises(ValueError):
                    client.read_file_context("org/repo", path, ref, line=1)


if __name__ == "__main__":
    unittest.main()
