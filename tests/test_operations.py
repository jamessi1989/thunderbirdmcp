import concurrent.futures
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thunderbird_mcp import operations, policy


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "operations.sqlite"
        env = {name: value for name, value in os.environ.items() if not name.startswith("TB_MCP_")}
        env["TB_OPERATIONS_DB"] = str(self.database)
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def create(self, **kwargs):
        return operations.create_operation("organize_emails", account="a", **kwargs)

    def test_private_durable_journal_and_redacted_outcomes(self):
        operation = self.create(total=2, metadata={"password": "synthetic-secret", "reference": "synthetic"})
        operation_id = operation["operation_id"]
        self.assertTrue(operations.claim_operation(operation_id)["claimed"])
        operations.update_operation(operation_id, completed=1, result={"moved": ["synthetic"]})
        operations.finish_operation(operation_id, status="partial", error=RuntimeError("synthetic-secret response"))
        saved = operations.get_operation(operation_id)
        self.assertEqual(saved["completed"], 1)
        self.assertEqual(saved["result"], {"moved": ["synthetic"]})
        self.assertEqual(saved["metadata"]["password"], "[redacted]")
        self.assertEqual(saved["error"], {"type": "RuntimeError"})
        self.assertNotIn(b"synthetic-secret", self.database.read_bytes())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)

    def test_claim_is_atomic_and_does_not_retry_uncertain_work(self):
        operation_id = self.create()["operation_id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: operations.claim_operation(operation_id), range(4)))
        self.assertEqual(sum(result["claimed"] for result in results), 1)
        operations.finish_operation(operation_id, status="uncertain")
        self.assertFalse(operations.claim_operation(operation_id)["claimed"])
        with self.assertRaises(ValueError):
            operations.finish_operation(operation_id, status="completed")

    def test_queued_and_running_cancellation_preserve_effects(self):
        queued = self.create()["operation_id"]
        cancelled = operations.request_cancel(queued)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(operations.claim_operation(queued)["claimed"])
        running = self.create(total=3)["operation_id"]
        operations.claim_operation(running)
        operations.update_operation(running, completed=1, result={"moved": ["first"]})
        requested = operations.request_cancel(running)
        self.assertEqual(requested["status"], "cancel_requested")
        self.assertFalse(requested["effects_reversed"])
        self.assertTrue(operations.cancellation_requested(running))
        operations.finish_operation(running, status="cancelled")
        self.assertEqual(operations.get_operation(running)["result"], {"moved": ["first"]})
        accepted = self.create()["operation_id"]
        operations.claim_operation(accepted)
        operations.finish_operation(accepted, status="completed", result={"accepted": True})
        final = operations.request_cancel(accepted)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], {"accepted": True})

    def test_scoped_idempotency_and_conflicting_plan(self):
        first = self.create(idempotency_key="synthetic", metadata={"ids": [1, 2]})
        second = self.create(idempotency_key="synthetic", metadata={"ids": [1, 2]})
        self.assertEqual(first["operation_id"], second["operation_id"])
        with self.assertRaises(ValueError):
            self.create(idempotency_key="synthetic", metadata={"ids": [1, 3]})
        third = operations.create_operation("organize_emails", account="b", idempotency_key="synthetic")
        self.assertNotEqual(first["operation_id"], third["operation_id"])

    def test_principal_account_scopes_isolate_history_and_mutation(self):
        with policy.context(policy.Policy(principal="alice", accounts={"a"})):
            alice = self.create()["operation_id"]
        with policy.context(policy.Policy(principal="bob", accounts={"a"})):
            self.assertEqual(operations.list_operations()["count"], 0)
            with self.assertRaises(policy.PolicyError):
                operations.get_operation(alice)
            with self.assertRaises(policy.PolicyError):
                operations.request_cancel(alice)
        with policy.context(policy.Policy(principal="alice", accounts={"b"})):
            with self.assertRaises(policy.PolicyError):
                operations.get_operation(alice)

    def test_policy_is_rechecked_before_claim_and_effects(self):
        operation_id = self.create()["operation_id"]
        with patch.dict(os.environ, TB_MCP_PROFILE="read-only"):
            with self.assertRaises(policy.PolicyError):
                operations.claim_operation(operation_id)
        self.assertTrue(operations.claim_operation(operation_id)["claimed"])
        with patch.dict(os.environ, TB_MCP_PROFILE="read-only"):
            with self.assertRaises(policy.PolicyError):
                operations.update_operation(operation_id, completed=1)
            # A worker may still record that it stopped; the true outcome survives.
            operations.finish_operation(operation_id, status="failed", error=policy.PolicyError("revoked"))

    def test_bounded_pagination_progress_and_validation(self):
        for i in range(5):
            self.create(metadata={"index": i})
        seen, cursor = [], None
        while True:
            page = operations.list_operations(limit=2, before=cursor)
            seen.extend(item["operation_id"] for item in page["operations"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(set(seen)), 5)
        for limit in (0, 101, True):
            with self.assertRaises(ValueError):
                operations.list_operations(limit=limit)
        with self.assertRaises(ValueError):
            operations.list_operations(before="bad cursor")
        operation_id = self.create(total=2)["operation_id"]
        with self.assertRaises(ValueError):
            operations.update_operation(operation_id, completed=1)
        operations.claim_operation(operation_id)
        operations.update_operation(operation_id, completed=1)
        for progress in (0, 3):
            with self.assertRaises(ValueError):
                operations.update_operation(operation_id, completed=progress)
        with self.assertRaises(ValueError):
            self.create(metadata={"unbounded": "x" * operations.MAX_JSON_BYTES})


if __name__ == "__main__":
    unittest.main()
