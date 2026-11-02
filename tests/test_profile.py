"""Profile discovery checks never inspect a user's real Thunderbird directory."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunderbird_mcp import profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        for context in (patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=self.home)):
            context.start()
            self.addCleanup(context.stop)

    def test_missing_profile_explains_override(self):
        with self.assertRaisesRegex(FileNotFoundError, "Set TB_PROFILE"):
            profile.find_profile()

    def test_macos_profile_root_and_literal_percent_path(self):
        root = self.home / "Library/Thunderbird"
        selected = root / "Profiles/100% mail"
        selected.mkdir(parents=True)
        (root / "profiles.ini").write_text("[Profile0]\nIsRelative=1\nPath=Profiles/100% mail\n", encoding="utf-8")
        with patch.object(profile.sys, "platform", "darwin"):
            self.assertEqual(profile.find_profile(), selected)
            self.assertEqual(profile._candidate_roots()[0], root)

    def test_linux_selection_preserves_largest_populated_profile(self):
        root = self.home / ".thunderbird"
        stub, populated = root / "empty.default", root / "mail.default-release"
        stub.mkdir(parents=True)
        mail = populated / "ImapMail/example.test"
        mail.mkdir(parents=True)
        (mail / "INBOX").write_bytes(b"synthetic content")
        (root / "profiles.ini").write_text("[Profile0]\nPath=empty.default\nDefault=1\n[Profile1]\nPath=mail.default-release\n", encoding="utf-8")
        with patch.object(profile.sys, "platform", "linux"):
            self.assertEqual(profile.find_profile(), populated)

    def test_absolute_profile_location_and_explicit_override(self):
        root = self.home / ".thunderbird"
        root.mkdir()
        selected = self.home / "mail elsewhere"
        selected.mkdir()
        (root / "profiles.ini").write_text(f"[Profile0]\nIsRelative=0\nPath={selected}\n", encoding="utf-8")
        self.assertEqual(profile.find_profile(), selected)
        override = self.home / "another profile"
        override.mkdir()
        with patch.dict(os.environ, {"TB_PROFILE": str(override)}):
            self.assertEqual(profile.find_profile(), override)

    def test_malformed_profiles_file_gives_actionable_error(self):
        root = self.home / ".thunderbird"
        root.mkdir()
        (root / "profiles.ini").write_text("not an ini file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Set TB_PROFILE") as result:
            profile.find_profile()
        self.assertNotIn("not an ini file", str(result.exception))


if __name__ == "__main__":
    unittest.main()
