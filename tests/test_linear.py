

import datetime
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("linear", os.path.join(os.path.dirname(__file__), "..", "collectors", "linear.py"))
linear = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linear)

NOW = datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc)


def issue(n, t, name, project=None, completed=None, updated="2026-10-20T00:00:00Z", created="2026-08-01T00:00:00Z", labels=(), due=None, priority=0):
    return {"identifier": f"ENG-{n}", "title": f"Issue {n}", "url": f"https://linear.app/example/issue/ENG-{n}",
            "createdAt": created, "updatedAt": updated, "completedAt": completed, "dueDate": due, "priority": priority,
            "state": {"type": t, "name": name}, "project": {"id": project} if project else None,
            "labels": {"nodes": [{"name": l} for l in labels]}}


PROJECTS = [{"id": "p1", "name": "Public launch", "url": "u1", "targetDate": "2026-12-01", "status": {"name": "In Progress", "type": "started"}},
            {"id": "p2", "name": "Ops Center", "url": "u2", "targetDate": None, "status": {"name": "Backlog", "type": "backlog"}},
            {"id": "p3", "name": "Dropped", "url": "u3", "targetDate": None, "status": {"name": "Canceled", "type": "canceled"}},
            {"id": "p4", "name": "Loose project", "url": "u4", "targetDate": None, "state": "started"},
            {"id": "p5", "name": "Demand capture", "url": "u5", "targetDate": "2026-11-20", "status": {"name": "Planned", "type": "planned"}}]
INITIATIVES = [{"id": "i2", "name": "2 · Rebuild", "url": "iu2", "targetDate": None, "status": "Active", "projects": {"nodes": [{"id": "p2"}]}},
               {"id": "i10", "name": "10 · Someday", "url": "iu10", "targetDate": None, "status": "Planned", "projects": {"nodes": []}},
               {"id": "i3", "name": "3 · Launch", "url": "iu3", "targetDate": None, "status": "Active",
                "projects": {"nodes": [{"id": "p1"}, {"id": "p3"}, {"id": "p5"}]}}]


class Stages(unittest.TestCase):
    def test_stages(self):
        self.assertEqual(linear.stage({"type": "started", "name": "In Review"}), "in_review")
        self.assertEqual(linear.stage({"type": "started", "name": "In Progress"}), "in_progress")
        self.assertEqual(linear.stage({"type": "unstarted", "name": "Todo"}), "todo")
        self.assertEqual(linear.stage({"type": "triage", "name": "Triage"}), "backlog")
        self.assertIsNone(linear.stage({"type": "canceled", "name": "Canceled"}))
        self.assertIsNone(linear.stage({"type": "canceled", "name": "Duplicate"}))

    def test_split_name(self):
        self.assertEqual(linear.split_name("3 · Launch"), (3, "Launch"))
        self.assertEqual(linear.split_name("1"), (None, "1"))
        self.assertEqual(linear.split_name("Launch"), (None, "Launch"))


class Rollup(unittest.TestCase):
    def setUp(self):
        issues = [issue(1, "completed", "Done", "p1", "2026-10-22T10:00:00Z"),
                  issue(2, "completed", "Done", "p1", "2026-10-01T10:00:00Z"),
                  issue(3, "started", "In Progress", "p1", updated="2026-10-23T09:00:00Z"),
                  issue(4, "started", "In Review", "p2", updated="2026-10-21T09:00:00Z"),
                  issue(5, "unstarted", "Todo", "p2", labels=["Owner action"]),
                  issue(6, "canceled", "Canceled", "p2"),
                  issue(7, "backlog", "Backlog"),
                  issue(8, "started", "In Progress", "p4"),
                  issue(9, "unstarted", "Todo", "p3"),
                  issue(10, "unstarted", "Todo", "p5", labels=["Owner action"], due="2026-10-30", priority=1)]
        self.s = linear.summarize(issues, PROJECTS, INITIATIVES, NOW)

    def test_totals_are_company_wide(self):
        s = self.s
        self.assertEqual((s["scope"], s["done"], s["in_progress"], s["in_review"], s["todo"], s["backlog"]), (9, 2, 2, 1, 3, 1))
        self.assertEqual(s["done_7d"], 1)
        self.assertEqual(s["unassigned"]["scope"], 1)

    def test_projects_skip_canceled_and_fall_back_to_state(self):
        names = [p["name"] for p in self.s["projects"]]
        self.assertNotIn("Dropped", names)
        loose = next(p for p in self.s["projects"] if p["name"] == "Loose project")
        self.assertEqual(loose["status"], "Started")
        self.assertEqual(self.s["other_project_ids"], ["p4"])

    def test_initiatives_sort_by_number_not_text(self):
        self.assertEqual([x["name"] for x in self.s["initiatives"]], ["2 · Rebuild", "3 · Launch", "10 · Someday"])
        launch = self.s["initiatives"][1]
        self.assertEqual(launch["project_ids"], ["p1", "p5"])

    def test_in_flight_most_recent_first(self):
        self.assertEqual([f["id"] for f in self.s["in_flight"]], ["ENG-3", "ENG-4", "ENG-8"])
        launch = next(p for p in self.s["projects"] if p["name"] == "Public launch")
        self.assertEqual([f["id"] for f in launch["in_flight"]], ["ENG-3"])

    def test_launch_cohort_is_the_initiative_without_canceled_projects(self):
        l = self.s["launch"]
        self.assertEqual(l["source"], "initiative")
        self.assertEqual(l["project_ids"], ["p1", "p5"])
        self.assertEqual(l["target"], "2026-11-20")
        self.assertEqual(l["target_source"], "projects")
        c = l["counts"]
        self.assertEqual((c["scope"], c["done"], c["in_progress"], c["todo"]), (4, 2, 1, 1))
        self.assertEqual(l["forecast"]["scope"], 4)

    def test_owner_actions_keep_undated_and_carry_priority(self):
        o = self.s["owner_actions"]
        self.assertEqual([x["id"] for x in o], ["ENG-10", "ENG-5"])
        self.assertEqual(o[0]["priority"], 1)
        self.assertEqual(o[0]["project_id"], "p5")
        self.assertIsNone(o[1]["due"])
        self.assertEqual(self.s["owner_actions_total"], 2)

    def test_index_carries_project_id(self):
        self.assertEqual(self.s["index"]["ENG-3"], ["Issue 3", "in_progress", "Public launch", "p1"])


class Cohort(unittest.TestCase):
    def cohort(self, *a, **k):
        c, why = linear.launch_cohort(*a, **k)
        self.assertIsNone(why)
        return c

    def error(self, *a, **k):
        c, why = linear.launch_cohort(*a, **k)
        self.assertIsNone(c)
        return why

    def test_override_projects_and_target(self):
        c = self.cohort(PROJECTS, INITIATIVES, {"project_ids": ["p2", "p4"], "target": "2027-01-15"})
        self.assertEqual((c["source"], c["project_ids"], c["target"], c["target_source"]), ("override", ["p2", "p4"], "2027-01-15", "override"))

    def test_override_with_unknown_or_canceled_projects_is_rejected(self):
        self.assertIn("unknown projects: typo", self.error(PROJECTS, INITIATIVES, {"project_ids": ["p1", "typo"]}))
        self.assertIn("canceled projects: Dropped", self.error(PROJECTS, INITIATIVES, {"project_ids": ["p1", "p3"]}))

    def test_override_target_only_keeps_initiative_projects(self):
        c = self.cohort(PROJECTS, INITIATIVES, {"project_ids": None, "target": "2026-12-05"})
        self.assertEqual((c["source"], c["project_ids"], c["target"]), ("initiative", ["p1", "p5"], "2026-12-05"))

    def test_default_target_when_no_project_has_one(self):
        projects = [dict(p, targetDate=None) for p in PROJECTS]
        c = self.cohort(projects, INITIATIVES)
        self.assertEqual((c["target"], c["target_source"]), ("2026-12-01", "default"))

    def test_no_initiative_no_cohort(self):
        self.assertIn("initiative found", self.error(PROJECTS, INITIATIVES[:2]))
        s = linear.summarize([], PROJECTS, INITIATIVES[:2], NOW)
        self.assertIn("error", s["launch"])

    def test_incomplete_or_unfetched_initiative_projects_are_errors(self):
        cut = [dict(INITIATIVES[2], projects={"pageInfo": {"hasNextPage": True}, "nodes": [{"id": "p1"}]})]
        self.assertIn("more projects than were fetched", self.error(PROJECTS, cut))
        ghost = [dict(INITIATIVES[2], projects={"nodes": [{"id": "p1"}, {"id": "gone"}]})]
        self.assertIn("weren't fetched", self.error(PROJECTS, ghost))

    def test_empty_cohort_is_not_done(self):
        only_canceled = [dict(INITIATIVES[2], projects={"nodes": [{"id": "p3"}]})]
        self.assertIn("no projects", self.error(PROJECTS, only_canceled))
        s = linear.summarize([], PROJECTS, INITIATIVES, NOW)
        self.assertEqual(s["launch"]["error"], "the launch projects have no issues")
        self.assertNotIn("forecast", s["launch"])

    def test_unreadable_override_is_an_error_not_ignored(self):
        s = linear.summarize([], PROJECTS, INITIATIVES, NOW, override_error="launch.json unreadable: x")
        self.assertEqual(s["launch"], {"error": "launch.json unreadable: x"})

    def test_version_follows_the_project_set(self):
        a = self.cohort(PROJECTS, INITIATIVES)
        b = self.cohort(PROJECTS, INITIATIVES, {"project_ids": ["p5", "p1"], "target": "2027-01-01"})
        self.assertEqual(a["version"], b["version"])

    def test_read_override_validates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "launch.json")
            self.assertEqual(linear.read_override(path), (None, None))
            with open(path, "w") as f:
                json.dump({"project_ids": ["p1"], "target": "Dec 1"}, f)
            self.assertEqual(linear.read_override(path)[0], None)
            self.assertIn("YYYY-MM-DD", linear.read_override(path)[1])
            with open(path, "w") as f:
                json.dump({"project_ids": ["p1"], "target": "2026-12-01"}, f)
            self.assertEqual(linear.read_override(path), ({"project_ids": ["p1"], "target": "2026-12-01"}, None))


class Inventory(unittest.TestCase):
    def test_fetch_all_follows_pages_and_falls_back(self):
        pages = {None: {"nodes": [1, 2], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
                 "c1": {"nodes": [3], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        calls = []

        def fake(key, q, variables=None):
            calls.append(q)
            if q == "rich":
                raise linear.LinearError("no such field")
            return {"things": pages[(variables or {}).get("after")]}
        orig = linear.gql
        linear.gql = fake
        try:
            self.assertEqual(linear.fetch_all("k", ["rich", "plain"], "things", "things"), [1, 2, 3])
            with self.assertRaises(linear.LinearError):
                linear.fetch_all("k", ["rich"], "things", "things")
        finally:
            linear.gql = orig


class Pagination(unittest.TestCase):
    def page(self, field, nodes, more=False, cursor=None):
        return {field: {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}

    def test_nonadvancing_cursors_rejected_before_repeating_a_request(self):
        cases = [("missing", [None], [None]), ("empty", [""], [None]),
                 ("number", [12], [None]), ("object", [{}], [None]),
                 ("repeat", ["c1", "c1"], [None, "c1"]),
                 ("cycle", ["c1", "c2", "c1"], [None, "c1", "c2"])]
        for field in ("issues", "projects", "initiatives"):
            for name, cursors, expected in cases:
                with self.subTest(field=field, case=name):
                    calls = []

                    def fake(key, query, variables):
                        calls.append(variables["after"])
                        index = len(calls) - 1


                        return self.page(field, ["duplicate"], index < len(cursors),
                                         cursors[index] if index < len(cursors) else None)

                    with mock.patch.object(linear, "gql", side_effect=fake):
                        with self.assertRaisesRegex(linear.LinearError, "pagination.*cursor"):
                            linear.fetch_all("fixture", ["query"], field, field)
                    self.assertEqual(calls, expected)

    def test_absent_end_cursor_is_a_pagination_error(self):
        page = self.page("projects", ["partial"], True)
        del page["projects"]["pageInfo"]["endCursor"]
        with mock.patch.object(linear, "gql", return_value=page) as gql:
            with self.assertRaises(Exception) as raised:
                linear.fetch_all("fixture", ["query"], "projects", "projects")
        self.assertIsInstance(raised.exception, linear.LinearError)
        self.assertRegex(str(raised.exception), "pagination.*cursor")
        self.assertEqual(gql.call_count, 1)

    def test_fallback_restarts_inventory_and_cursor_tracking(self):
        calls = []

        def fake(key, query, variables):
            after = variables["after"]
            calls.append((query, after))
            if query == "rich":

                rich_count = sum(q == "rich" for q, _ in calls)
                return self.page("projects", ["discard"], rich_count < 3, "c1")
            return self.page("projects", ["a"] if after is None else ["b"], after is None, "c1")

        with mock.patch.object(linear, "gql", side_effect=fake):
            result = linear.fetch_all("fixture", ["rich", "plain"], "projects", "projects")
        self.assertEqual(result, ["a", "b"])
        self.assertEqual(calls, [("rich", None), ("rich", "c1"), ("plain", None), ("plain", "c1")])

    def test_terminal_and_empty_pages_need_no_next_cursor(self):
        for nodes in ([], ["only"]):
            with self.subTest(nodes=nodes), mock.patch.object(
                    linear, "gql", return_value=self.page("projects", nodes)) as gql:
                self.assertEqual(linear.fetch_all("fixture", ["query"], "projects", "projects"), nodes)
                self.assertEqual(gql.call_count, 1)

    def test_fetch_uses_whole_paginated_inventories(self):
        calls = []
        issues = [issue(1, "started", "In Progress", "p1"), issue(2, "unstarted", "Todo", "p1")]

        def fake(key, query, variables):
            after = variables["after"]
            calls.append((query, after))
            if query == linear.ISSUES:
                return self.page("issues", issues[:1] if after is None else issues[1:], after is None, "i1")
            if query in linear.PROJECTS:
                return self.page("projects", PROJECTS[:2] if after is None else PROJECTS[2:], after is None, "p1")
            return self.page("initiatives", INITIATIVES[:2] if after is None else INITIATIVES[2:], after is None, "n1")

        with mock.patch.object(linear, "gql", side_effect=fake):
            result = linear.fetch("fixture")
        self.assertEqual((result["scope"], result["launch"]["counts"]["scope"]), (2, 2))
        self.assertEqual(set(result["index"]), {"ENG-1", "ENG-2"})
        self.assertEqual([after for _, after in calls], [None, "i1", None, "p1", None, "n1"])

    def test_a_repeated_issue_counts_once(self):
        one = issue(1, "unstarted", "Todo")
        out = linear.summarize([one, dict(one)], [], [])
        self.assertEqual((out["scope"], out["todo"], len(out["index"])), (1, 1, 1))

    def test_conflicting_copies_of_an_issue_are_refused(self):
        one = issue(1, "unstarted", "Todo")
        with self.assertRaises(linear.LinearError):
            linear.summarize([one, dict(one, title="Other")], [], [])

    def test_issue_cursor_failure_serves_stale_or_error_without_saving(self):
        for cached in (None, {"scope": 99, "fetched": 123}):
            with self.subTest(cached=cached):
                calls = []

                def fake(key, query, variables):
                    if query == linear.ISSUES:
                        calls.append(variables["after"])
                        return self.page("issues", [issue(1, "started", "In Progress", "p1")],
                                         len(calls) < 3, "c1")
                    if query in linear.PROJECTS:
                        return self.page("projects", PROJECTS)
                    return self.page("initiatives", INITIATIVES)

                output = io.StringIO()
                with mock.patch.object(linear, "load", side_effect=lambda path: cached if path == linear.CACHE else []), \
                        mock.patch.object(linear.os.path, "getmtime", return_value=0), \
                        mock.patch.object(linear, "key", return_value="fixture"), \
                        mock.patch.object(linear, "read_override", return_value=(None, None)), \
                        mock.patch.object(linear, "gql", side_effect=fake), \
                        mock.patch.object(linear, "save") as save, contextlib.redirect_stdout(output):
                    linear.main()
                result = json.loads(output.getvalue())
                self.assertIn("pagination", result.get("stale_reason" if cached else "error", ""))
                if cached:
                    self.assertEqual((result["scope"], result["fetched"]), (99, 123))
                else:
                    self.assertNotIn("scope", result)
                save.assert_not_called()
                self.assertEqual(calls, [None, "c1"])


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 10, 24, tzinfo=datetime.timezone.utc)
        self.old = self.now - datetime.timedelta(days=60)

    def rows(self, done_per_day, remaining):
        rows = []
        for d in range(28):
            t = self.now - datetime.timedelta(days=27 - d, hours=1)
            rows += [(self.old, True, t)] * done_per_day
        rows += [(self.old, False, None)] * remaining
        return rows

    def test_steady_pace_projects_remaining_over_rate(self):
        f = linear.forecast(self.rows(2, 40), "2026-12-01", self.now)
        self.assertFalse(f["insufficient"])
        self.assertEqual(f["p50"], "2026-11-13")
        self.assertEqual(f["prob_by_target"], 100)

    def test_thin_history_refuses_to_guess(self):
        f = linear.forecast([(None, True, self.now)] * 3 + [(None, False, None)] * 10, "2026-12-01", self.now)
        self.assertTrue(f["insufficient"])
        self.assertNotIn("p50", f)

    def test_one_bulk_day_is_not_enough_history(self):
        bulk = [(self.old, True, self.now - datetime.timedelta(hours=3))] * 40
        f = linear.forecast(bulk + [(self.old, False, None)] * 10, "2026-12-01", self.now)
        self.assertEqual(f["active_days"], 1)
        self.assertTrue(f["insufficient"])

    def test_undated_completions_are_excluded_from_pace(self):
        rows = [(self.old, True, None)] * 30 + [(self.old, False, None)] * 10
        f = linear.forecast(rows, "2026-12-01", self.now)
        self.assertEqual((f["per_week"], f["undated_done"], f["done"]), (0, 30, 30))
        self.assertTrue(f["insufficient"])
        self.assertEqual(f["burnup"][-1]["done"], 30)
        self.assertEqual(f["burnup"][-2]["done"], 0)

    def test_future_completion_is_not_pace(self):
        rows = self.rows(1, 5) + [(self.old, True, self.now + datetime.timedelta(days=2))]
        self.assertEqual(linear.forecast(rows, None, self.now)["undated_done"], 1)

    def test_censored_runs_report_beyond_horizon(self):
        slow = [(self.old, True, self.now - datetime.timedelta(days=3 * i + 1)) for i in range(8)]
        f = linear.forecast(slow + [(self.old, False, None)] * 452, "2029-01-01", self.now)
        self.assertFalse(f["insufficient"])
        self.assertIsNone(f["p50"])
        self.assertIsNone(f["p85"])
        self.assertEqual(f["prob_by_target"], 0)

    def test_far_target_extends_the_simulation(self):
        rows = [(self.old, True, self.now - datetime.timedelta(days=d)) for d in range(1, 9)] + [(None, False, None)] * 30
        f = linear.forecast(rows, "2027-12-01", self.now, runs=100, horizon=2)
        self.assertEqual(f["horizon_days"], (datetime.date(2027, 12, 1) - self.now.date()).days)
        self.assertGreater(f["prob_by_target"], 90)
        self.assertNotIn("prob_note", f)

    def test_target_beyond_any_horizon_gets_no_chance(self):
        slow = [(self.old, True, self.now - datetime.timedelta(days=3 * i + 1)) for i in range(8)]
        f = linear.forecast(slow + [(self.old, False, None)] * 50, "2040-01-01", self.now, runs=50)
        self.assertNotIn("prob_by_target", f)
        self.assertEqual(f["prob_note"], "target beyond the simulation horizon")

    def test_burnup_counts_done_and_scope_by_week(self):
        f = linear.forecast(self.rows(1, 5), None, self.now)
        self.assertEqual(f["burnup"][-1], {"t": "2026-10-24", "done": 28, "scope": 33})
        self.assertTrue(f["burnup_reconstructed"])


class History(unittest.TestCase):
    def launch(self, done, scope, p50, version="v1"):
        return {"version": version, "forecast": {"done": done, "scope": scope, "p50": p50}}

    def test_no_drift_without_a_week_old_snapshot(self):
        l, hist = linear.with_history(self.launch(10, 100, "2027-01-10"), [{"date": "2026-10-20", "cohort": "v1", "done": 5, "scope": 90, "p50": "2027-01-01"}], "2026-10-23")
        self.assertNotIn("drift", l)
        self.assertEqual([h["date"] for h in hist], ["2026-10-20", "2026-10-23"])

    def test_drift_against_same_cohort(self):
        hist = [{"date": "2026-10-10", "cohort": "v1", "done": 5, "scope": 90, "p50": "2027-01-01"},
                {"date": "2026-10-14", "cohort": "v0", "done": 1, "scope": 10, "p50": "2026-11-01"}]
        l, hist = linear.with_history(self.launch(10, 100, "2027-01-10"), hist, "2026-10-23")
        self.assertEqual(l["drift"], {"since": "2026-10-10", "scope_change": 10, "done_change": 5, "p50_days": 9})

    def test_today_is_replaced_not_duplicated(self):
        _, hist = linear.with_history(self.launch(10, 100, None), [{"date": "2026-10-23", "cohort": "v1", "done": 9, "scope": 99, "p50": None}], "2026-10-23")
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["done"], 10)


class Cache(unittest.TestCase):
    def serve(self, fetched, mtime_offset):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "linear.json")
            with open(path, "w") as f:
                json.dump({"fetched": fetched(time.time()), "scope": 0}, f)
            os.utime(path, (time.time() + mtime_offset, time.time() + mtime_offset))
            with mock.patch.object(linear, "CACHE", path), mock.patch.object(linear, "key", return_value=None) as asked, contextlib.redirect_stdout(io.StringIO()):
                linear.main()
            return asked.call_count

    def test_a_current_cache_is_served(self):
        self.assertEqual(self.serve(lambda now: now - 60, -60), 0)

    def test_a_future_modification_time_cannot_hold_a_refresh(self):
        self.assertEqual(self.serve(lambda now: 1000, 86400), 1)

    def test_a_recent_file_with_an_old_or_future_payload_clock_is_refreshed(self):
        self.assertEqual(self.serve(lambda now: 1000, -60), 1)
        self.assertEqual(self.serve(lambda now: now + 3600, -60), 1)


if __name__ == "__main__":
    unittest.main()
