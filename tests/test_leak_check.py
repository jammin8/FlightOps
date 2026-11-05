import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("privacy_scan", SOURCE / "scripts" / "privacy_scan.py")
SCANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCANNER)


class LeakCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "scripts").mkdir()
        for name in ("leak-check.sh", "privacy_scan.py"):
            shutil.copyfile(SOURCE / "scripts" / name, self.repo / "scripts" / name)
        (self.repo / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.tool = self.bin / "gitleaks"
        self.tool.write_text("#!/bin/sh\nexit 0\n")
        self.tool.chmod(0o755)
        self.denylist = self.root / "private-patterns"
        self.denylist.write_text("SYNTHETIC_PRIVATE_MARKER\n")
        self.env = dict(os.environ)
        self.env.update(PATH=str(self.bin) + os.pathsep + os.environ["PATH"], FLIGHTOPS_DENYLIST=str(self.denylist))
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def scan(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts" / "privacy_scan.py"), *args],
            cwd=self.repo, env=self.env if env is None else env,
            capture_output=True, text=True, timeout=10,
        )

    def assert_refused(self, result):
        self.assertEqual(result.returncode, 1)
        self.assertIn("leak-check: FAILED", result.stderr)
        self.assertNotIn("leak-check: clean", result.stdout)
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", result.stdout + result.stderr)

    def test_staged_content_cannot_be_hidden_by_clean_worktree(self):
        path = self.repo / "report.txt"
        path.write_text("SYNTHETIC_PRIVATE_MARKER")
        self.git("add", "report.txt")
        path.write_text("clean replacement")
        self.assert_refused(self.scan("--staged"))

    def test_unstaged_content_does_not_replace_clean_index(self):
        path = self.repo / "report.txt"
        path.write_text("clean staged content")
        self.git("add", "report.txt")
        path.write_text("SYNTHETIC_PRIVATE_MARKER")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staged_file_is_scanned_after_worktree_removal(self):
        path = self.repo / "report.txt"
        path.write_text("SYNTHETIC_PRIVATE_MARKER")
        self.git("add", "report.txt")
        path.unlink()
        self.assert_refused(self.scan("--staged"))

    def test_private_filename_is_blocked_without_echoing_it(self):
        name = "SYNTHETIC_PRIVATE_MARKER.txt"
        (self.repo / name).write_text("clean")
        self.git("add", name)
        self.assert_refused(self.scan("--staged"))

    def test_missing_gitleaks_blocks_publication(self):
        self.tool.unlink()
        env = dict(self.env, PATH=str(self.bin))
        result = self.scan("--tree", str(self.repo), env=env)
        self.assert_refused(result)
        self.assertIn("gitleaks is required", result.stderr)

    def test_gitleaks_failure_output_is_suppressed(self):
        self.tool.write_text("#!/bin/sh\nprintf 'SYNTHETIC_PRIVATE_MARKER'\nprintf 'SYNTHETIC_PRIVATE_MARKER' >&2\nexit 1\n")
        (self.repo / "report.txt").write_text("clean")
        self.git("add", "report.txt")
        self.assert_refused(self.scan("--staged"))

    def test_binary_index_content_is_refused(self):
        path = self.repo / "report.txt"
        path.write_bytes(b"clean\x00SYNTHETIC_PRIVATE_MARKER")
        self.git("add", "report.txt")
        path.write_text("clean")
        self.assert_refused(self.scan("--staged"))

    def test_unsupported_archive_is_refused_even_if_ascii(self):
        (self.repo / "report.zip").write_text("not an inspectable archive")
        self.git("add", "report.zip")
        self.assert_refused(self.scan("--staged"))

    def test_unreadable_text_encoding_is_refused(self):
        (self.repo / "report.txt").write_bytes(b"\xff\xfe")
        self.git("add", "report.txt")
        self.assert_refused(self.scan("--staged"))

    def test_symlink_cannot_bypass_tree_scan(self):
        target = self.root / "outside.txt"
        target.write_text("clean")
        tree = self.root / "artifact"
        tree.mkdir()
        (tree / "link").symlink_to(target)
        self.assert_refused(self.scan("--tree", str(tree)))

    def test_directory_symlink_is_refused(self):
        tree = self.root / "artifact"
        tree.mkdir()
        (tree / "link").symlink_to(self.repo, target_is_directory=True)
        self.assert_refused(self.scan("--tree", str(tree)))

    def test_staged_symlink_is_refused(self):
        (self.repo / "link").symlink_to("report.txt")
        self.git("add", "link")
        self.assert_refused(self.scan("--staged"))

    def test_missing_explicit_denylist_is_refused(self):
        self.denylist.unlink()
        self.assert_refused(self.scan("--staged"))

    def test_invalid_private_regex_is_refused_without_echoing_it(self):
        self.denylist.write_text("[SYNTHETIC_PRIVATE_MARKER")
        self.assert_refused(self.scan("--staged"))

    def test_clean_tree_passes(self):
        tree = self.root / "artifact"
        tree.mkdir()
        (tree / "report.txt").write_text("clean report")
        result = self.scan("--tree", str(tree))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "leak-check: clean\n")

    def test_scan_configuration_files_are_not_exempt_from_patterns(self):
        path = self.repo / ".gitleaks.toml"
        path.write_text("SYNTHETIC_PRIVATE_MARKER")
        self.git("add", ".gitleaks.toml")
        self.assert_refused(self.scan("--staged"))

    def test_oversized_file_is_refused_before_gitleaks(self):
        tree = self.root / "artifact"
        tree.mkdir()
        (tree / "report.txt").write_bytes(b"a" * 20)
        with mock.patch.object(SCANNER, "MAX_FILE_BYTES", 10):
            with self.assertRaises(SCANNER.Refused):
                SCANNER.snapshot("tree", tree, self.root, [])

    def test_nonregular_file_does_not_block_scan(self):
        path = self.root / "pipe"
        os.mkfifo(path)
        with self.assertRaises(SCANNER.Refused):
            SCANNER.read_file(path)

    def test_filename_manifest_reaches_gitleaks(self):
        calls = self.root / "calls"
        self.tool.write_text("#!/bin/sh\nfor arg do last=$arg; done\ntest -f \"$last/filenames.txt\" && echo filenames >> \"$FLIGHTOPS_TEST_CALLS\"\ntest -f \"$last/report.txt\" && echo report >> \"$FLIGHTOPS_TEST_CALLS\"\nexit 0\n")
        (self.repo / "report.txt").write_text("clean")
        self.git("add", "report.txt")
        result = self.scan("--staged", env=dict(self.env, FLIGHTOPS_TEST_CALLS=str(calls)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["report", "filenames"])

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks is unavailable")
    def test_real_gitleaks_keeps_path_filtered_rules_for_index_and_tree(self):
        self.tool.unlink()
        (self.repo / ".gitleaks.toml").write_text(
            "[[rules]]\nid = 'synthetic-path-secret'\ndescription = 'Synthetic path token'\n"
            "regex = '''SYNTHETIC_SECRET_[A-Z0-9]{8}'''\npath = '''credentials[.]txt$'''\n"
        )
        artifact = self.root / "artifact"
        artifact.mkdir()
        (artifact / "filenames.txt").write_text("clean manifest-named input")
        path = artifact / "credentials.txt"
        path.write_text("SYNTHETIC_SECRET_ABC12345")
        self.assert_refused(self.scan("--tree", str(artifact)))
        staged = self.repo / "nested" / "credentials.txt"
        staged.parent.mkdir()
        staged.write_bytes(path.read_bytes())
        self.git("add", "nested/credentials.txt")
        staged.write_text("clean replacement")
        self.assert_refused(self.scan("--staged"))

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks is unavailable")
    def test_real_gitleaks_checks_the_staged_snapshot(self):
        self.tool.unlink()
        (self.repo / ".gitleaks.toml").write_text(
            "[[rules]]\nid = 'synthetic-secret'\ndescription = 'Synthetic test token'\n"
            "regex = '''SYNTHETIC_SECRET_[A-Z0-9]{8}'''\n"
        )
        path = self.repo / "report.txt"
        path.write_text("clean staged content")
        self.git("add", "report.txt")
        clean = self.scan("--staged")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        path.write_text("SYNTHETIC_SECRET_ABC12345")
        self.git("add", "report.txt")
        path.write_text("clean replacement")
        result = self.scan("--staged")
        self.assert_refused(result)
        self.assertNotIn("SYNTHETIC_SECRET_ABC12345", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
