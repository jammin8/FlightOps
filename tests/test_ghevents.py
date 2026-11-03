
import json
import os
import tempfile
import unittest
from unittest import mock

from gh_samples import iso, load

ev = load("ghevents")
T = 1792900000.0


def event(i, t, kind="PushEvent", **payload):
    pl = {"ref": "refs/heads/feat", "before": "0" * 40, "head": f"{i:040d}", "push_id": i} if kind == "PushEvent" else payload
    return {"id": str(i), "type": kind, "created_at": iso(t), "actor": {"login": "someone"}, "payload": pl}


def opened(i, t, number, sha):
    return event(i, t, "PullRequestEvent", action="opened", number=number,
                 pull_request={"number": number, "head": {"sha": sha, "ref": "feat"}, "base": {"ref": "main"}})


class Feed:


    def __init__(self, events, interval=60):
        self.events, self.interval, self.calls = list(events), interval, []

    def etag(self):
        return f'"{self.events[0]["id"] if self.events else "empty"}"'

    def __call__(self, repo, page, etag=None):
        self.calls.append((repo, page, etag))
        if page == 1 and etag and etag == self.etag():
            return 304, {"etag": self.etag(), "x-poll-interval": str(self.interval)}, None
        body = self.events[(page - 1) * 100:page * 100]
        more = len(self.events) > page * 100 and page < 3
        return 200, {"etag": self.etag(), "x-poll-interval": str(self.interval), "link": 'rel="next"' if more else ""}, body


class Poll(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def poll(self, feeds, now):

        return ev.poll(now, self.d, get=lambda repo, page, etag=None: feeds.get(repo, Feed([]))(repo, page, etag))

    def state(self):
        return ev.load_state(self.d)["repos"]

    def test_the_first_poll_reads_the_whole_feed_and_covers_from_its_oldest_event(self):
        feed = Feed([event(1000 - i, T - 60 * i) for i in range(250)])
        self.poll({"backend": feed}, T)
        self.assertEqual([c[1] for c in feed.calls], [1, 2, 3])
        self.assertEqual(self.state()["backend"]["coverage"], [[T - 60 * 249, T]])
        self.assertEqual(len(ev.load_receipts(self.d)["backend"]["pushes"]), 250)

    def test_a_304_extends_coverage_and_isn_t_counted(self):
        feed = Feed([event(5, T - 100)])
        self.poll({"backend": feed}, T)
        s = self.poll({"backend": feed}, T + 300)
        self.assertEqual(self.state()["backend"]["coverage"], [[T - 100, T + 300]])
        self.assertEqual(feed.calls[-1], ("backend", 1, '"5"'))
        self.assertEqual(s["repos"]["backend"]["last"], "not modified")
        self.assertEqual(s["counted_last_hour"], 3)

    def test_a_page_reaching_a_seen_event_is_continuous(self):
        feed = Feed([event(5, T - 100)])
        self.poll({"backend": feed}, T)
        feed.events = [event(7, T + 200), event(6, T + 100)] + feed.events
        self.poll({"backend": feed}, T + 300)
        st = self.state()["backend"]
        self.assertEqual((st["coverage"], st.get("gaps", [])), ([[T - 100, T + 300]], []))
        self.assertEqual(len(ev.load_receipts(self.d)["backend"]["pushes"]), 3)
        self.assertEqual(len(feed.calls), 2)

    def test_more_new_events_than_three_pages_is_a_gap(self):
        feed = Feed([event(5, T - 100)])
        self.poll({"backend": feed}, T)
        feed.events = [event(10000 - i, T + 290 - i // 2) for i in range(400)] + feed.events
        self.poll({"backend": feed}, T + 300)
        st = self.state()["backend"]
        oldest_read = T + 290 - 299 // 2
        self.assertEqual(st["coverage"], [[T - 100, T], [oldest_read, T + 300]])
        self.assertEqual(st["gaps"], [{"after": T, "before": oldest_read, "detected": T + 300}])

    def test_not_due_before_five_minutes_or_the_poll_interval(self):
        feed = Feed([event(5, T - 100)], interval=600)
        self.poll({"backend": feed}, T)
        self.assertEqual(self.poll({"backend": feed}, T + 299)["repos"]["backend"]["last"], "not due")
        self.assertEqual(self.poll({"backend": feed}, T + 400)["repos"]["backend"]["last"], "not due")
        self.assertEqual(self.poll({"backend": feed}, T + 600)["repos"]["backend"]["last"], "not modified")

    def test_the_hourly_cap(self):
        st = ev.load_state(self.d)
        st["counted"] = [T - 10] * ev.CAP
        os.makedirs(self.d, exist_ok=True)
        ev.save_state(st, self.d)
        s = self.poll({"backend": Feed([event(5, T)])}, T)
        self.assertEqual(s["repos"]["backend"]["last"], "deferred (hourly cap)")
        self.assertEqual(s["counted_last_hour"], ev.CAP)

    def test_running_out_of_budget_mid_poll_is_a_gap(self):
        feed = Feed([event(5, T - 100)])
        self.poll({"backend": feed}, T)
        st = ev.load_state(self.d)
        st["counted"] = [T + 100] * (ev.CAP - 1)
        ev.save_state(st, self.d)
        feed.events = [event(10000 - i, T + 290 - i) for i in range(150)] + feed.events
        self.poll({"backend": feed}, T + 300)
        self.assertEqual(len(self.state()["backend"]["gaps"]), 1)

    def test_the_etag_is_kept_only_when_the_whole_poll_succeeded(self):
        feed = Feed([event(5, T - 100)])
        self.poll({"backend": feed}, T)
        feed.events = [event(10000 - i, T + 290 - i) for i in range(150)] + feed.events
        broken = lambda repo, page, etag=None: (502, {}, None) if page == 2 else feed(repo, page, etag)
        ev.poll(T + 300, self.d, get=lambda repo, page, etag=None: broken(repo, page, etag) if repo == "backend" else Feed([])(repo, page, etag))
        st = self.state()["backend"]
        self.assertEqual((st["etag"], st["error"], st["coverage"]), ('"5"', "HTTP 502", [[T - 100, T]]))

    def test_receipts_are_idempotent_and_kept_only_for_opens_and_pushes(self):
        feed = Feed([opened(9, T - 50, 12, "a" * 40), event(8, T - 60, "IssueCommentEvent"), event(7, T - 70)])
        self.poll({"web": feed}, T)
        st = ev.load_state(self.d)
        st["repos"]["web"].update(newest_id="6", seen=[], polled=0, etag=None)
        ev.save_state(st, self.d)
        self.poll({"web": feed}, T + 300)
        with open(os.path.join(self.d, "receipts.jsonl")) as f:
            lines = [json.loads(l) for l in f]
        self.assertEqual(sorted(l["id"] for l in lines), ["7", "9"])
        r = ev.load_receipts(self.d)["web"]
        self.assertEqual(r["opened"], {12: [{"sha": "a" * 40, "t": T - 50, "id": "9"}]})


class TornReceipts(unittest.TestCase):
    def test_a_torn_line_keeps_the_valid_receipts_and_is_quarantined_by_the_next_poll(self):
        d = tempfile.mkdtemp()
        feed = Feed([event(1000 - i, T - 60 * i) for i in range(2)])
        ev.poll(T, d, get=lambda repo, page, etag=None: (feed if repo == "backend" else Feed([]))(repo, page, etag))
        with open(os.path.join(d, "receipts.jsonl"), "a") as f:
            f.write('{"id":')
        r = ev.load_receipts(d)["backend"]
        self.assertEqual((len(r["pushes"]), r["coverage"]), (2, []))
        ev.poll(T + 600, d, get=lambda repo, page, etag=None: Feed([])(repo, page, etag))
        r = ev.load_receipts(d)["backend"]
        self.assertEqual(len(r["pushes"]), 2)
        self.assertTrue(r["coverage"])
        with open(os.path.join(d, "receipts.corrupt.jsonl")) as f:
            self.assertEqual(f.read(), '{"id":\n')


class Budget(unittest.TestCase):


    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_timed_out_pages_stay_counted_and_the_hour_never_exceeds_the_cap(self):
        sent = []
        n = [0]

        def get(repo, page, etag=None):
            sent.append((repo, page))
            n[0] += 1
            if repo == "backend" and page == 2:
                raise ev.subprocess.TimeoutExpired("gh", 30)

            body = [event(10 ** 6 * n[0] + i, T - i) for i in range(100 if repo == "backend" else 1)]
            return 200, {"etag": f'"{n[0]}"', "link": 'rel="next"' if repo == "backend" else ""}, body

        for i in range(12):
            ev.poll(T + 300 * i, self.d, get=get)
        st = ev.load_state(self.d)
        self.assertEqual(len(sent), ev.CAP)
        self.assertEqual(len([t for t in st["counted"] if t > T + 3300 - 3600]), len(sent))

    def test_a_reservation_survives_a_process_that_dies_mid_request(self):
        def dies(repo, page, etag=None):
            raise RuntimeError("killed")
        with self.assertRaises(RuntimeError):
            ev.poll(T, self.d, get=dies)
        self.assertEqual(ev.load_state(self.d)["counted"], [T])

    def test_a_304_is_refunded_only_when_confirmed(self):
        feed = Feed([event(5, T - 100)])
        ev.poll(T, self.d, get=lambda repo, page, etag=None: feed(repo, page, etag) if repo == "backend" else Feed([])(repo, page, etag))
        ev.poll(T + 300, self.d, get=lambda repo, page, etag=None: feed(repo, page, etag) if repo == "backend" else (500, {}, None))
        self.assertEqual(len(ev.load_state(self.d)["counted"]), 3 + 2)


class LateEvents(unittest.TestCase):


    def test_an_event_that_appears_below_one_already_seen_is_kept(self):
        d = tempfile.mkdtemp()
        feed = Feed([event(10, T - 10), event(8, T - 30)])
        ev.poll(T, d, get=lambda repo, page, etag=None: feed(repo, page, etag) if repo == "backend" else Feed([])(repo, page, etag))
        feed.events = [event(10, T - 10), event(9, T - 20), event(8, T - 30)]
        feed.etag = lambda: '"late"'
        ev.poll(T + 300, d, get=lambda repo, page, etag=None: feed(repo, page, etag) if repo == "backend" else Feed([])(repo, page, etag))
        ids = sorted(int(x["id"]) for x in ev.load_receipts(d)["backend"]["pushes"])
        self.assertEqual(ids, [8, 9, 10])
        self.assertEqual(ev.load_state(d)["repos"]["backend"].get("gaps", []), [])


class Durability(unittest.TestCase):



    CHILD = """
import json, os, signal, sys
sys.path.insert(0, sys.argv[1])
import ghevents as ev
def get(repo, page, etag=None):
    if repo == "backend":
        return 200, {"etag": '"b"'}, [{"id": "9", "type": "PullRequestEvent", "created_at": "2026-10-25T00:00:00Z", "actor": {},
                                        "payload": {"action": "opened", "number": 12,
                                                    "pull_request": {"head": {"sha": "a" * 40, "ref": "feat"}, "base": {"ref": "main"}}}}]
    os.kill(os.getpid(), signal.SIGKILL)   # killed during the next repository's request
ev.poll(1792900000.0, sys.argv[2], get=get)
"""

    def test_a_process_killed_mid_poll_never_acknowledges_an_unstored_receipt(self):
        import subprocess, sys
        d = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, "-c", self.CHILD, os.path.dirname(ev.__file__), d])
        self.assertNotEqual(r.returncode, 0)
        st = ev.load_state(d)
        stored = set()
        if os.path.exists(os.path.join(d, "receipts.jsonl")):
            with open(os.path.join(d, "receipts.jsonl")) as f:
                stored = {json.loads(l)["id"] for l in f if l.strip()}
        acknowledged = set((st["repos"].get("backend") or {}).get("seen") or [])
        self.assertTrue(acknowledged <= stored, (acknowledged, stored))
        self.assertEqual(stored, {"9"})
        self.assertEqual(len(st["counted"]), 2)

    def test_state_from_before_seen_ids_is_migrated_by_stored_receipts(self):
        d = tempfile.mkdtemp()
        os.makedirs(d, exist_ok=True)
        ev.save_state({"repos": {"backend": {"newest_id": "10", "etag": '"old"', "coverage": [[T - 1000, T - 100]]}}, "counted": []}, d)
        feed = Feed([event(10, T - 10), opened(9, T - 20, 12, "a" * 40), event(8, T - 30)])
        ev.poll(T, d, get=lambda repo, page, etag=None: feed(repo, page, etag) if repo == "backend" else Feed([])(repo, page, etag))
        self.assertEqual(ev.load_receipts(d)["backend"]["opened"], {12: [{"sha": "a" * 40, "t": T - 20, "id": "9"}]})
        st = ev.load_state(d)["repos"]["backend"]
        self.assertEqual((st.get("gaps", []), st["coverage"]), ([], [[T - 1000, T]]))


class Fetch(unittest.TestCase):
    def run_gh(self, stdout, code=0):
        return mock.Mock(stdout=stdout, stderr="", returncode=code)

    def test_a_200_with_headers(self):
        out = 'HTTP/2.0 200 OK\r\nEtag: W/"abc"\r\nX-Poll-Interval: 60\r\nLink: <x>; rel="next"\r\n\r\n[{"id": "1"}]'
        with mock.patch.object(ev.subprocess, "run", return_value=self.run_gh(out)) as run:
            status, headers, body = ev.fetch("backend", 1, 'W/"old"')
        self.assertEqual((status, headers["etag"], headers["x-poll-interval"], body), (200, 'W/"abc"', "60", [{"id": "1"}]))
        self.assertIn("If-None-Match: W/\"old\"", run.call_args[0][0])

    def test_a_304_exits_non_zero_but_is_read_from_the_status_line(self):
        out = 'HTTP/2.0 304 Not Modified\r\nEtag: "abc"\r\n\r\n'
        with mock.patch.object(ev.subprocess, "run", return_value=self.run_gh(out, 1)):
            self.assertEqual(ev.fetch("backend", 1, '"abc"')[0], 304)

    def test_no_answer(self):
        with mock.patch.object(ev.subprocess, "run", return_value=mock.Mock(stdout="", stderr="gh: offline", returncode=1)):
            self.assertEqual(ev.fetch("backend", 1), (0, {}, "gh: offline"))


class ReceiptDamage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name
        self.good = {"id": "10", "repo": "backend", "type": "PushEvent", "ref": "refs/heads/feat", "head": "a" * 40, "before": "0" * 40, "t": T, "at": iso(T)}

    def write(self, *lines):
        with open(os.path.join(self.d, "receipts.jsonl"), "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def coverage(self, cover):
        ev.save_state({"repos": {repo: {"coverage": [list(c) for c in cover]} for repo in ev.REPOS}, "counted": []}, self.d)

    def test_a_row_missing_its_type_fields_is_quarantined_and_cannot_stop_facts(self):
        for row in ({"id": "1", "type": "PushEvent", "repo": "backend"},
                    {"id": "2", "type": "PushEvent", "repo": "backend", "ref": "r", "before": None, "head": "h"},
                    {"id": "3", "type": "PullRequestEvent", "repo": "backend", "t": T, "action": "opened"},
                    {"id": "4", "type": "PullRequestEvent", "repo": "backend", "t": T, "action": "edited", "number": 1},
                    {"id": "5", "type": "IssuesEvent", "repo": "backend", "t": T},
                    {"id": "6", "type": "PushEvent", "repo": "nowhere", "t": T, "ref": "r", "before": None, "head": "h"}):
            self.write(json.dumps(self.good), json.dumps(row))
            ev.repair_receipts(self.d)
            self.assertEqual([x["id"] for x in ev.load_receipts(self.d)["backend"]["pushes"]], ["10"], row)
            with open(os.path.join(self.d, "receipts.corrupt.jsonl")) as f:
                self.assertIn(row["id"], [json.loads(line)["id"] for line in f])

    def test_an_undated_damaged_line_leaves_no_coverage_anywhere(self):
        self.coverage([[T, T + 200]])
        self.write(json.dumps(self.good), '{"id":"11","head":"bbb"')
        self.assertEqual(ev.load_receipts(self.d)["backend"]["coverage"], [])
        ev.repair_receipts(self.d, now=T + 300)
        loaded = ev.load_receipts(self.d)
        self.assertEqual([loaded[repo]["coverage"] for repo in ev.REPOS], [[], [], []])
        self.assertEqual(ev.load_state(self.d)["repos"]["backend"]["gaps"][-1]["detected"], T + 300)

    def test_a_dated_damaged_line_splits_only_its_own_repository(self):
        self.coverage([[T, T + 200]])
        self.write(json.dumps({"id": "11", "repo": "backend", "type": "PushEvent", "t": T + 100}))
        ev.repair_receipts(self.d, now=T + 300)
        loaded = ev.load_receipts(self.d)
        self.assertEqual(loaded["backend"]["coverage"], [[T, T + 99], [T + 101, T + 200]])
        self.assertEqual(loaded["web"]["coverage"], [[T, T + 200]])
        self.assertEqual(ev.merge(loaded["backend"]["coverage"], 0), loaded["backend"]["coverage"])

    def test_the_marker_survives_a_second_repair_and_a_poll(self):
        self.coverage([[T, T + 200]])
        self.write('{"id":"11"')
        ev.repair_receipts(self.d, now=T + 300)
        ev.repair_receipts(self.d, now=T + 301)
        ev.poll(T + 400, self.d, Feed([event(1, T + 350)]))
        self.assertEqual(ev.load_receipts(self.d)["backend"]["coverage"], [[T + 350, T + 400]])

    def test_a_crash_before_replacing_the_file_loses_no_marker(self):
        self.coverage([[T, T + 200]])
        self.write(json.dumps(self.good), '{"id":"11"')
        real = os.replace

        def flaky(source, target):
            if target.endswith("receipts.jsonl"):
                raise OSError("disk")
            real(source, target)
        with mock.patch.object(ev.os, "replace", flaky):
            with self.assertRaises(OSError):
                ev.repair_receipts(self.d, now=T + 300)
        self.assertEqual(ev.load_state(self.d)["repos"]["backend"]["coverage"], [])


class Retention(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name
        self.now = T + 200 * 86400

    def row(self, i, t, **extra):
        return {"id": str(i), "repo": "backend", "type": "PushEvent", "ref": "refs/heads/feat", "before": None, "head": f"{i:040d}", "t": t} | extra

    def write(self, *rows):
        with open(os.path.join(self.d, "receipts.jsonl"), "w") as f:
            f.write("".join(json.dumps(row) + "\n" for row in rows))

    def test_old_receipts_move_to_the_archive_and_are_never_deleted(self):
        old, fresh = self.row(1, self.now - ev.KEEP - 86400), self.row(2, self.now - 86400)
        self.write(old, fresh)
        self.assertEqual(ev.archive_receipts(self.d, self.now), 1)
        self.assertEqual([x["id"] for x in ev.parse_receipts(self.d)[0]], ["2"])
        self.assertEqual([x["id"] for x in ev.load_receipts(self.d)["backend"]["pushes"]], ["2"])
        self.assertEqual([x["id"] for x in ev.load_receipts(self.d, archived=True)["backend"]["pushes"]], ["1", "2"])

    def test_rows_a_stored_pr_still_needs_stay_hot(self):
        push = self.row(1, self.now - ev.KEEP - 86400)
        opened_row = {"id": "2", "repo": "backend", "type": "PullRequestEvent", "action": "opened", "number": 7, "head_sha": "a", "t": push["t"]}
        other = self.row(3, push["t"], ref="refs/heads/other")
        self.write(push, opened_row, other)
        hold = {"backend": {"numbers": {7}, "refs": {"refs/heads/feat"}}}
        self.assertEqual(ev.archive_receipts(self.d, self.now, hold), 1)
        self.assertEqual({x["id"] for x in ev.parse_receipts(self.d)[0]}, {"1", "2"})

    def test_a_crash_after_archiving_repeats_nothing_in_the_merged_view(self):
        old = self.row(1, self.now - ev.KEEP - 86400)
        self.write(old)
        ev.archive_receipts(self.d, self.now)
        self.write(old)
        ev.archive_receipts(self.d, self.now)
        self.assertEqual(len(ev.load_receipts(self.d, archived=True)["backend"]["pushes"]), 1)

    def test_nothing_old_means_no_rewrite(self):
        self.write(self.row(1, self.now))
        self.assertEqual(ev.archive_receipts(self.d, self.now), 0)
        self.assertFalse(os.path.exists(os.path.join(self.d, "archive")))


if __name__ == "__main__":
    unittest.main()
