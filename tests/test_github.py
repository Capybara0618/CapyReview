import hashlib
import hmac
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
            "read_file_context",
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


if __name__ == "__main__":
    unittest.main()
