import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import ledger_activity
import ledger_github
import ledger_store
from ledger_fakes import FakeGitHub, http, page_link
from ledger_store import ZERO

TOP = ledger_activity.page_path("web")
NOW = ledger_store.epoch("2026-11-01T12:00:00Z")


def sha(n):
    return f"{n:040x}"


def event(n, at, before=None, after=None, kind="push", ref="refs/heads/b"):
    return {"id": n, "ref": ref, "before": before if before is not None else sha(n - 1), "after": after if after is not None else sha(n),
            "timestamp": at, "activity_type": kind, "actor": {"login": "jammin8", "id": 1, "avatar_url": "x"}}


def row(n, at, before=None, after=None):
    made = event(n, at, before, after)
    return {"event_id": n, "type": "push", "ref": "refs/heads/b", "before_sha": made["before"], "after_sha": made["after"], "at": at}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ledger_store.Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.store.begin_pass(NOW)

    def read(self, fake, now=NOW):
        client = ledger_github.Client(runner=fake)
        client.start()
        try:
            return ledger_activity.read_repo(self.store, client, "web", now)
        except ledger_github.Stop:
            return None

    def state(self):
        return ledger_activity.state_of(self.store, "web")

    def count(self, table):
        return self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def gaps(self):
        return [r[0] for r in self.store.db.execute("SELECT kind FROM gaps WHERE closed_pass IS NULL")]


class Reader(Base):
    def test_full_identifiers_and_actor_are_kept(self):
        self.read(FakeGitHub({TOP: [event(5, "2026-11-01T10:00:00Z")]}))
        kept = self.store.db.execute("SELECT * FROM activity_events").fetchone()
        self.assertEqual((kept["event_id"], kept["before_sha"], kept["after_sha"], kept["actor"], kept["actor_id"]), (5, sha(4), sha(5), "jammin8", 1))

    def test_an_unconditional_first_read_then_a_conditional_one(self):
        fake = FakeGitHub({TOP: http(200, [event(5, "2026-11-01T10:00:00Z")], {"ETag": 'W/"a"'})})
        self.read(fake)
        fake.routes[TOP] = lambda p, e: http(304)
        self.read(fake, NOW + 900)
        self.assertEqual([c[1] for c in fake.calls], [None, 'W/"a"'])

    def test_a_304_advances_coverage_without_rows(self):
        fake = FakeGitHub({TOP: http(200, [event(5, "2026-11-01T10:00:00Z")], {"ETag": "a"})})
        self.read(fake)
        fake.routes[TOP] = lambda p, e: http(304)
        self.read(fake, NOW + 900)
        self.assertEqual(self.state()["covered_to"], ledger_store.iso(NOW + 900))

    def test_a_backfill_stops_at_its_page_limit_and_resumes_from_the_cursor(self):
        routes = {ledger_activity.page_path("web", f"c{i}" if i else None): http(200, [event(100 - i, f"2026-10-{20 - i}T00:00:00Z")], page_link(f"c{i + 1}"))
                  for i in range(ledger_activity.PAGES_PER_REPO + 2)}
        routes[ledger_activity.page_path("web", f"c{ledger_activity.PAGES_PER_REPO + 2}")] = [event(1, "2026-10-01T00:00:00Z")]
        fake = FakeGitHub(routes)
        self.read(fake)
        self.assertEqual((len(fake.calls), self.state()["walk"]["after"], self.gaps()), (ledger_activity.PAGES_PER_REPO, f"c{ledger_activity.PAGES_PER_REPO}", ["walk-open"]))
        self.read(fake, NOW + 900)
        self.assertEqual((self.state()["walk"], self.state()["feed_start"], self.gaps()), (None, True, []))
        self.assertEqual(self.state()["covered_from"], "2026-10-01T00:00:00Z")

    def test_reading_reaches_back_through_the_overlap_and_no_further(self):
        fake = FakeGitHub({TOP: http(200, [event(5, "2026-11-01T11:00:00Z")], {"ETag": "a"})})
        self.read(fake)
        older = ledger_store.iso(NOW - ledger_activity.OVERLAP - 1)
        fake.routes[TOP] = http(200, [event(9, ledger_store.iso(NOW + 500)), event(8, older)], page_link("next"))
        fake.calls.clear()
        self.read(fake, NOW + 900)
        self.assertEqual((len(fake.calls), self.state()["walk"]), (1, None))

    def test_an_event_appearing_inside_the_overlap_is_recorded(self):
        fake = FakeGitHub({TOP: http(200, [event(5, "2026-11-01T11:00:00Z")], {"ETag": "a"})})
        self.read(fake)
        fake.routes[TOP] = [event(6, ledger_store.iso(NOW - 60)), event(5, "2026-11-01T11:00:00Z")]
        self.read(fake, NOW + 900)
        self.assertEqual(self.count("activity_events"), 2)

    def test_a_lost_cursor_is_recorded_and_the_walk_restarts(self):
        routes = {TOP: http(200, [event(9, "2026-10-20T00:00:00Z")], page_link("gone"))}
        fake = FakeGitHub(routes)
        self.read(fake)
        self.assertIn("cursor-lost", self.gaps())
        self.assertIsNone(self.state()["walk"]["after"])

    def test_a_failed_read_keeps_the_cursor_and_says_so(self):
        fake = FakeGitHub({TOP: http(200, [event(5, "2026-11-01T10:00:00Z")])})
        self.read(fake)
        held = self.state()["covered_to"]
        fake.routes[TOP] = http(502, {})
        self.read(fake, NOW + 900)
        self.assertEqual((self.state()["covered_to"], self.gaps()), (held, ["read-failed"]))

    def test_a_malformed_row_is_refused_and_counted(self):
        self.read(FakeGitHub({TOP: [event(5, "2026-11-01T10:00:00Z"), dict(event(6, "2026-11-01T10:00:00Z"), after="short")]}))
        self.assertEqual((self.count("activity_events"), self.gaps()), (1, ["row-malformed"]))

    def test_page_coverage_is_recorded_per_repo_and_pass(self):
        self.read(FakeGitHub({TOP: [event(5, "2026-11-01T10:00:00Z")]}))
        kept = self.store.db.execute("SELECT pages, rows_read, rows_new, newest_at, oldest_at, walk_open FROM activity_coverage").fetchone()
        self.assertEqual(tuple(kept), (1, 1, 1, "2026-11-01T10:00:00Z", "2026-11-01T10:00:00Z", 0))

    def test_stopping_on_budget_keeps_what_was_read(self):
        fake = FakeGitHub({TOP: http(200, [event(5, "2026-10-20T00:00:00Z")], page_link("c1"), remaining=999)})
        self.read(fake)
        self.assertEqual((self.count("activity_events"), self.state()["walk"]["after"]), (1, "c1"))


class Lifetimes(unittest.TestCase):
    def test_a_recreated_branch_is_a_new_incarnation(self):
        built = ledger_activity.incarnations([row(1, "2026-10-01T00:00:00Z", before=ZERO, after=sha(1)), row(2, "2026-10-02T00:00:00Z", before=sha(1), after=ZERO),
                                              row(3, "2026-10-03T00:00:00Z", before=ZERO, after=sha(3))])
        self.assertEqual([(i["seq"], i["start_basis"], i["end_basis"]) for i in built], [(1, "branch_creation", "branch_deletion"), (2, "branch_creation", None)])

    def test_a_missing_push_is_a_chain_break(self):
        built = ledger_activity.incarnations([row(1, "2026-10-01T00:00:00Z", before=ZERO, after=sha(1)), row(5, "2026-10-02T00:00:00Z", before=sha(4), after=sha(5))])
        self.assertEqual([e["event_id"] for e in built[0]["breaks"]], [5])

    def test_same_second_events_follow_the_chain_not_the_id(self):
        rows = [row(1, "2026-10-01T00:00:00Z", before=ZERO, after=sha(1)), row(3, "2026-10-02T00:00:00Z", before=sha(1), after=sha(9)), row(2, "2026-10-02T00:00:00Z", before=sha(9), after=sha(8))]
        rows[2]["event_id"], rows[1]["event_id"] = 2, 3
        built = ledger_activity.incarnations(rows)
        self.assertEqual((built[0]["breaks"], built[0]["last_sha"]), ([], sha(8)))

    def test_a_feed_edge_start_is_not_called_a_creation(self):
        self.assertEqual(ledger_activity.incarnations([row(2, "2026-10-01T00:00:00Z")], feed_start=False)[0]["start_basis"], "feed-edge")

    def test_events_after_a_deletion_with_no_creation_are_marked(self):
        built = ledger_activity.incarnations([row(1, "2026-10-01T00:00:00Z", before=sha(7), after=ZERO), row(2, "2026-10-02T00:00:00Z", before=sha(7), after=sha(2))])
        self.assertEqual(built[1]["start_basis"], "no-creation-seen")


class Reconcile(Base):
    def add(self, rows):
        self.read(FakeGitHub({TOP: rows}))

    def test_a_chain_break_is_an_explicit_gap_that_heals(self):
        self.add([event(5, "2026-10-02T00:00:00Z", before=sha(4), after=sha(5)), event(1, "2026-10-01T00:00:00Z", before=ZERO, after=sha(1))])
        ledger_activity.reconcile(self.store, "web", {"refs/heads/b"}, NOW)
        self.assertEqual(self.gaps(), ["chain-break"])
        self.add([event(4, "2026-10-01T12:00:00Z", before=sha(1), after=sha(4))])
        ledger_activity.reconcile(self.store, "web", {"refs/heads/b"}, NOW)
        self.assertEqual(self.gaps(), [])


class HeadAt(unittest.TestCase):
    EVENTS = [row(1, "2026-10-01T00:00:00Z", before=ZERO, after=sha(1)), row(2, "2026-10-01T01:00:00Z", before=sha(1), after=sha(2))]

    def test_the_last_push_strictly_before_the_moment(self):
        self.assertEqual(ledger_activity.head_at(self.EVENTS, "2026-10-01T00:30:00Z")["sha"], sha(1))

    def test_a_push_one_second_after_the_moment_is_not_the_head(self):
        self.assertEqual(ledger_activity.head_at(self.EVENTS, "2026-10-01T00:59:59Z")["sha"], sha(1))

    def test_a_push_in_the_same_second_is_tied_with_both_candidates(self):
        got = ledger_activity.head_at(self.EVENTS, "2026-10-01T01:00:00Z")
        self.assertEqual((got["status"], got["sha"], got["candidates"]), ("tied", None, [sha(1), sha(2)]))

    def test_a_break_around_the_moment_leaves_it_unknown_with_candidates(self):
        events = [self.EVENTS[0], row(5, "2026-10-01T02:00:00Z", before=sha(4), after=sha(5))]
        got = ledger_activity.head_at(events, "2026-10-01T01:00:00Z")
        self.assertEqual((got["status"], got["sha"], got["candidates"]), ("gap", None, [sha(1), sha(4)]))

    def test_a_break_well_before_the_moment_does_not_taint_it(self):
        events = [self.EVENTS[0], row(5, "2026-10-01T02:00:00Z", before=sha(4), after=sha(5)), row(6, "2026-10-01T04:00:00Z", before=sha(5), after=sha(6))]
        self.assertEqual(ledger_activity.head_at(events, "2026-10-01T03:00:00Z")["status"], "exact")

    def test_nothing_before_the_moment_is_unknown(self):
        self.assertEqual(ledger_activity.head_at(self.EVENTS, "2026-09-01T00:00:00Z")["status"], "unknown")


class Coverage(unittest.TestCase):
    STATE = {"covered_from": "2026-10-01T00:00:00Z", "covered_to": "2026-10-10T00:00:00Z", "feed_start": True, "walk": None}

    def test_a_moment_older_than_the_feed_is_before_feed(self):
        self.assertEqual(ledger_activity.coverage_of(self.STATE, "2026-09-01T00:00:00Z"), "before-feed")

    def test_a_moment_newer_than_the_last_read_is_pending(self):
        self.assertEqual(ledger_activity.coverage_of(self.STATE, "2026-10-11T00:00:00Z"), "pending")

    def test_an_open_walk_leaves_its_range_pending(self):
        state = dict(self.STATE, walk={"after": "x", "stop_at": "2026-10-05T00:00:00Z", "top_at": "2026-10-10T00:00:00Z"})
        self.assertEqual(ledger_activity.coverage_of(state, "2026-10-06T00:00:00Z"), "pending")
        self.assertEqual(ledger_activity.coverage_of(state, "2026-10-04T00:00:00Z"), "covered")


if __name__ == "__main__":
    unittest.main()
