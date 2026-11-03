import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import ledger_store

RUN = {"id": 7, "run_attempt": 1, "name": "CI", "status": "completed", "conclusion": "failure", "head_sha": "a" * 40, "updated_at": "2026-11-01T00:00:00Z"}


def attempt(**changes):
    return ledger_store.normal_run("web", dict(RUN, **changes))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ledger_store.Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.store.begin_pass(1000)

    def add(self, row):
        with self.store.tx():
            return self.store.add_run(row, "github-rest", None, "2026-11-01T00:00:00Z")

    def test_the_file_is_private_to_the_owner(self):
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_evidence_rows_cannot_be_rewritten(self):
        self.add(attempt())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.db.execute("UPDATE ci_run_attempts SET conclusion = 'success'")

    def test_evidence_rows_cannot_be_deleted(self):
        self.add(attempt())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.db.execute("DELETE FROM ci_run_attempts")

    def test_an_unchanged_observation_adds_nothing(self):
        self.assertEqual((self.add(attempt()), self.add(attempt())), (1, 0))

    def test_the_same_observation_from_two_sources_keeps_both_witnesses(self):
        self.add(attempt())
        with self.store.tx():
            self.store.add_run(attempt(), "ci-program-daily", "1", None)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ci_run_attempts").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ci_attempt_latest").fetchone()[0], 1)

    def test_a_changed_attempt_keeps_both_observations(self):
        self.add(attempt(status="in_progress", conclusion=None, updated_at="2026-11-01T00:00:00Z"))
        self.add(attempt(updated_at="2026-11-01T01:00:00Z"))
        rows = self.store.db.execute("SELECT conclusion FROM ci_run_attempts ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], [None, "failure"])

    def test_an_older_observation_arriving_later_does_not_become_latest(self):
        self.add(attempt(updated_at="2026-11-01T01:00:00Z"))
        self.add(attempt(status="in_progress", conclusion=None, updated_at="2026-11-01T00:00:00Z"))
        self.assertEqual(self.store.db.execute("SELECT conclusion FROM ci_attempt_latest").fetchone()[0], "failure")

    def test_a_pass_on_retry_is_labelled_retry_recovered(self):
        self.add(attempt())
        self.add(attempt(run_attempt=2, conclusion="success"))
        row = self.store.db.execute("SELECT label, failed_attempt, passed_attempt FROM ci_retry_outcomes").fetchone()
        self.assertEqual(tuple(row), ("retry recovered", 1, 2))

    def test_a_retry_that_fails_last_is_not_recovered(self):
        self.add(attempt())
        self.add(attempt(run_attempt=2, conclusion="success"))
        self.add(attempt(run_attempt=3))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ci_retry_outcomes").fetchone()[0], 0)

    def test_a_reopened_gap_keeps_each_episode_with_its_time_and_detail(self):
        with self.store.tx():
            self.store.open_gap("activity", "web", "walk", "open", "first", 1000)
            self.store.close_gap("activity", "web", "walk", "open", 1100)
            self.store.open_gap("activity", "web", "walk", "open", "second", 1200)
        rows = [tuple(r) for r in self.store.db.execute("SELECT detail, opened_at, closed_at FROM gaps ORDER BY id")]
        self.assertEqual(rows, [("first", ledger_store.iso(1000), ledger_store.iso(1100)), ("second", ledger_store.iso(1200), None)])

    def test_a_changed_detail_is_appended_and_the_first_stays(self):
        with self.store.tx():
            self.store.open_gap("activity", "web", "walk", "open", "first", 1000)
            self.store.open_gap("activity", "web", "walk", "open", "first", 1050)
            self.store.open_gap("activity", "web", "walk", "open", "second", 1100)
        self.assertEqual([tuple(r) for r in self.store.db.execute("SELECT event, detail, at FROM gap_events ORDER BY id")],
                         [("opened", "first", ledger_store.iso(1000)), ("updated", "second", ledger_store.iso(1100))])
        self.assertEqual(self.store.db.execute("SELECT detail FROM gaps").fetchone()[0], "first")

    def test_closing_a_closed_gap_adds_nothing(self):
        with self.store.tx():
            self.store.close_gap("activity", "web", "walk", "open", 1000)
            self.store.open_gap("activity", "web", "walk", "open", "first", 1000)
            self.store.close_gap("activity", "web", "walk", "open", 1100)
            self.store.close_gap("activity", "web", "walk", "open", 1200)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM gap_events").fetchone()[0], 2)

    def test_gap_history_cannot_be_rewritten(self):
        with self.store.tx():
            self.store.open_gap("activity", "web", "walk", "open", "first", 1000)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.db.execute("UPDATE gap_events SET detail = 'other'")

    def test_a_read_only_store_refuses_writes(self):
        reader = ledger_store.Store(self.tmp.name, readonly=True)
        self.addCleanup(reader.close)
        with self.assertRaises(sqlite3.OperationalError):
            reader.db.execute("INSERT INTO meta VALUES ('a', 'b')")

    def test_a_newer_schema_is_refused_not_rewritten(self):
        self.store.db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ledger_store.Refused, "schema 99"):
            ledger_store.Store(self.tmp.name)


def old_file(directory):
    path = os.path.join(directory, ledger_store.FILE)
    made = sqlite3.connect(path)
    made.execute("CREATE TABLE gaps(id INTEGER PRIMARY KEY, detail TEXT)")
    made.execute("INSERT INTO gaps(detail) VALUES ('kept')")
    made.execute("PRAGMA user_version=1")
    made.commit()
    made.close()
    return path


class OldFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = old_file(self.tmp.name)
        with open(self.path, "rb") as f:
            self.bytes = f.read()

    def assert_kept(self):
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), self.bytes)

    def test_a_version_one_file_is_refused_with_a_clear_reason_and_kept(self):
        with self.assertRaisesRegex(ledger_store.Refused, "schema 1, this build needs 2.*unchanged.*aside"):
            ledger_store.Store(self.tmp.name)
        self.assert_kept()

    def test_the_read_only_report_refuses_it_too(self):
        with self.assertRaises(ledger_store.Refused):
            ledger_store.Store(self.tmp.name, readonly=True)
        self.assert_kept()

    def test_moving_it_aside_rebuilds(self):
        os.rename(self.path, self.path + ".v1")
        store = ledger_store.Store(self.tmp.name)
        self.addCleanup(store.close)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(store.db.execute("SELECT count(*) FROM gap_events").fetchone()[0], 0)


class Normal(unittest.TestCase):
    def test_a_run_without_identity_is_refused(self):
        self.assertIsNone(ledger_store.normal_run("web", {"id": "x", "run_attempt": 1}))
        self.assertIsNone(ledger_store.normal_run("web", {"id": 1, "run_attempt": 0}))

    def test_free_text_never_enters_a_row(self):
        row = attempt(display_title="SECRET", head_commit={"message": "SECRET"})
        self.assertNotIn("SECRET", repr(row))

    def test_unknown_clocks_stay_null(self):
        self.assertIsNone(attempt()["started_at"])


if __name__ == "__main__":
    unittest.main()
