"""Provider adapter contract tests use synthetic mail and mocked HTTPS only."""

import base64
import io
import json
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from thunderbird_mcp import oauth, outgoing, policy, providers


class Response(io.BytesIO):
    status = 200


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "profile").mkdir()
        self.environment = patch.dict(os.environ, {
            "TB_PROFILE": str(self.root / "profile"), "TB_OUTBOX_DB": str(self.root / "outbox.sqlite"),
            "TB_OPERATIONS_DB": str(self.root / "operations.sqlite"),
            "TB_OAUTH_LOCK_DIR": str(self.root / "locks"), "TB_FROM": "me@example.test",
            "TB_IMAP_HOST": "imap.gmail.com", "TB_IMAP_USER": "me@example.test",
            "TB_SMTP_HOST": "smtp.gmail.com", "TB_SMTP_USER": "me@example.test",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.token = patch.object(oauth, "access_token", return_value="synthetic-access-token")
        self.token.start()
        self.addCleanup(self.token.stop)
        self.message = EmailMessage()
        for name, value in (("From", "sender@example.test"), ("To", "me@example.test"),
                            ("Subject", "Provider fixture"), ("Message-ID", "<provider@example.test>")):
            self.message[name] = value
        self.message.set_content("Synthetic provider body")

    def http(self, *responses):
        def respond(*args, **kwargs):
            value = next(iterator)
            if isinstance(value, Exception):
                raise value
            return Response(value if isinstance(value, bytes) else json.dumps(value).encode())
        iterator = iter(responses)
        opener = Mock()
        opener.open.side_effect = respond
        return opener

    def reference(self, provider, identifier="message1"):
        return providers._locator(providers.config(provider), identifier).reference

    def draft(self, **kwargs):
        return outgoing.prepare_draft(["recipient@example.test"], "Reviewed subject", "Reviewed body", **kwargs)

    def test_gmail_search_has_account_bound_refs_and_read_only_requests(self):
        details = {"id": "message1", "labelIds": ["INBOX", "UNREAD"], "snippet": "Synthetic",
                   "payload": {"headers": [{"name": name, "value": str(value)} for name, value in self.message.items()]}}
        opener = self.http({"messages": [{"id": "message1"}], "nextPageToken": "next", "resultSizeEstimate": 3}, details)
        with patch.object(providers, "build_opener", return_value=opener):
            result = providers.search_email("gmail", "from:sender@example.test", limit=1)
        self.assertEqual(result["results"][0]["message_id"], "<provider@example.test>")
        self.assertTrue(result["results"][0]["unread"])
        self.assertEqual(providers.parse_reference(result["results"][0]["reference"]).account, "environment")
        self.assertEqual(providers._continuation(providers.config("gmail"), "from:sender@example.test", result["next_cursor"]), "next")
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertEqual(request.method, "GET")
            self.assertIn("/users/me%40example.test/", request.full_url)
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic-access-token")

    def test_graph_immutable_ids_and_pagination_cannot_leave_account(self):
        opener = self.http({"value": [{"id": "immutable/id+", "subject": "Graph fixture", "isRead": False}]})
        with patch.object(providers, "build_opener", return_value=opener):
            result = providers.search_email("graph", "review")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.headers["Prefer"], 'IdType="ImmutableId"')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["$search"], ['"review"'])
        self.assertEqual(providers.parse_reference(result["results"][0]["reference"]).message_id, "immutable/id+")
        settings = providers.config("graph")
        for url in ("https://attacker.example.test/token", "https://graph.microsoft.com/v1.0/users/other%40example.test/messages"):
            cursor = providers._cursor(settings, "review", url)
            with patch.object(providers, "build_opener") as network, self.assertRaises(ValueError):
                providers.search_email("graph", "review", cursor=cursor)
            network.assert_not_called()
        with patch.object(providers, "build_opener") as network, self.assertRaises(ValueError):
            providers._request(settings, providers._base(settings) + "/%2E%2E/other/messages")
        network.assert_not_called()
        with self.assertRaises(ValueError):
            providers._continuation(settings, "changed query", providers._cursor(settings, "review", "next"))

    def test_both_provider_reads_parse_original_mime_without_mutations(self):
        raw = self.message.as_bytes()
        for provider in ("gmail", "graph"):
            value = {"id": "message1", "raw": base64.urlsafe_b64encode(raw).decode()} if provider == "gmail" else raw
            opener = self.http(value)
            with patch.object(providers, "build_opener", return_value=opener):
                result = providers.get_email(self.reference(provider), max_body_chars=10)
            self.assertEqual(result["body"], "Synthetic ")
            self.assertTrue(result["body_truncated"])
            self.assertTrue(result["headers_complete"])
            self.assertEqual(opener.open.call_args.args[0].method, "GET")

    def test_provider_triage_uses_labels_or_graph_fields_and_policy(self):
        opener = self.http({"labelIds": ["UNREAD", "STARRED"]})
        with patch.object(providers, "build_opener", return_value=opener):
            providers.update_labels(self.reference("gmail"), unread=True, flagged=True, remove=["INBOX"])
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(payload, {"addLabelIds": ["STARRED", "UNREAD"], "removeLabelIds": ["INBOX"]})
        opener = self.http({"categories": ["Reviewed"]})
        with patch.object(providers, "build_opener", return_value=opener):
            providers.update_labels(self.reference("graph"), unread=False, flagged=False, categories=["Reviewed"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(json.loads(request.data), {"isRead": True, "flag": {"flagStatus": "notFlagged"}, "categories": ["Reviewed"]})
        with patch.dict(os.environ, TB_MCP_PROFILE="read-only"), patch.object(providers, "build_opener") as network:
            with self.assertRaises(policy.PolicyError):
                providers.update_labels(self.reference("gmail"), unread=False)
        network.assert_not_called()

    def test_native_conversations_are_bounded_and_describe_provider_grouping(self):
        headers = [{"name": name, "value": str(value)} for name, value in self.message.items()]
        opener = self.http({"threadId": "conversation1"}, {"messages": [
            {"id": "message1", "payload": {"headers": headers}},
            {"id": "message2", "payload": {"headers": headers}}]})
        with patch.object(providers, "build_opener", return_value=opener):
            result = providers.get_thread(self.reference("gmail"), limit=1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["match_method"], "provider_conversation")
        opener = self.http({"conversationId": "quoted'conversation"}, {"value": [
            {"id": "message1", "internetMessageHeaders": headers, "subject": "Graph fixture"}]})
        with patch.object(providers, "build_opener", return_value=opener):
            result = providers.get_thread(self.reference("graph"))
        self.assertEqual(result["count"], 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["$filter"], ["conversationId eq 'quoted''conversation'"])
        self.assertEqual(request.headers["Prefer"], 'IdType="ImmutableId"')

    def test_api_send_and_smtp_share_claim_and_preserve_reviewed_bcc(self):
        draft = self.draft(bcc=["private@example.test"])
        opener = self.http({"id": "accepted1"})
        with patch.object(providers, "build_opener", return_value=opener), patch.object(outgoing, "smtp_session") as smtp:
            result = providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"])
            repeat = providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"])
            smtp_repeat = outgoing.send_draft(draft["draft_id"])
        self.assertTrue(result["sent"])
        self.assertTrue(repeat["duplicate_prevented"])
        self.assertTrue(smtp_repeat["duplicate_prevented"])
        self.assertEqual(opener.open.call_count, 1)
        smtp.assert_not_called()
        raw = providers._decode_mime(json.loads(opener.open.call_args.args[0].data)["raw"])
        self.assertIn(b"Bcc: private@example.test", raw)
        self.assertIn(b"Reviewed body", raw)

    def test_graph_mime_handoff_is_unsent_and_locks_local_snapshot(self):
        draft = self.draft()
        opener = self.http({"id": "draft1"})
        with patch.object(providers, "build_opener", return_value=opener):
            result = providers.save_local_draft(draft["draft_id"], "graph", draft["revision"])
            repeat = providers.save_local_draft(draft["draft_id"], "graph", draft["revision"])
        self.assertFalse(result["sent"])
        self.assertTrue(result["saved"])
        self.assertEqual(result["status"], "saved_to_provider")
        self.assertTrue(repeat["duplicate_prevented"])
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/messages"))
        self.assertEqual(request.headers["Content-type"], "text/plain")
        self.assertIn(b"Reviewed body", base64.b64decode(request.data))
        with patch.object(outgoing, "smtp_session") as smtp:
            self.assertTrue(outgoing.send_draft(draft["draft_id"])["duplicate_prevented"])
        smtp.assert_not_called()

    def test_ambiguous_api_delivery_and_acceptance_persistence_failure_cannot_retry(self):
        draft = self.draft()
        opener = self.http(TimeoutError("synthetic-access-token"))
        with patch.object(providers, "build_opener", return_value=opener):
            failed = providers.send_local_draft(draft["draft_id"], "graph", draft["revision"])
            repeated = providers.send_local_draft(draft["draft_id"], "graph", draft["revision"], retry_failed=True)
        self.assertEqual(failed["status"], "uncertain")
        self.assertNotIn("synthetic-access-token", json.dumps(failed))
        self.assertTrue(repeated["duplicate_prevented"])
        self.assertEqual(opener.open.call_count, 1)
        draft = self.draft()
        with patch.object(providers, "_request", return_value={"id": "accepted"}), patch.object(outgoing, "_record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"])
        self.assertEqual(outgoing.get_draft(draft["draft_id"])["status"], "sending")
        with patch.object(providers, "_request") as request:
            self.assertTrue(providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"])["duplicate_prevented"])
        request.assert_not_called()

    def test_definitive_rejection_requires_explicit_retry_and_same_revision(self):
        draft = self.draft()
        opener = self.http(HTTPError("https://gmail.googleapis.com", 403, "private provider response", {}, None), {"id": "sent1"})
        with patch.object(providers, "build_opener", return_value=opener):
            failed = providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"])
            self.assertEqual(failed["status"], "failed")
            self.assertTrue(providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"])["duplicate_prevented"])
            accepted = providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"], retry_failed=True)
        self.assertTrue(accepted["sent"])
        self.assertEqual(opener.open.call_count, 2)
        self.assertNotIn("private provider response", json.dumps(failed))

    def test_policy_stale_revision_size_and_changed_identity_block_before_network(self):
        draft = self.draft()
        with patch.object(providers, "build_opener") as network:
            with patch.dict(os.environ, TB_MCP_PROFILE="draft-only"), self.assertRaises(policy.PolicyError):
                providers.send_local_draft(draft["draft_id"], "gmail", draft["revision"])
            with self.assertRaises(ValueError):
                providers.send_local_draft(draft["draft_id"], "gmail", 9)
            with patch.object(providers, "MAX_GRAPH_MIME_BYTES", 10), self.assertRaises(ValueError):
                providers.send_local_draft(draft["draft_id"], "graph", draft["revision"])
            reference = self.reference("gmail")
            with patch.dict(os.environ, TB_IMAP_USER="other@example.test"), self.assertRaises(LookupError):
                providers.get_email(reference)
        network.assert_not_called()
        self.assertEqual(outgoing.get_draft(draft["draft_id"])["status"], "prepared")

    def test_cancellation_during_api_auth_stops_before_mutating_http_request(self):
        from thunderbird_mcp import operations
        for handoff in (False, True):
            with self.subTest(handoff=handoff):
                draft = outgoing.prepare_draft(["person@example.test"], "Synthetic", "Reviewed body")
                def token_then_cancel(*args):
                    active = outgoing.get_draft(draft["draft_id"])
                    operations.request_cancel(active["operation_id"])
                    return "synthetic-token"
                with patch.object(oauth, "access_token", side_effect=token_then_cancel), \
                        patch.object(providers, "build_opener") as opener:
                    method = providers.save_local_draft if handoff else providers.send_local_draft
                    result = method(draft["draft_id"], "gmail", revision=1)
                    repeated = method(draft["draft_id"], "gmail", revision=1)
                self.assertEqual(result["status"], "cancelled")
                self.assertFalse(result["sent"])
                self.assertFalse(result["saved"])
                self.assertTrue(repeated["duplicate_prevented"])
                opener.return_value.open.assert_not_called()
                self.assertEqual(operations.get_operation(result["operation_id"])["status"], "cancelled")

    def test_api_keyring_keys_are_separate_by_provider_account_user_and_tenant(self):
        gmail = providers.config("gmail")
        graph = providers.config("graph")
        keys = {oauth._key(gmail, "gmail"), oauth._key(graph, "graph"), oauth._key(gmail, "imap"),
                oauth._key({**gmail, "account_id": "other"}, "gmail"),
                oauth._key({**gmail, "gmail_user": "other@example.test"}, "gmail"),
                oauth._key({**graph, "graph_tenant": "tenant2"}, "graph")}
        self.assertEqual(len(keys), 6)
        secrets = {}
        with patch.object(oauth.keyring, "get_password", side_effect=lambda service, name: secrets.get((service, name))), \
             patch.object(oauth.keyring, "set_password", side_effect=lambda service, name, value: secrets.__setitem__((service, name), value)):
            imported = oauth.store_token(gmail, "gmail", {"access_token": "synthetic-api-token", "expires_in": 3600})
            self.assertTrue(imported["credential_available"])
            self.assertFalse(oauth.token_status(gmail, "imap")["credential_available"])
            self.assertFalse(oauth.token_status(graph, "graph")["credential_available"])
        self.assertNotIn("synthetic-api-token", json.dumps(imported))


if __name__ == "__main__":
    unittest.main()
