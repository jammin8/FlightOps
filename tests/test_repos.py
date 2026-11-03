
import datetime
import time
import tempfile
import json
import io
import contextlib
import importlib.util
import os
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("repos", os.path.join(os.path.dirname(__file__), "..", "collectors", "repos.py"))
repos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repos)


def run(name, conclusion=None, started="2026-10-23T10:00:00Z", completed="2026-10-23T10:10:00Z", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "startedAt": started, "completedAt": completed if status == "COMPLETED" else None}


def roll(*nodes):
    return {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": list(nodes)}}


def ci(*nodes):
    return repos.ci_of(repos.latest_attempts(roll(*nodes)))


class Attempts(unittest.TestCase):
    def test_successful_rerun_clears_earlier_failure(self):
        self.assertEqual(ci(run("test", "FAILURE", "2026-10-23T09:00:00Z", "2026-10-23T09:10:00Z"),
                            run("test", "SUCCESS", "2026-10-23T10:00:00Z", "2026-10-23T10:10:00Z")), ("passed", 0, False))

    def test_failed_rerun_after_success_fails(self):
        self.assertEqual(ci(run("test", "SUCCESS", "2026-10-23T09:00:00Z"), run("test", "FAILURE", "2026-10-23T10:00:00Z"))[0], "failing")

    def test_running_never_clears_a_known_failure(self):
        self.assertEqual(ci(run("lint", "FAILURE"), run("test", status="IN_PROGRESS")), ("failing", 1, True))

    def test_queued_rerun_is_newest(self):
        self.assertEqual(ci(run("test", "FAILURE"), run("test", started=None, status="QUEUED")), ("running", 0, True))

    def test_cancelled_only_when_nothing_else_ran(self):
        self.assertEqual(ci(run("test", "CANCELLED", "2026-10-23T11:00:00Z"), run("test", "SUCCESS")), ("passed", 0, False))
        self.assertEqual(ci(run("test", "CANCELLED")), ("cancelled", 0, False))

    def test_no_checks_is_none_not_cancelled(self):
        self.assertEqual(ci(), ("none", 0, False))
        self.assertEqual(ci(run("review-gate", "SUCCESS")), ("none", 0, False))

    def test_missing_connection_metadata_never_proves_ci(self):
        failing = {"state": "FAILURE", "contexts": {"nodes": [{"__typename": "CheckRun", "name": "fast", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}
        self.assertFalse(repos.complete(failing))
        self.assertEqual(repos.ci_of(repos.latest_attempts(failing), failing["state"], repos.complete(failing))[0], "failing")
        for roll in ({"contexts": {"pageInfo": {"hasNextPage": False}}}, {"contexts": {"pageInfo": {}, "nodes": []}}, {"contexts": None}, {}):
            self.assertFalse(repos.complete(roll), roll)
        self.assertTrue(repos.complete({"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": []}}))

    def test_missing_merged_page_info_leaves_the_day_count_unknown(self):
        now = datetime.datetime(2026, 10, 25, tzinfo=datetime.timezone.utc)
        repo = {"open": {"totalCount": 0, "nodes": []}, "merged": {"nodes": []}, "defaultBranchRef": None}
        out = repos.summarize({"backend": repo, "web": repo, "ios": repo}, now)
        self.assertIsNone(out["repos"][0]["merged_24h"])

    def test_equal_start_times_never_depend_on_list_order(self):
        def attempt(i, conclusion):
            return {"__typename": "CheckRun", "databaseId": i, "name": "fast", "status": "COMPLETED", "conclusion": conclusion,
                    "startedAt": "2026-10-22T08:00:00Z", "completedAt": "2026-10-22T08:05:00Z"}
        first, second = attempt(1, "FAILURE"), attempt(2, "SUCCESS")
        for nodes in ([first, second], [second, first]):
            self.assertEqual(repos.latest_attempts(roll(*nodes))["fast"]["state"], "SUCCESS")
        self.assertEqual(repos.flaky_checks(roll(first, second)), repos.flaky_checks(roll(second, first)))
        self.assertEqual(repos.flaky_checks(roll(first, second)), ["fast"])

    def test_a_tie_with_no_order_is_never_chosen_as_a_pass(self):
        def status(state):
            return {"__typename": "StatusContext", "context": "ci", "state": state, "createdAt": "2026-10-22T08:00:00Z"}
        for nodes in ([status("FAILURE"), status("SUCCESS")], [status("SUCCESS"), status("FAILURE")]):
            self.assertEqual(repos.latest_attempts(roll(*nodes))["ci"]["state"], "FAILURE")

    def test_status_contexts(self):
        ctx = {"__typename": "StatusContext", "context": "ci/legacy", "state": "FAILURE", "createdAt": "2026-10-23T10:00:00Z"}
        self.assertEqual(ci(ctx), ("failing", 1, False))

    def test_status_failure_is_not_hidden_by_a_passing_check_run_of_the_same_name(self):
        ctx = {"__typename": "StatusContext", "context": "fast", "state": "FAILURE", "createdAt": "2026-10-23T09:00:00Z"}
        self.assertEqual(ci(ctx, run("fast", "SUCCESS", "2026-10-23T10:00:00Z")), ("failing", 1, False))

    def test_stale_is_not_a_pass(self):
        self.assertEqual(ci(run("fast", "STALE")), ("none", 0, False))

    def test_partial_check_list_never_proves_a_pass(self):
        r = roll(*[run(f"m{i}", "SUCCESS") for i in range(60)])
        r["contexts"]["pageInfo"] = {"hasNextPage": True}
        r["state"] = "FAILURE"
        self.assertEqual(repos.ci_of(repos.latest_attempts(r), r["state"], repos.complete(r)), ("failing", 1, False))
        r["state"] = "SUCCESS"
        self.assertEqual(repos.ci_of(repos.latest_attempts(r), r["state"], repos.complete(r))[0], "none")

    def test_later_failure_does_not_reset_failing_since(self):
        c = repos.latest_attempts(roll(run("fast", "FAILURE", completed="2026-10-23T09:00:00Z"),
                                       run("integration", "FAILURE", completed="2026-10-25T09:00:00Z")))
        self.assertEqual(repos.failing_since(c), "2026-10-23T09:00:00Z")


class Rows(unittest.TestCase):
    def node(self, *checks, merge_state="CLEAN", draft=False, base="main"):
        return {"number": 7, "title": "ENG-12: Fix it", "headRefName": "x", "isDraft": draft, "createdAt": "2026-10-20T00:00:00Z",
                "updatedAt": "2026-10-23T12:00:00Z", "baseRefName": base, "mergeable": "MERGEABLE", "mergeStateStatus": merge_state,
                "commits": {"nodes": [{"commit": {"statusCheckRollup": roll(*checks)}}]}}

    def test_ready_since_is_when_the_last_check_finished(self):
        r = repos.pr_row("backend", self.node(run("test", "SUCCESS", completed="2026-10-23T10:10:00Z"),
                                              run("review-gate", "SUCCESS", completed="2026-10-23T11:00:00Z")))
        self.assertEqual((r["ci"], r["review"], r["ready_since"], r["issue"]), ("passed", "approved", "2026-10-23T11:00:00Z", "ENG-12"))
        self.assertEqual((r["blocker"], r["merge_ready"]), ("mergeable", True))

    def test_conflicts_are_named(self):
        r = repos.pr_row("web", self.node(run("test", "SUCCESS"), run("review-gate", "SUCCESS"), merge_state="DIRTY"))
        self.assertEqual((r["blocker"], r["merge_ready"], r["ready_since"]), ("conflicts", False, None))

    def test_unknown_merge_state_is_not_guessed(self):
        for state in (None, "UNKNOWN"):
            r = repos.pr_row("web", self.node(run("optional-lint", "SUCCESS"), run("review-gate", "SUCCESS"), merge_state=state))
            self.assertEqual((r["blocker"], r["merge_ready"], r["ready_since"]), (None, None, None))

    def test_unstable_is_not_ready(self):
        r = repos.pr_row("web", self.node(run("test", "SUCCESS"), run("review-gate", "SUCCESS"), merge_state="UNSTABLE"))
        self.assertEqual((r["merge_ready"], r["blocker"]), (False, "optional checks not passing"))

    def test_failing_since(self):
        r = repos.pr_row("web", self.node(run("test", "FAILURE", completed="2026-10-22T08:00:00Z"), run("e2e", status="IN_PROGRESS")))
        self.assertEqual((r["ci"], r["failed_checks"], r["checks_running"], r["failing_since"], r["ready_since"], r["merge_ready"]),
                         ("failing", 1, True, "2026-10-22T08:00:00Z", None, False))

    def test_summary_counts_match_the_listed_prs(self):
        node = self.node(run("test", "SUCCESS"))
        repo = {"open": {"totalCount": 3, "nodes": [node, self.node(draft=True), self.node(base="release")]},
                "merged": {"pageInfo": {"hasNextPage": False}, "nodes": [{"number": 1, "title": "ENG-1 x", "headRefName": "a", "mergedAt": "2026-10-23T11:00:00Z", "createdAt": "x"}]},
                "defaultBranchRef": {"target": {"statusCheckRollup": roll()}}}
        s = repos.summarize({"backend": repo, "web": repo, "ios": repo}, datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc))
        b = s["repos"][0]
        self.assertEqual((b["open"], b["drafts"], b["open_total"], b["merged_24h"], b["main"]), (1, 1, 3, 1, "unknown"))
        self.assertTrue(s["merge_state_known"])

    def test_merged_count_is_unknown_when_the_page_does_not_reach_the_window(self):
        old = {"number": 1, "title": "x", "headRefName": "a", "mergedAt": "2026-09-01T00:00:00Z", "createdAt": "x", "updatedAt": "2026-10-23T11:30:00Z"}
        repo = {"open": {"totalCount": 0, "nodes": []}, "merged": {"pageInfo": {"hasNextPage": True}, "nodes": [old]},
                "defaultBranchRef": {"target": {"statusCheckRollup": roll()}}}
        s = repos.summarize({"backend": repo, "web": repo, "ios": repo}, datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc))
        self.assertIsNone(s["repos"][0]["merged_24h"])
        repo["merged"]["nodes"][0]["updatedAt"] = "2026-10-20T00:00:00Z"
        s = repos.summarize({"backend": repo, "web": repo, "ios": repo}, datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc))
        self.assertEqual(s["repos"][0]["merged_24h"], 0)

    def test_recent_merges_sort_by_merge_time_before_slicing(self):
        nodes = [{"number": i, "title": "x", "headRefName": "a", "mergedAt": f"2026-10-23T{i:02d}:00:00Z", "createdAt": "x", "updatedAt": "2026-10-23T11:30:00Z"}
                 for i in range(1, 12)]
        repo = {"open": {"totalCount": 0, "nodes": []}, "merged": {"nodes": nodes[::-1][1:] + nodes[-1:]},
                "defaultBranchRef": {"target": {"statusCheckRollup": roll()}}}
        s = repos.summarize({"backend": repo, "web": repo, "ios": repo}, datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc))
        self.assertEqual(s["recent"][0]["number"], 11)

    def test_checkless_main_is_unknown_unless_declared(self):
        repo = {"open": {"totalCount": 0, "nodes": []}, "merged": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "defaultBranchRef": {"target": {"statusCheckRollup": None}}}
        s = repos.summarize({"backend": repo, "web": repo, "ios": repo}, datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc),
                            unchecked_main={"ios"})
        self.assertEqual([r["main"] for r in s["repos"]], ["unknown", "unknown", "not_applicable"])

    def test_main_without_checks_config(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "repos.json")
            self.assertEqual(repos.main_without_checks(path), set())
            with open(path, "w") as f:
                f.write('{"main_without_checks": ["ios", 3]}')
            self.assertEqual(repos.main_without_checks(path), {"ios"})
            with open(path, "w") as f:
                f.write("[1]")
            self.assertEqual(repos.main_without_checks(path), set())

    def test_main_failure_survives_a_running_check(self):
        repo = {"open": {"totalCount": 0, "nodes": []}, "merged": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                "defaultBranchRef": {"target": {"statusCheckRollup": roll(run("fast", "FAILURE"), run("integration", status="IN_PROGRESS"))}}}
        s = repos.summarize({"backend": repo, "web": repo, "ios": repo}, datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.timezone.utc), merge_state_known=False)
        b = s["repos"][0]
        self.assertEqual((b["main"], b["main_failed_checks"], b["main_running"]), ("failure", 1, True))
        self.assertFalse(s["merge_state_known"])
        self.assertEqual(len([p for p in s["prs"] if p["repo"] == "backend"]), b["open"])


class EvidenceFields(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 1, 12, tzinfo=datetime.timezone.utc)

    def data(self, merged, more=False):
        empty = {"open": {"totalCount": 0, "nodes": []}, "merged": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "defaultBranchRef": None}
        return {"backend": dict(empty, merged={"pageInfo": {"hasNextPage": more}, "nodes": merged}), "web": empty, "ios": empty}

    def test_every_merged_row_in_the_response_keeps_its_full_head(self):
        rows = [{"number": n, "title": "t", "headRefName": "b", "headRefOid": f"{n:040x}", "mergedAt": "2026-11-01T01:00:00Z",
                 "createdAt": "2026-10-31T01:00:00Z", "updatedAt": "2026-11-01T01:00:00Z"} for n in range(1, 13)]
        out = repos.summarize(self.data(rows), self.NOW)
        self.assertEqual(len(out["merged"]), 12)
        self.assertEqual(len(out["recent"]), 10)
        self.assertEqual(out["merged"][0]["head"], f"{1:040x}")

    def test_a_cut_merged_list_says_so(self):
        out = repos.summarize(self.data([], more=True), self.NOW)
        self.assertIs(out["repos"][0]["merged_complete"], False)

    def test_check_rows_keep_the_identity_and_kind_of_each_context(self):
        rows = repos.check_runs({"contexts": {"nodes": [run("test", "SUCCESS") | {"databaseId": 7}, {"__typename": "StatusContext", "context": "gate", "state": "SUCCESS", "createdAt": "2026-10-23T10:00:00Z"}]}})
        self.assertEqual([(r["kind"], r["name"], r["id"]) for r in rows], [("check_run", "test", 7), ("status", "gate", None)])

    def test_no_extra_github_call_is_needed(self):
        self.assertEqual(repos.FRAGMENT.count("pullRequests("), 2)
        self.assertNotIn("timelineItems", repos.FRAGMENT)


if __name__ == "__main__":
    unittest.main()


class CIWaiting(unittest.TestCase):
    def test_queued_only_is_waiting(self):
        self.assertTrue(repos.ci_waiting({"fast": {"state": "QUEUED"}, "integration": {"state": "PENDING"}}))

    def test_any_started_is_in_ci(self):
        self.assertFalse(repos.ci_waiting({"fast": {"state": "IN_PROGRESS"}, "integration": {"state": "QUEUED"}}))

    def test_gate_and_finished_checks_ignored(self):
        self.assertFalse(repos.ci_waiting({"review-gate": {"state": "QUEUED"}, "fast": {"state": "SUCCESS"}}))


class Runs(unittest.TestCase):
    def test_only_unfinished_actions_runs(self):
        u = "https://github.com/o/r/actions/runs/{}/job/9"
        roll = {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"__typename": "CheckRun", "name": "quality", "status": "QUEUED", "detailsUrl": u.format(40207760043)},
            {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": u.format(1)},
            {"__typename": "CheckRun", "name": "ext", "status": "IN_PROGRESS", "detailsUrl": "https://ci.example/7"},
            {"__typename": "StatusContext", "context": "review-gate", "state": "PENDING"}]}}
        self.assertEqual(repos.running_runs(roll), [40207760043])


class Budget(unittest.TestCase):
    def test_budget_travels_with_the_repo_data(self):
        s = repos.with_budget({"repos": []}, read=lambda: {"core": 1, "core_limit": 2, "core_reset": 3, "graphql": 4})
        self.assertEqual(s["github_budget"]["value"]["core"], 1)
        self.assertGreater(s["github_budget"]["fetched"], 0)

    def test_failed_budget_is_an_error_not_a_zero(self):
        def fail():
            raise RuntimeError("no gh")
        b = repos.with_budget({"repos": []}, read=fail)["github_budget"]
        self.assertEqual((b["value"], b["fetched"], b["error"]), (None, None, "no gh"))


class QueuePlace(unittest.TestCase):
    Q = staticmethod(lambda t, i=None: {"state": "QUEUED", "done": None, "started": t, "id": i})
    DONE = {"state": "SUCCESS", "done": "x", "started": "y"}

    def test_linux_lanes_have_no_tier(self):
        for repo in ("backend", "web"):
            self.assertEqual(repos.queue_place(repo, {"integration": self.Q("2026-10-25T16:05:00Z"), "fast": self.DONE}), (None, "2026-10-25T16:05:00Z"))

    def test_mac_lane_keeps_tiers(self):
        self.assertEqual(repos.queue_place("ios", {"plan": self.Q("2026-10-25T16:19:50Z")}), (0, "2026-10-25T16:19:50Z"))
        self.assertEqual(repos.queue_place("ios", {"verify customer": self.Q("2026-10-25T16:05:00Z")}), (1, "2026-10-25T16:05:00Z"))

    def test_nothing_queued(self):
        self.assertEqual(repos.queue_place("backend", {"fast": self.DONE}), (None, None))

    def test_queued_jobs_are_separate_legs_with_ids(self):
        jobs = repos.queued_jobs({"integration": self.Q("2026-10-25T16:05:00Z", 9), "fast": self.Q("2026-10-25T16:00:00Z", 8),
                                  "review-gate": self.Q("2026-10-25T15:00:00Z", 7), "done": self.DONE})
        self.assertEqual([(j["job"], j["id"]) for j in jobs], [("fast", 8), ("integration", 9)])

    def test_check_run_ids_are_read(self):
        roll = {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "CheckRun", "databaseId": 42, "name": "fast", "status": "QUEUED", "startedAt": "t"}]}}
        self.assertEqual(repos.latest_attempts(roll)["fast"]["id"], 42)


class Flaky(unittest.TestCase):
    def roll(self, *attempts):
        return {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "CheckRun", "name": n, "status": "COMPLETED", "conclusion": c,
                                        "startedAt": t} for n, c, t in attempts]}}

    def test_fail_then_pass_is_flaky(self):
        self.assertEqual(repos.flaky_checks(self.roll(("fast", "FAILURE", "1"), ("fast", "SUCCESS", "2"))), ["fast"])

    def test_pass_then_fail_is_not(self):
        self.assertEqual(repos.flaky_checks(self.roll(("fast", "SUCCESS", "1"), ("fast", "FAILURE", "2"))), [])

    def test_gate_ignored(self):
        self.assertEqual(repos.flaky_checks(self.roll(("review-gate", "FAILURE", "1"), ("review-gate", "SUCCESS", "2"))), [])


class Cache(unittest.TestCase):
    def test_a_headless_run_keeps_its_cache_in_its_own_data_directory(self):
        self.assertEqual(repos.cache_path({"FLIGHTOPS_DATA_DIR": "/data"}), "/data/cache/repos.json")

    def test_the_mac_app_keeps_its_existing_cache(self):
        self.assertEqual(repos.cache_path({}), os.path.expanduser("~/Library/Caches/flightops/repos.json"))


class CacheClocks(unittest.TestCase):
    def serve(self, fetched, mtime_offset):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "repos.json")
            with open(path, "w") as f:
                json.dump({"fetched": fetched(time.time()), "repos": [], "prs": []}, f)
            os.utime(path, (time.time() + mtime_offset, time.time() + mtime_offset))
            with mock.patch.object(repos, "CACHE", path), mock.patch.object(repos, "with_budget", lambda x: x), \
                    mock.patch.object(repos, "fetch", return_value={"fetched": 9e9}) as fetch, contextlib.redirect_stdout(io.StringIO()):
                repos.main()
            return fetch.call_count

    def test_a_current_cache_is_served(self):
        self.assertEqual(self.serve(lambda now: now - 60, -60), 0)

    def test_a_future_modification_time_cannot_hold_a_refresh(self):
        self.assertEqual(self.serve(lambda now: 1000, 86400), 1)

    def test_a_recent_file_with_an_old_or_future_payload_clock_is_refreshed(self):
        self.assertEqual(self.serve(lambda now: 1000, -60), 1)
        self.assertEqual(self.serve(lambda now: now + 3600, -60), 1)
        self.assertEqual(self.serve(lambda now: "soon", -60), 1)
