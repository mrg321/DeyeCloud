"""Publication checks include removed historical files, staged data and metadata."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.check_publication import audit, content_findings


class PublicationTests(unittest.TestCase):
    def repo(self, root):
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        self.run_git(root, "config", "user.name", "Synthetic test")
        self.run_git(root, "config", "user.email", "fake@example.invalid")

    def run_git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    def test_removed_private_file_stays_detectable_in_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.repo(root)
            (root / "private.json").write_text('{"station_id":123}')
            self.run_git(root, "add", "private.json")
            self.run_git(root, "commit", "-qm", "Synthetic fixture")
            (root / "private.json").unlink()
            (root / "README.md").write_text("Generic project")
            self.run_git(root, "add", "-A")
            self.run_git(root, "commit", "-qm", "Remove synthetic fixture")
            report = audit(root)
            self.assertEqual(report["commits_checked"], 2)
            self.assertIn("UNREVIEWED_HISTORICAL_PATH", {item["code"] for item in report["findings"]})
            self.assertNotIn("station_id", json.dumps(report))

    def test_staged_secret_is_detected_even_after_cleaning_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.repo(root)
            path = root / "README.md"
            path.write_text("Generic project")
            self.run_git(root, "add", "README.md")
            self.run_git(root, "commit", "-qm", "Synthetic fixture")
            secret = "ghp_" + "x" * 36
            path.write_text(secret)
            self.run_git(root, "add", "README.md")
            path.write_text("Generic project")
            report = audit(root)
            self.assertIn({"scope": "index", "file": "README.md", "code": "GITHUB_TOKEN"}, report["findings"])
            self.assertNotIn(secret, json.dumps(report))

    def test_email_metadata_flag_is_value_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.repo(root)
            self.run_git(root, "config", "user.email", "fake@personal.invalid")
            (root / "README.md").write_text("Generic project")
            self.run_git(root, "add", "README.md")
            self.run_git(root, "commit", "-qm", "Synthetic fixture")
            report = audit(root)
            self.assertIn("GIT_IDENTITY_REVIEW", {item["code"] for item in report["findings"]})
            self.assertNotIn("fake@personal.invalid", json.dumps(report))

    def test_scan_does_not_echo_or_mistake_templates_for_secrets(self):
        self.assertEqual(content_findings('{"password":"fake-password"}'), [])
        self.assertEqual(content_findings('{"password":"DEYE_PASSWORD"}'), [])
        self.assertIn("CREDENTIAL_LITERAL", content_findings('{"password":"' + "unmarked" + '"}'))
        self.assertIn("PRIVATE_KEY", content_findings("-----BEGIN " + "PRIVATE KEY-----"))


if __name__ == "__main__":
    unittest.main()
