#!/usr/bin/env python3
"""Keep private daily GitHub traffic snapshots without accessing any mail."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


DEFAULT_REPOSITORY = "jamessi1989/thunderbirdmcp"


def github(endpoint: str) -> object:
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        # Do not save or echo authentication diagnostics or the user's environment.
        raise RuntimeError(f"GitHub request failed: {endpoint}. Check gh auth status and repository access.")
    return json.loads(result.stdout)


def collect(repository: str) -> dict:
    root = f"repos/{repository}"
    repository_data = github(root)
    snapshot = {
        "schema_version": 1,
        "repository": repository,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "repository_counts": {key: repository_data[key] for key in (
            "stargazers_count", "forks_count", "subscribers_count", "open_issues_count",
        )},
        "views": github(root + "/traffic/views?per=day"),
        "clones": github(root + "/traffic/clones?per=day"),
        "referrers": github(root + "/traffic/popular/referrers"),
        "popular_paths": github(root + "/traffic/popular/paths"),
        "releases": [],
    }
    page = 1
    while True:
        releases = github(root + f"/releases?per_page=100&page={page}")
        for release in releases:
            if release.get("draft"):
                continue
            snapshot["releases"].append({
                "id": release["id"], "tag_name": release["tag_name"],
                "published_at": release["published_at"],
                "assets": [{key: asset[key] for key in ("id", "name", "download_count")}
                           for asset in release["assets"]],
            })
        if len(releases) < 100:
            break
        page += 1
    return snapshot


def storage_directory(repository: str) -> Path:
    value = os.environ.get("XDG_DATA_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / ".local/share"
    return base / "thunderbird-mcp/repo-traffic" / repository.replace("/", "--")


def save_snapshot(directory: Path, snapshot: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory.chmod(0o700)
    destination = directory / (snapshot["collected_at"][:10] + ".json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".snapshot-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def report(snapshot: dict, directory: Path, count: int) -> None:
    print(f"{snapshot['repository']} — collected {snapshot['collected_at']}")
    for field, label in (("views", "Repository views"), ("clones", "Full clones")):
        values = snapshot[field]
        print(f"{label}, GitHub's rolling 14 days: {values['count']} ({values['uniques']} unique)")
    counts = snapshot["repository_counts"]
    print(f"Stars: {counts['stargazers_count']} | Forks: {counts['forks_count']}")
    for release in snapshot["releases"]:
        artifacts = [f"{asset['name']}: {asset['download_count']}"
                     for asset in release["assets"] if asset["name"] != "SHA256SUMS"]
        print(f"{release['tag_name']} lifetime artifact downloads: " + ", ".join(artifacts))
    sources = snapshot["referrers"]
    print("Reported referrers: " + (", ".join(
        f"{source['referrer']} ({source['count']})" for source in sources
    ) if sources else "none reported"))
    print(f"Saved daily snapshots: {count} | {directory}")
    print("Counts may include CI, bots and our own checks; they are not installed-user counts.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "report"), nargs="?", default="report")
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--json", action="store_true", help="Print the complete latest snapshot")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", args.repo) or args.repo.split("/")[-1] in {".", ".."}:
        parser.error("--repo must be OWNER/REPOSITORY")
    directory = storage_directory(args.repo)
    try:
        if args.command == "collect":
            # Only commit a snapshot after every API request has succeeded.
            snapshot = collect(args.repo)
            save_snapshot(directory, snapshot)
        paths = sorted(path for path in directory.glob("????-??-??.json") if path.is_file())
        if not paths:
            raise RuntimeError("No snapshots yet. Run this command with collect first.")
        snapshot = json.loads(paths[-1].read_text(encoding="utf-8"))
        if args.json:
            print(json.dumps(snapshot, indent=2, ensure_ascii=True))
        else:
            report(snapshot, directory, len(paths))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Traffic collection/report failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
