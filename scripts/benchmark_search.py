#!/usr/bin/env python3
"""Reproducible retrieval check using generated mail, never the user's index.

Export the corpus and query labels to compare another search implementation.
Its ranked Message-ID results can be scored with --score-results results.json.
This is a small regression benchmark, not a claim about real-world accuracy.
"""

from __future__ import annotations

import argparse
import json
import mailbox
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time
from email.message import EmailMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CORPUS_VERSION = 1
DATE = "Mon, 02 Nov 2026 09:00:00 +0000"


def message(name: str, subject: str, body: str, **headers: str) -> EmailMessage:
    mail = EmailMessage()
    for key, value in {"From": "Sender <sender@example.test>",
                       "To": "Reader <reader@example.test>", "Date": DATE,
                       "Message-ID": f"<{name}@benchmark.example.test>",
                       "Subject": subject, **headers}.items():
        mail[key] = value
    mail.set_content(body)
    return mail


def corpus() -> dict[str, list[EmailMessage]]:
    contract = message("contract", "Project paperwork", "Please find the signed document attached.")
    contract.add_attachment("The Zephyrlattice agreement expires in September.\n",
                            subtype="plain", filename="signed-agreement.txt")
    budget = message("budget", "Project totals", "The detailed figures are in the spreadsheet.")
    budget.add_attachment("department,total\nSpectrobridge,3721\n",
                          subtype="csv", filename="budget-q4.csv")
    inbox = [
        message("quasar", "Quasar deployment review", "Review the quasar rollout checklist on Tuesday."),
        message("quasar-note", "Quasar hardware notes", "The quasar equipment arrived on Monday."),
        message("quasar-receipt", "Quasar delivery receipt", "The quasar shipment was received."),
        message("invoice", "Orion invoice", "Invoice INV-2049 for the Orion storage project."),
        message("security", "Security key rotation", "The encryption keys rotate every ninety days."),
        message("spanish", "Reunión del proyecto", "La reunión de coordinación será el martes."),
        message("thread-root", "Auroraforge planning", "The Auroraforge kickoff is ready."),
        message("thread-reply", "Re: Auroraforge planning", "The Auroraforge kickoff is confirmed.",
                **{"In-Reply-To": "<thread-root@benchmark.example.test>",
                   "References": "<thread-root@benchmark.example.test>"}),
        message("sender", "Vela delivery", "Vela release was delivered.", **{"From": "Alex <alex@example.test>"}),
        message("recipient", "Nimbus assignment", "Nimbus assignment for the operations team.",
                **{"To": "Operations <ops@example.test>"}),
        message("sender-foil", "Vela discussion", "Vela planning for another team.",
                **{"From": "Blair <blair@example.test>"}),
        message("recipient-foil", "Nimbus discussion", "Nimbus planning for the sales team.",
                **{"To": "Sales <sales@example.test>"}),
        message("folder-foil", "Heliograph discussion", "The Heliograph meeting is pending."),
        message("date-foil", "Orion earlier discussion", "Orion planning from the previous month.",
                **{"Date": "Thu, 01 Oct 2026 09:00:00 +0000"}),
        message("attachment-foil", "Spectrobridge discussion", "Spectrobridge notes in the body without attachments."),
        contract, budget,
    ]
    inbox.extend(message(f"noise-{i}", f"Routine bulletin {i}",
                         "General status bulletin about ordinary maintenance and schedules.") for i in range(60))
    return {"INBOX": inbox,
            "Archive": [message("archive", "Heliograph archive", "Final Heliograph acceptance record.")]}


def cases() -> list[dict]:
    def case(name, query, expected, **options):
        return {"id": name, "query": query,
                "expected": [f"<{key}@benchmark.example.test>" for key in expected],
                "options": options}
    return [
        case("exact-code", "INV-2049", ["invoice"]),
        case("body-word", "encryption", ["security"]),
        case("common-word-typo", "quasra", ["quasar", "quasar-note", "quasar-receipt"]),
        case("accent", "reunion", ["spanish"]),
        case("sender", "from:alex Vela", ["sender"]),
        case("recipient", "to:ops Nimbus", ["recipient"]),
        case("folder", "folder:Archive Heliograph", ["archive"]),
        case("date", "after:2026-11-02 before:2026-11-03 Orion", ["invoice"]),
        case("thread-individual", "Auroraforge", ["thread-root", "thread-reply"], deduplicate_threads=False),
        case("attachment-text", "Zephyrlattice", ["contract"]),
        case("attachment-filename", "attachment:budget-q4.csv", ["budget"]),
        case("attachment-content-filter", "has:attachment Spectrobridge", ["budget"]),
        case("empty-result", "unmatchablelexemezxq", []),
    ]


def write_corpus(root: Path) -> tuple[Path, int]:
    profile = root / "profile"
    account = profile / "ImapMail" / "benchmark.example.test"
    account.mkdir(parents=True)
    count = 0
    for folder, mails in corpus().items():
        box = mailbox.mbox(account / folder)
        try:
            for mail in mails:
                box.add(mail)
                count += 1
            box.flush()
        finally:
            box.close()
        (account / (folder + ".msf")).touch()
    (root / "queries.json").write_text(json.dumps(
        {"corpus_version": CORPUS_VERSION, "cases": cases()}, indent=2) + "\n", encoding="utf-8")
    return profile, count


def score(rankings: dict[str, list[str]], *, k: int = 5) -> dict:
    """Score ranked Message-IDs; duplicate results never earn duplicate credit."""
    details, recalls, precisions, reciprocal_ranks, top1, negatives = [], [], [], [], [], []
    for case in cases():
        if case["id"] not in rankings:
            raise ValueError(f"Missing ranked results for {case['id']}")
        values = rankings[case["id"]]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("Each case must contain an ordered list of Message-ID strings")
        actual = values[:k]
        expected = set(case["expected"])
        if not expected:
            passed = not actual
            negatives.append(passed)
            details.append({"id": case["id"], "no_false_positives": passed, "results": actual})
            continue
        recall = len(expected.intersection(actual)) / len(expected)
        precision = len(expected.intersection(actual)) / len(actual) if actual else 0
        rr = next((1 / position for position, value in enumerate(actual, 1) if value in expected), 0)
        recalls.append(recall)
        precisions.append(precision)
        reciprocal_ranks.append(rr)
        top1.append(bool(actual and actual[0] in expected))
        details.append({"id": case["id"], "recall": recall, "precision": precision, "reciprocal_rank": rr,
                        "results": actual})
    return {"corpus_version": CORPUS_VERSION, "k": k, "cases": len(details),
            "recall_at_k": statistics.mean(recalls), "mrr_at_k": statistics.mean(reciprocal_ranks),
            "precision_at_k": statistics.mean(precisions),
            "top1_accuracy": statistics.mean(top1), "negative_cases_passed": sum(negatives),
            "negative_cases": len(negatives), "details": details}


def run(*, semantic=False, repeat=1, k=5) -> dict:
    from thunderbird_mcp import index, search
    with tempfile.TemporaryDirectory(prefix="thunderbird-benchmark-") as temporary:
        root = Path(temporary)
        profile, count = write_corpus(root)
        database = root / "index.sqlite"
        stats = index.reindex(db_path=database, profile=profile)
        if semantic:
            search.embed_missing(db_path=database, progress=lambda value: print(value, file=sys.stderr))
        rankings, durations = {}, []
        semantic_states = set()
        for _ in range(repeat):
            for case in cases():
                start = time.perf_counter()
                result = search.search(case["query"], limit=k, db_path=database,
                    use=("fts", "vec") if semantic else ("fts",), **case["options"])
                durations.append((time.perf_counter() - start) * 1000)
                semantic_states.add(result.diagnostics["semantic"])
                rankings[case["id"]] = [row["message_id"] for row in result]
        report = score(rankings, k=k)
        report.update({"mode": "hybrid" if semantic else "keyword", "messages": count,
                       "index_seconds": stats.seconds, "repeat": repeat,
                       "latency_median_ms": round(statistics.median(durations), 3),
                       "latency_p95_ms": round(sorted(durations)[math.ceil(len(durations) * .95) - 1], 3),
                       "semantic_states": sorted(semantic_states),
                       "note": "Small synthetic regression corpus; not a real-mail or competitor accuracy claim."})
        return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--export-corpus", type=Path, metavar="NEW_DIRECTORY")
    modes.add_argument("--score-results", type=Path, metavar="JSON")
    parser.add_argument("--semantic", action="store_true", help="Also run the local embedding model (may download it)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--require-recall", type=float, help="Fail if mean recall is below this threshold")
    parser.add_argument("--require-precision", type=float, help="Fail if mean returned-result precision is below this threshold")
    parser.add_argument("--require-no-false-positives", action="store_true",
                        help="Fail if a labeled empty-result query returns any messages")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 20 or not 1 <= args.k <= 100:
        parser.error("repeat must be 1..20 and k must be 1..100")
    if args.require_recall is not None and not 0 <= args.require_recall <= 1:
        parser.error("require-recall must be between 0 and 1")
    if args.require_precision is not None and not 0 <= args.require_precision <= 1:
        parser.error("require-precision must be between 0 and 1")
    try:
        if args.export_corpus:
            args.export_corpus.mkdir(mode=0o700, parents=True, exist_ok=False)
            profile, count = write_corpus(args.export_corpus)
            print(json.dumps({"profile": str(profile), "messages": count,
                              "queries": str(args.export_corpus / "queries.json")}, indent=2))
            return 0
        if args.score_results:
            report = score(json.loads(args.score_results.read_text(encoding="utf-8")), k=args.k)
        else:
            report = run(semantic=args.semantic, repeat=args.repeat, k=args.k)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(report, indent=2))
    failed_recall = args.require_recall is not None and report["recall_at_k"] < args.require_recall
    failed_precision = args.require_precision is not None and report["precision_at_k"] < args.require_precision
    failed_negative = args.require_no_false_positives and report["negative_cases_passed"] < report["negative_cases"]
    return int(failed_recall or failed_precision or failed_negative)


if __name__ == "__main__":
    raise SystemExit(main())
