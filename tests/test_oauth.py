"""OAuth tests use an in-memory keyring and fake protocol/HTTPS connections."""

import json
import os
import smtplib
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import MagicMock, patch

from thunderbird_mcp import imap, oauth, outgoing


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        profile = self.root / "profile"
        profile.mkdir()
        env = patch.dict(os.environ, {
            "TB_PROFILE": str(profile), "TB_OAUTH_LOCK_DIR": str(self.root / "locks"),
            "TB_OUTBOX_DB": str(self.root / "outbox.sqlite"),
            "TB_FROM": "me@example.test", "TB_IMAP_HOST": "imap.gmail.com", "TB_IMAP_USER": "me@example.test",
            "TB_SMTP_HOST": "smtp.gmail.com", "TB_SMTP_USER": "me@example.test",
            "TB_IMAP_AUTH": "oauth2", "TB_SMTP_AUTH": "oauth2",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.secrets = {}
        get = patch.object(oauth.keyring, "get_password", side_effect=lambda service, user: self.secrets.get((service, user)))
        put = patch.object(oauth.keyring, "set_password", side_effect=lambda service, user, value: self.secrets.__setitem__((service, user), value))
        get.start()
        put.start()
        self.addCleanup(get.stop)
        self.addCleanup(put.stop)
        self.config = outgoing.account_config()

    def token(self, protocol="imap", **extra):
        value = {"access_token": "synthetic-access-token", "expires_in": 3600, **extra}
        return oauth.store_token(self.config, protocol, value)

    def response(self, **values):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(values).encode()
        return opener

    def test_import_and_status_do_not_return_tokens_or_contact_provider(self):
        with patch.object(oauth, "build_opener") as http:
            imported = self.token(refresh_token="synthetic-refresh-token", client_id="registered-app")
            status = oauth.token_status(self.config)
        self.assertTrue(imported["stored"])
        self.assertTrue(status["credential_available"])
        self.assertTrue(status["refresh_available"])
        self.assertNotIn("synthetic", json.dumps(imported))
        self.assertNotIn("synthetic", json.dumps(status))
        http.assert_not_called()
        self.assertEqual(oauth.access_token(self.config, "imap"), "synthetic-access-token")

    def test_tokens_are_isolated_by_protocol_account_and_server(self):
        self.token()
        self.assertFalse(oauth.token_status(self.config, "smtp")["credential_available"])
        other = {**self.config, "account_id": "account2"}
        self.assertFalse(oauth.token_status(other)["credential_available"])
        changed = {**self.config, "imap_host": "other.example.test"}
        self.assertFalse(oauth.token_status(changed)["credential_available"])

    def test_draft_config_never_contains_oauth_secrets(self):
        self.token()
        self.token("smtp")
        with patch.dict(os.environ, {"TB_IMAP_OAUTH_CLIENT_SECRET": "synthetic-client-secret"}):
            outgoing.prepare_draft(["recipient@example.test"], "Synthetic", "Body")
        contents = outgoing.outbox_path().read_bytes().decode(errors="ignore")
        self.assertNotIn("synthetic-access-token", contents)
        self.assertNotIn("synthetic-client-secret", contents)

    def test_refresh_sends_form_over_https_and_preserves_rotated_token(self):
        oauth.store_token(self.config, "imap", {"access_token": "expired-token", "expires_at": 1,
                          "refresh_token": "old-refresh", "client_id": "registered-app", "client_secret": "client-secret"})
        opener = self.response(access_token="new-access", refresh_token="new-refresh", expires_in=3600, token_type="Bearer")
        with patch.object(oauth, "build_opener", return_value=opener):
            self.assertEqual(oauth.access_token(self.config, "imap"), "new-access")
            self.assertEqual(oauth.access_token(self.config, "imap"), "new-access")
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://oauth2.googleapis.com/token")
        form = parse_qs(request.data.decode())
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["old-refresh"])
        self.assertEqual(form["client_secret"], ["client-secret"])
        saved = json.loads(next(iter(self.secrets.values())))
        self.assertEqual(saved["refresh_token"], "new-refresh")

    def test_expired_environment_credentials_use_rotated_cached_token(self):
        variables = {"TB_IMAP_OAUTH_ACCESS_TOKEN": "expired-env", "TB_IMAP_OAUTH_EXPIRES_AT": "1",
                     "TB_IMAP_OAUTH_REFRESH_TOKEN": "old-env-refresh", "TB_IMAP_OAUTH_CLIENT_ID": "registered-app"}
        opener = self.response(access_token="refreshed-env", refresh_token="rotated-env-refresh", expires_in=3600)
        with patch.dict(os.environ, variables), patch.object(oauth, "build_opener", return_value=opener):
            self.assertEqual(oauth.access_token(self.config, "imap"), "refreshed-env")
            self.assertEqual(oauth.access_token(self.config, "imap"), "refreshed-env")
            self.assertEqual(opener.open.call_count, 1)
            with patch.dict(os.environ, {"TB_IMAP_OAUTH_ACCESS_TOKEN": "explicit-new-token",
                                         "TB_IMAP_OAUTH_EXPIRES_AT": str(time.time() + 3600)}):
                self.assertEqual(oauth.access_token(self.config, "imap"), "explicit-new-token")

    def test_refresh_failure_does_not_leak_provider_response_or_secrets(self):
        oauth.store_token(self.config, "imap", {"refresh_token": "private-refresh", "client_id": "registered-app"})
        opener = MagicMock()
        opener.open.side_effect = OSError("server echoed private-refresh")
        with patch.object(oauth, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "OAuth refresh failed") as failure:
                oauth.access_token(self.config, "imap")
        self.assertNotIn("private-refresh", str(failure.exception))

    def test_invalid_types_injection_plain_http_and_redirects_are_rejected(self):
        for fields in ({"token_type": []}, {"access_token": "a\x01auth=evil"}, {"expires_at": float("inf")},
                       {"expires_in": "NaN"}, {"token_url": "http://provider.example.test/token"},
                       {"token_url": "https://user:secret@provider.example.test/token"},
                       {"token_url": "https://provider.example.test/token?secret=value"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                oauth.store_token(self.config, "imap", {"access_token": "safe-token", **fields})
        with self.assertRaises(Exception):
            oauth._NoRedirect().redirect_request(MagicMock(full_url="https://provider.example.test/token"),
                                                None, 307, "redirect", {}, "https://different.example.test/token")
        self.assertEqual(self.secrets, {})

    def test_imap_xoauth2_payload_and_error_challenge_are_correct(self):
        self.token()
        with patch.object(imap.imaplib, "IMAP4_SSL") as constructor:
            connection = constructor.return_value
            def authenticate(mechanism, responder):
                self.assertEqual(mechanism, "XOAUTH2")
                self.assertEqual(responder(b""), b"user=me@example.test\x01auth=Bearer synthetic-access-token\x01\x01")
                self.assertEqual(responder(b'{"status":"401"}'), b"")
                return "OK", [b"Authenticated"]
            connection.authenticate.side_effect = authenticate
            connection.capability.return_value = "OK", [b"IMAP4rev1"]
            with imap.session(config=self.config):
                pass
            connection.login.assert_not_called()
            connection.logout.assert_called_once()

    def test_smtp_xoauth2_uses_empty_response_on_error_challenge(self):
        self.token("smtp")
        with patch.object(smtplib, "SMTP_SSL") as constructor:
            connection = constructor.return_value
            def authenticate(mechanism, responder):
                self.assertEqual(mechanism, "XOAUTH2")
                self.assertEqual(responder(), "user=me@example.test\x01auth=Bearer synthetic-access-token\x01\x01")
                self.assertEqual(responder(b'{"status":"401"}'), "")
                return 235, b"Authenticated"
            connection.auth.side_effect = authenticate
            with outgoing.smtp_session(self.config):
                pass
            connection.login.assert_not_called()
            connection.quit.assert_called_once()

    def test_health_checks_token_availability_without_refreshing(self):
        self.token("imap", expires_at=1, expires_in=1)
        self.token("smtp", refresh_token="refresh-secret", client_id="registered-app")
        with patch.object(oauth, "_refresh") as refresh:
            result = outgoing.health_check()
        self.assertFalse(result["ready"])
        self.assertFalse(result["imap"]["credential_available"])
        refresh.assert_not_called()
        self.assertNotIn("refresh-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
