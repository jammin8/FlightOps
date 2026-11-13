import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import ledger_import
import ledger_store

NOW = ledger_store.epoch("2026-11-01T12:00:00Z")
COLLECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collectors")


def run_row(run_id, attempt=1, conclusion="failure", **more):
    return dict({"id": run_id, "run_attempt": attempt, "name": "Q", "status": "completed", "conclusion": conclusion, "head_sha": "a" * 40, "created_at": "2026-10-05T00:00:00Z",
                 "updated_at": f"2026-10-05T0{attempt}:00:00Z", "display_title": "SECRET é"}, **more)



def address_space_capped(limit=160 * 1024 * 1024):
    probe = f"import resource; resource.setrlimit(resource.RLIMIT_AS, ({limit}, {limit}))"
    return subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True).returncode == 0

class Import(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "daily")
        self.store = ledger_store.Store(os.path.join(self.tmp.name, "data"))
        self.addCleanup(self.store.close)
        self.store.begin_pass(NOW)

    def snapshot(self, day, rows, repo="example-web", raw=None):
        folder = os.path.join(self.root, day, repo)
        os.makedirs(folder)
        with open(os.path.join(self.root, day, "config.json"), "w") as f:
            json.dump({"start": "2026-10-01T00:00:00+00:00", "end": f"{day}T06:00:00+00:00"}, f)
        path = os.path.join(folder, "runs.json")
        with open(path, "w") as f:
            f.write(raw if raw is not None else json.dumps(rows))
        return path

    def fingerprint(self, path):
        with open(path, "rb") as f:
            return os.stat(path).st_mtime_ns, f.read()

    def rows(self, sql):
        return [tuple(r) for r in self.store.db.execute(sql).fetchall()]

    def run_import(self, **kwargs):
        return ledger_import.run(self.store, self.root, now=NOW, **kwargs)

    def test_runs_are_marked_with_their_source_and_snapshot_clock(self):
        self.snapshot("2026-10-24", [run_row(1)])
        self.run_import()
        self.assertEqual(self.rows("SELECT source, observed_at, conclusion, repo FROM ci_run_attempts"), [("ci-program-daily", "2026-10-24T06:00:00+00:00", "failure", "web")])

    def test_each_file_states_its_limits(self):
        self.snapshot("2026-10-24", [run_row(1)])
        self.run_import()
        self.assertIn("No jobs", self.rows("SELECT limits FROM import_files")[0][0])

    def test_the_snapshot_is_not_modified(self):
        path = self.snapshot("2026-10-24", [run_row(1)])
        before = self.fingerprint(path)
        self.run_import()
        self.assertEqual(self.fingerprint(path), before)

    def test_import_happens_once_and_a_later_snapshot_is_ignored(self):
        self.snapshot("2026-10-24", [run_row(1)])
        self.run_import()
        self.snapshot("2026-10-25", [run_row(2)])
        self.run_import()
        self.assertEqual(self.rows("SELECT count(*) FROM ci_run_attempts"), [(1,)])

    def test_the_same_run_across_snapshots_counts_once_unless_it_changed(self):
        self.snapshot("2026-10-24", [run_row(1), run_row(2)])
        self.snapshot("2026-10-25", [run_row(1), run_row(2, conclusion="success")])
        self.run_import()
        self.assertEqual(self.rows("SELECT run_id, conclusion FROM ci_run_attempts ORDER BY id"), [(1, "failure"), (2, "failure"), (2, "success")])

    def test_an_attempt_seen_only_in_an_earlier_snapshot_survives(self):
        self.snapshot("2026-10-24", [run_row(1)])
        self.snapshot("2026-10-25", [run_row(1, attempt=2, conclusion="success")])
        self.run_import()
        self.assertEqual(self.rows("SELECT run_attempt, conclusion FROM ci_attempt_latest ORDER BY run_attempt"), [(1, "failure"), (2, "success")])

    def test_a_time_limit_resumes_mid_file_without_loss_or_repeats(self):
        self.snapshot("2026-10-24", [run_row(i) for i in range(1, ledger_import.COMMIT_EVERY * 2 + 3)])
        ticks = iter([0, 0, 1, 2, 2, 2])
        progress = self.run_import(seconds=1, clock=lambda: next(ticks, 9))
        self.assertEqual(progress["pending"], 1)
        self.assertEqual(self.rows("SELECT rows_read FROM import_files"), [(ledger_import.COMMIT_EVERY,)])
        self.run_import()
        self.assertEqual(self.rows("SELECT state, rows_read, rows_new FROM import_files"), [("done", ledger_import.COMMIT_EVERY * 2 + 2, ledger_import.COMMIT_EVERY * 2 + 2)])

    def test_a_truncated_file_is_a_gap_and_keeps_what_was_whole(self):
        self.snapshot("2026-10-24", None, raw=json.dumps([run_row(1), run_row(2)])[:-30])
        self.run_import()
        self.assertEqual(self.rows("SELECT state, rows_read FROM import_files"), [("failed", 1)])
        self.assertEqual(self.rows("SELECT kind FROM gaps"), [("file-unreadable",)])
        self.assertEqual(self.rows("SELECT count(*) FROM ci_run_attempts"), [(1,)])

    def test_a_file_changed_after_the_manifest_froze_is_not_read(self):
        path = self.snapshot("2026-10-24", [run_row(1)])
        ledger_import.freeze_manifest(self.store, self.root, NOW)
        with open(path, "a") as f:
            f.write(" ")
        self.run_import()
        self.assertEqual(self.rows("SELECT state FROM import_files"), [("changed",)])

    def test_a_missing_folder_is_a_gap_and_retried(self):
        self.run_import()
        self.assertEqual(self.rows("SELECT kind FROM gaps WHERE closed_pass IS NULL"), [("source-missing",)])
        self.snapshot("2026-10-24", [run_row(1)])
        self.run_import()
        self.assertEqual(self.rows("SELECT count(*) FROM gaps WHERE closed_pass IS NULL"), [(0,)])

    def test_a_run_without_identity_is_counted_as_bad(self):
        self.snapshot("2026-10-24", [{"id": "x"}, run_row(1)])
        self.run_import()
        self.assertEqual(self.rows("SELECT rows_read, rows_bad, rows_new FROM import_files"), [(2, 1, 1)])

    def test_free_text_is_not_imported(self):
        self.snapshot("2026-10-24", [run_row(1)])
        self.run_import()
        self.store.checkpoint()
        with open(self.store.path, "rb") as f:
            self.assertNotIn(b"SECRET", f.read())

    def test_a_multibyte_file_resumes_on_the_exact_byte(self):
        self.snapshot("2026-10-24", None, raw=json.dumps([run_row(i) for i in range(1, ledger_import.COMMIT_EVERY + 3)], ensure_ascii=False))
        ticks = iter([0, 0, 1, 2, 2])
        self.run_import(seconds=1, clock=lambda: next(ticks, 9))
        self.run_import()
        self.assertEqual(self.rows("SELECT state, rows_bad, rows_new FROM import_files"), [("done", 0, ledger_import.COMMIT_EVERY + 2)])

    @unittest.skipUnless(address_space_capped(), "this OS refuses an address-space limit")
    def test_a_large_file_stays_inside_a_small_address_space(self):
        path = self.snapshot("2026-10-24", [])
        with open(path, "w") as f:
            f.write("[")
            padding = "x" * 16000
            for i in range(1, 4001):
                f.write((", " if i > 1 else "") + json.dumps(run_row(i, pad=padding)))
            f.write("]")
        self.assertGreater(os.path.getsize(path), 60 * 1024 * 1024)
        script = textwrap.dedent(f"""
            import resource, sys
            resource.setrlimit(resource.RLIMIT_AS, (160 * 1024 * 1024, 160 * 1024 * 1024))
            sys.path.insert(0, {COLLECTORS!r})
            import ledger_import, ledger_store
            store = ledger_store.Store({os.path.join(self.tmp.name, 'bounded')!r})
            store.begin_pass({NOW})
            print(ledger_import.run(store, {self.root!r}, seconds=120, now={NOW}))
        """)
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        self.assertIn("'pending': 0", done.stdout, done.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
