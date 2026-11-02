"""Check metric correctness so the public benchmark cannot award fake credit."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "benchmark_search", Path(__file__).resolve().parents[1] / "scripts" / "benchmark_search.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class BenchmarkScoreTests(unittest.TestCase):
    def test_perfect_results_and_negative_query(self):
        rankings = {case["id"]: case["expected"] for case in benchmark.cases()}
        report = benchmark.score(rankings)
        self.assertEqual(report["recall_at_k"], 1)
        self.assertEqual(report["mrr_at_k"], 1)
        self.assertEqual(report["negative_cases_passed"], 1)

    def test_duplicate_results_do_not_inflate_recall_and_topk_is_real(self):
        rankings = {case["id"]: case["expected"] for case in benchmark.cases()}
        rankings["thread-individual"] = ["<thread-root@benchmark.example.test>"] * 5
        rankings["body-word"] = ["<irrelevant@example.test>"] * 5 + ["<security@benchmark.example.test>"]
        rankings["empty-result"] = ["<irrelevant@example.test>"]
        report = benchmark.score(rankings)
        details = {case["id"]: case for case in report["details"]}
        self.assertEqual(details["thread-individual"]["recall"], .5)
        self.assertEqual(details["body-word"]["recall"], 0)
        self.assertEqual(report["negative_cases_passed"], 0)
        self.assertLess(report["recall_at_k"], 1)

    def test_missing_case_is_not_silently_excluded(self):
        with self.assertRaisesRegex(ValueError, "Missing ranked results"):
            benchmark.score({})
