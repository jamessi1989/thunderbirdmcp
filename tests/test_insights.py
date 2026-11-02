import unittest
from unittest.mock import patch
from thunderbird_mcp import insights
from test_index import MailFixture, mail


class EvidenceTests(unittest.TestCase):
    def test_conflicting_headers_preserve_provenance_and_no_trust_verdict(self):
        raw = b"Authentication-Results: mx.example.test; spf=pass; dkim=pass; dmarc=pass\r\nAuthentication-Results: other.example.test; spf=fail\r\n\r\nbody"
        with patch.object(insights, "original", return_value=raw):
            result = insights.authentication_evidence("synthetic")
        self.assertEqual(result["summary"], {"spf": "conflicting", "dkim": "pass", "dmarc": "pass"})
        self.assertFalse(result["independently_verified"])
        self.assertEqual(result["evidence"][0]["provenance"], "unverified_message_header")

    def test_comments_arc_and_signature_are_not_verification(self):
        raw = b'Authentication-Results: mx.test; none (dkim=pass; spf=pass)\r\nARC-Authentication-Results: i=1; mx.test; dkim=pass\r\nDKIM-Signature: fake\r\n\r\n'
        with patch.object(insights, "original", return_value=raw):
            result = insights.authentication_evidence("synthetic")
        self.assertEqual(set(result["summary"].values()), {"unknown"})
        self.assertTrue(result["dkim_signature_present"])

    def test_evidence_is_bounded(self):
        raw = b"".join(b"Authentication-Results: mx.test; spf=pass\r\n" for _ in range(30)) + b"\r\n"
        with patch.object(insights, "original", return_value=raw):
            result = insights.authentication_evidence("synthetic")
        self.assertEqual(len(result["evidence"]), 20)
        self.assertTrue(result["truncated"])


class FollowUpTests(MailFixture):
    def test_header_response_excludes_candidate_but_same_subject_does_not(self):
        self.folder("Sent", [mail("answered", subject="Question", **{"From": "me@example.test", "To": "you@example.test"}),
                             mail("pending", subject="Question", **{"From": "me@example.test", "To": "you@example.test"})])
        self.folder("INBOX", [mail("reply", subject="Changed", **{"From": "you@example.test", "To": "me@example.test", "In-Reply-To": "<answered@example.test>"}),
                              mail("unrelated", subject="Question", **{"From": "you@example.test", "To": "me@example.test"})])
        self.refresh()
        folder = next(r["folder"] for r in self.rows() if r["message_id"] == "<pending@example.test>")
        result = insights.follow_up_candidates(folders=[folder], own_addresses=["me@example.test"],
                                               min_age_days=0, now=2_000_000_000, db_path=self.db)
        self.assertEqual([r["message_id"] for r in result["candidates"]], ["<pending@example.test>"])
        self.assertEqual(result["exclusions"]["has_header_linked_response"], 1)
        self.assertFalse(result["automatic_actions"])


if __name__ == "__main__":
    unittest.main()
