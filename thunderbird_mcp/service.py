"""Explicit foreground automation service; never enabled by client setup."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter

from . import automation, outgoing, policy, runtime


def _parser():
    parser = argparse.ArgumentParser(description="Run opt-in Thunderbird mail watcher or scheduler ticks")
    modes = parser.add_subparsers(dest="mode", required=True)
    for name in ("watch", "scheduler"):
        command = modes.add_parser(name)
        command.add_argument("--once", action="store_true", help="Run one bounded tick and exit (suitable for cron)")
        command.add_argument("--interval", type=int, default=60, help="Seconds between ticks, from 1 to 3600 (default: 60)")
        command.add_argument("--account", help="Restrict this process to one configured account")
        if name == "watch":
            command.add_argument("--folder", default="INBOX")
            command.add_argument("--idle", type=int, default=0, metavar="SECONDS", help="Wait up to 50 seconds using IDLE when advertised")
            command.add_argument("--include-existing", action="store_true", help="Include existing mail only when first establishing this watch")
        else:
            command.add_argument("--limit", type=int, default=10, help="Maximum due jobs per tick (1 to 100)")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not 1 <= args.interval <= 3600:
        parser.error("--interval must be between 1 and 3600")
    if args.mode == "watch" and not 0 <= args.idle <= 50:
        parser.error("--idle must be between 0 and 50")
    if args.mode == "scheduler" and not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    runtime.configure_session_environment()
    account = outgoing.account_config(args.account)["account_id"] if args.account else None
    scope = policy.Policy(accounts=frozenset({account}) if account else None)
    try:
        with policy.context(scope):
            while True:
                started = time.monotonic()
                try:
                    if args.mode == "watch":
                        result = automation.watch_mail(account=account, folder=args.folder, wait_seconds=args.idle,
                                                       include_existing=args.include_existing)
                        summary = {key: result[key] for key in ("mode", "baseline", "folder_reset", "recorded", "more_pending", "cursor")}
                    else:
                        result = automation.run_scheduled(limit=args.limit)
                        summary = {"processed": result["count"],
                                   "outcomes": dict(Counter(job["status"] for job in result["schedules"]))}
                    print(json.dumps({"service": args.mode, "time": time.time(), **summary}), flush=True)
                    exit_code = 0
                except Exception as exc:
                    # Do not log server replies, mail subjects, recipients,
                    # bodies or credential-bearing exception messages.
                    print(json.dumps({"service": args.mode, "time": time.time(), "error": {"type": type(exc).__name__}}), flush=True)
                    exit_code = 1
                if args.once:
                    return exit_code
                remaining = args.interval - (time.monotonic() - started)
                while remaining > 0:
                    time.sleep(min(remaining, 1))
                    remaining = args.interval - (time.monotonic() - started)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
