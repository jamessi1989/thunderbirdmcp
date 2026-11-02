"""Locate the active Thunderbird profile and enumerate its mbox mail folders."""

from __future__ import annotations

import configparser
import os
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


def _candidate_roots() -> list[Path]:
    home = Path.home()
    roots = [
        home / ".thunderbird",
        home / ".mozilla-thunderbird",
        home / "snap" / "thunderbird" / "common" / ".thunderbird",
        home / ".var" / "app" / "org.mozilla.Thunderbird" / ".thunderbird",
    ]
    if sys.platform == "darwin":
        roots.insert(0, home / "Library" / "Thunderbird")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        roaming = Path(appdata).expanduser() if appdata and Path(appdata).expanduser().is_absolute() else home / "AppData" / "Roaming"
        roots.insert(0, roaming / "Thunderbird")
    return roots


def find_profile() -> Path:
    """Return the path to the Thunderbird profile to index.

    Honours the TB_PROFILE env var (absolute path to a profile dir). Otherwise
    reads profiles.ini and picks the profile with the largest local mail store.
    This avoids Thunderbird's empty default-profile stub. Set TB_PROFILE when
    several populated profiles exist and a different one should be selected.
    """
    override = os.environ.get("TB_PROFILE")
    if override:
        p = Path(override).expanduser().absolute()
        if not p.is_dir():
            raise FileNotFoundError(f"TB_PROFILE is not a directory: {p}")
        return p

    for root in _candidate_roots():
        ini = root / "profiles.ini"
        if not ini.is_file():
            continue
        # Profile names and paths can contain literal percent signs.
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read(ini, encoding="utf-8")
        except (configparser.Error, UnicodeError, OSError):
            raise ValueError(f"Could not read Thunderbird profiles.ini: {ini}. Set TB_PROFILE to the profile directory.") from None

        profiles: list[Path] = []
        for section in cfg.sections():
            if not section.lower().startswith("profile"):
                continue
            path = cfg[section].get("Path")
            if not path:
                continue
            is_relative = cfg[section].get("IsRelative", "1") == "1"
            if sys.platform == "win32":
                path = path.replace("\\", "/")
            profile_dir = (root / path) if is_relative else Path(path)
            if profile_dir.is_dir():
                profiles.append(profile_dir)

        # Prefer a profile that actually has mail stores on disk.
        with_mail = [p for p in profiles if (p / "ImapMail").is_dir() or (p / "Mail").is_dir()]
        candidates = with_mail or profiles
        if candidates:
            # Largest mail footprint wins (handles default vs default-release).
            return max(candidates, key=_mail_footprint)

    raise FileNotFoundError(
        "Could not locate a Thunderbird profile. Set TB_PROFILE to the profile directory."
    )


def _mail_footprint(profile: Path) -> int:
    total = 0
    for base in ("ImapMail", "Mail"):
        d = profile / base
        if d.is_dir():
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    pass
    return total


@dataclass
class Folder:
    name: str  # human-readable, e.g. "mail.example.org/INBOX" or "Local Folders/Sent"
    path: Path  # absolute path to the mbox file
    size: int
    mtime: float


def _folder_name(profile: Path, mbox: Path) -> str:
    """Build a readable folder name from the mbox file path."""
    for base in ("ImapMail", "Mail"):
        root = profile / base
        try:
            rel = mbox.relative_to(root)
        except ValueError:
            continue
        # ".sbd" directories hold subfolders; collapse them into a path separator.
        parts = [urllib.parse.unquote(p[:-4] if p.endswith(".sbd") else p) for p in rel.parts]
        return "/".join(parts)
    return urllib.parse.unquote(mbox.name)


def discover_folders(profile: Path) -> list[Folder]:
    """Return every mbox folder in the profile.

    A file is treated as an mbox folder iff a sibling ``<name>.msf`` summary
    file exists — that is exactly how Thunderbird marks a real mail folder, and
    it cleanly skips .msf/.dat/.sbd and other metadata.
    """
    folders: list[Folder] = []
    for base in ("ImapMail", "Mail"):
        root = profile / base
        if not root.is_dir():
            continue
        for msf in root.rglob("*.msf"):
            mbox = msf.with_suffix("")  # strip ".msf"
            if not mbox.is_file():
                continue
            try:
                st = mbox.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            folders.append(
                Folder(
                    name=_folder_name(profile, mbox),
                    path=mbox,
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
    folders.sort(key=lambda f: f.name.lower())
    return folders
