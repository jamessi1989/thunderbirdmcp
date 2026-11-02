"""OAuth consent contract tests: fake loopback callback and HTTPS token exchange."""

import base64
import hashlib
import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from thunderbird_mcp import enrollment, oauth, policy


class EnrollmentTests(unittest.TestCase):
    def test_granular_api_scopes_are_not_substituted_for_imap(self):
        gmail = enrollment.guide("gmail")
        self.assertEqual(gmail["scopes"], ["https://www.googleapis.com/auth/gmail.readonly"])
        self.assertEqual(enrollment.guide("gmail", protocol="imap")["scopes"], ["https://mail.google.com/"])
        graph = enrollment.guide("graph", mode="draft-only")
        self.assertIn("https://graph.microsoft.com/Mail.ReadWrite", graph["scopes"])
        self.assertNotIn("https://graph.microsoft.com/Mail.Send", graph["scopes"])
        self.assertIn("offline_access", graph["scopes"])
        self.assertIn("Gmail compose permission can send", " ".join(gmail["notes"]))

    def pending(self):
        return enrollment._pending(enrollment._settings("gmail", None, "read-only", "common"),
                                   "synthetic-client", "http://127.0.0.1:12345/oauth/callback")

    def test_authorization_uses_random_state_and_s256_pkce(self):
        first, second = self.pending(), self.pending()
        params = parse_qs(urlsplit(first.authorization_url()).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(first.verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], [expected])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertNotEqual(first.state, second.state)
        self.assertNotEqual(first.verifier, second.verifier)
        self.assertNotIn(first.verifier, repr(first))
        self.assertNotIn(first.verifier, first.authorization_url())

    def callback(self, pending, result, fields, host="127.0.0.1:12345"):
        handler = object.__new__(enrollment._callback_handler(pending, result))
        handler.path = "/oauth/callback?" + urlencode(fields)
        handler.headers = {"Host": host}
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()
        return handler

    def test_callback_rejects_wrong_state_host_and_repeated_state_without_logging_code(self):
        pending, result = self.pending(), {}
        for fields, host in (({"state": "wrong", "code": "private-code"}, "127.0.0.1:12345"),
                             ({"state": pending.state, "code": "private-code"}, "attacker.example.test"),
                             ({"state": "ñ", "code": "private-code"}, "127.0.0.1:12345")):
            handler = self.callback(pending, result, fields, host)
            handler.send_response.assert_called_once_with(400)
            self.assertEqual(result, {})
            self.assertNotIn(b"private-code", handler.wfile.getvalue())
        handler = self.callback(pending, result, {"state": pending.state, "code": "private-code"})
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(result, {"code": "private-code"})
        self.assertNotIn(b"private-code", handler.wfile.getvalue())

    def test_exchange_includes_verifier_and_redacts_provider_failures(self):
        pending = self.pending()
        response = io.BytesIO(json.dumps({"access_token": "private-access", "refresh_token": "private-refresh",
                                         "expires_in": 3600, "token_type": "Bearer", "unexpected": "ignored"}).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch.object(enrollment, "build_opener", return_value=opener):
            token = enrollment._exchange(pending, "private-code")
        params = parse_qs(opener.open.call_args.args[0].data.decode())
        self.assertEqual(params["code_verifier"], [pending.verifier])
        self.assertEqual(params["code"], ["private-code"])
        self.assertEqual(token["refresh_token"], "private-refresh")
        self.assertNotIn("unexpected", token)
        opener.open.side_effect = OSError("provider leaked private-code")
        with patch.object(enrollment, "build_opener", return_value=opener):
            with self.assertRaises(RuntimeError) as failure:
                enrollment._exchange(pending, "private-code")
        self.assertNotIn("private-code", str(failure.exception))

    def test_consent_denial_returns_only_redacted_error(self):
        pending, result = self.pending(), {}
        handler = self.callback(pending, result, {"state": pending.state, "error": "access_denied",
                                                "error_description": "private provider content"})
        self.assertIn("declined", result["error"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn(b"private", handler.wfile.getvalue())

    def test_read_only_policy_blocks_enrollment_before_listener(self):
        with patch.dict(os.environ, {"TB_MCP_PROFILE": "read-only"}, clear=True), \
             patch("thunderbird_mcp.providers.config", return_value={"account_id": "synthetic"}), \
             patch.object(enrollment, "HTTPServer") as server:
            with self.assertRaises(policy.PolicyError):
                enrollment.enroll(provider="gmail", client_id="client")
        server.assert_not_called()

    def test_complete_flow_uses_loopback_and_returns_no_codes_or_tokens(self):
        ready = []
        test = self
        class FakeServer:
            def __init__(self, address, handler):
                test.assertEqual(address, ("127.0.0.1", 0))
                self.server_port = 12345
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def handle_request(self):
                state = parse_qs(urlsplit(ready[0][0]).query)["state"][0]
                handler = object.__new__(self.RequestHandlerClass)
                handler.path = "/oauth/callback?" + urlencode({"state": state, "code": "private-code"})
                handler.headers = {"Host": "127.0.0.1:12345"}
                handler.wfile = io.BytesIO()
                handler.send_response = handler.send_header = handler.end_headers = Mock()
                handler.do_GET()
        config = {"account_id": "synthetic", "gmail_host": "gmail.googleapis.com", "gmail_user": "me@example.test"}
        with patch.dict(os.environ, {}, clear=True), patch("thunderbird_mcp.providers.config", return_value=config), \
             patch.object(enrollment, "HTTPServer", FakeServer), patch.object(enrollment.webbrowser, "open") as browser, \
             patch.object(enrollment, "_exchange", return_value={"access_token": "private-access", "scope": "requested"}) as exchange, \
             patch.object(oauth, "store_token", return_value={"stored": True}) as store:
            result = enrollment.enroll(provider="gmail", client_id="synthetic-client", open_browser=False,
                                       on_ready=lambda url, redirect: ready.append((url, redirect)))
        browser.assert_not_called()
        self.assertEqual(ready[0][1], "http://127.0.0.1:12345/oauth/callback")
        self.assertEqual(exchange.call_args.args[1], "private-code")
        self.assertEqual(store.call_args.args[:2], (config, "gmail"))
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
