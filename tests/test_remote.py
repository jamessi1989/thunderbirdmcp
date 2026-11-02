import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from starlette.testclient import TestClient
from thunderbird_mcp import policy, remote, server


class RemoteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.auth = self.root / "auth.json"
        self.tokens = {"one": "one-" + "a" * 40, "two": "two-" + "b" * 40}
        value = {"allowed_hosts": ["testserver"], "allowed_origins": ["https://trusted.example.test"],
                 "principals": [{"name": name, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                 "accounts": [name], "profile": "read-only", "file_roots": []}
                                for name, token in self.tokens.items()]}
        self.auth.write_text(json.dumps(value))
        self.auth.chmod(0o600)
        self.mcp = policy.PolicyFastMCP("synthetic-http")
        @self.mcp.tool(capability="read")
        def scope_probe(account: str | None = None) -> dict:
            return {"account": account, "principal": policy.current().principal}
        @self.mcp.tool(capability="send")
        def send_probe(account: str | None = None) -> dict:
            raise AssertionError("Send must never execute")
        self.patch = patch.object(server, "mcp", self.mcp)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.client = TestClient(remote.create_app(self.auth))
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)

    def headers(self, name="one"):
        return {"Authorization": "Bearer " + self.tokens[name], "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25"}

    def call(self, name, args=None, principal="one", headers=None):
        return self.client.post("/mcp", headers=headers or self.headers(principal),
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args or {}}})

    def test_missing_token_origin_host_and_session_are_rejected(self):
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        headers = {**self.headers(), "Origin": "https://evil.example.test"}
        self.assertEqual(self.call("scope_probe", headers=headers).status_code, 403)
        headers = {**self.headers(), "Host": "evil.example.test"}
        self.assertEqual(self.call("scope_probe", headers=headers).status_code, 403)
        headers = {**self.headers(), "Mcp-Session-Id": "another-principal-session"}
        self.assertEqual(self.call("scope_probe", headers=headers).status_code, 400)

    def test_real_http_tool_requests_keep_principal_and_account_isolated(self):
        for principal in ("one", "two", "one"):
            response = self.call("scope_probe", principal=principal)
            self.assertEqual(response.status_code, 200)
            payload = response.json()["result"]
            result = payload.get("structuredContent") or json.loads(payload["content"][0]["text"])
            self.assertEqual(result, {"account": principal, "principal": principal})
            self.assertNotIn("mcp-session-id", response.headers)
        denied = self.call("scope_probe", {"account": "two"}).json()
        self.assertTrue(denied["result"]["isError"])

    def test_tool_discovery_and_hidden_dispatch_both_deny_send(self):
        response = self.client.post("/mcp", headers=self.headers(),
                                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([v["name"] for v in response.json()["result"]["tools"]], ["scope_probe"])
        denied = self.call("send_probe")
        self.assertTrue(denied.json()["result"]["isError"])

    def test_file_roots_are_denied_by_default_and_auth_file_is_private(self):
        values = remote.load_auth(self.auth)
        with policy.context(values["principals"][0]["policy"]), self.assertRaises(PermissionError):
            policy.require("file-write", account="one", paths=[self.root / "output"])
        if os.name != "nt":
            self.auth.chmod(0o644)
            with self.assertRaises(ValueError):
                remote.load_auth(self.auth)

    def test_chunked_request_body_cannot_exceed_limit(self):
        response = self.client.post("/mcp", headers=self.headers(), content=b"x" * (1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
