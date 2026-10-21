"""Windows and release-update regression tests; no network or Docker required."""
import io
import json
import os
import unittest

from bounty_hunter.desktop_updates import check_for_updates
from bounty_hunter.isolation import (
    clone_command, baseline_clone_command, execution_command,
    preparation_command, SAFE_NPM_PREPARATION,
)


class DesktopPlatformTests(unittest.TestCase):
    def test_official_newer_release(self):
        data = {
            "tag_name": "v0.3.0",
            "html_url": "https://github.com/Unborne-Fetus/bounty-hunter/releases/tag/v0.3.0",
        }
        fetcher = lambda request, timeout: io.BytesIO(json.dumps(data).encode())
        update = check_for_updates(fetcher=fetcher)
        self.assertTrue(update["available"])
        self.assertEqual(update["latest_version"], "v0.3.0")

    def test_release_link_cannot_be_redirected_to_hostile_origin(self):
        data = {
            "tag_name": "v999.0.0",
            "html_url": "https://example.net/malware.exe",
        }
        fetcher = lambda request, timeout: io.BytesIO(json.dumps(data).encode())
        with self.assertRaisesRegex(ValueError, "unexpected download"):
            check_for_updates(fetcher=fetcher)

    @unittest.skipUnless(os.name == "nt", "Windows-specific uid absence")
    def test_isolation_commands_do_not_call_unix_getuid_on_windows(self):
        clone = clone_command(
            "docker", "https://github.com/example/repo",
            r"C:\\work\\attempt", image_reference="alpine/git@sha256:" + "a" * 64
        )
        baseline = baseline_clone_command(
            "docker", r"C:\\work\\source", r"C:\\work\\baseline",
            "alpine/git@sha256:" + "a" * 64
        )
        exec_args = execution_command(
            "docker", r"C:\\work\\source", "python:3.11", ["python", "-m", "pytest"]
        )
        preparation = preparation_command(
            "docker", r"C:\\work\\source", r"C:\\work\\deps",
            "node:20", "javascript", SAFE_NPM_PREPARATION
        )
        for args in (clone, baseline, exec_args, preparation):
            self.assertNotIn("--user", args)
            self.assertIn("--cap-drop", args)
            self.assertIn("ALL", args)


if __name__ == "__main__":
    unittest.main()
