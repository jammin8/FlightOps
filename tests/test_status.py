import json
import os
import tempfile
import time
import unittest
from unittest import mock

import synthetic
from synthetic import NOW, ago, check, live_pr, put_live, status_context
import collect
import config
import flightops
import github
import make_sample
import metrics
import store


def one(node, approval=None, viewer="me", now=NOW):
    return metrics.classify(node, approval, viewer, now)


class Stages(unittest.TestCase):
    def test_draft(self):
        self.assertEqual(one(live_pr(1, "t", ago(100), draft=True))["stage"], "draft")

    def test_failing_check_is_fixing_and_cancelled_is_not(self):
        failing = one(live_pr(1, "t", ago(900), checks=[check("a", "COMPLETED", "FAILURE", ago(800), ago(700))]))
        self.assertEqual((failing["stage"], failing["stage_since"], failing["ci"]), ("fixing", ago(700), "failing"))
        cancelled = one(live_pr(1, "t", ago(900), checks=[check("a", "COMPLETED", "CANCELLED", ago(800), ago(700))]))
        self.assertEqual((cancelled["stage"], cancelled["ci"]), ("in_review", "none"))

    def test_changes_requested_is_fixing(self):
        node = live_pr(1, "t", ago(900), decision="CHANGES_REQUESTED", reviews=[("CHANGES_REQUESTED", ago(300), "a")])
        self.assertEqual((one(node)["stage"], one(node)["stage_since"]), ("fixing", ago(300)))

    def test_queued_waits_for_ci_and_running_is_in_ci(self):
        queued = one(live_pr(1, "t", ago(900), checks=[check("a", "QUEUED", queued=ago(500))]))
        self.assertEqual((queued["stage"], queued["stage_since"]), ("waiting_ci", ago(500)))
        both = one(live_pr(1, "t", ago(900), checks=[check("a", "QUEUED", queued=ago(500)), check("b", "IN_PROGRESS", started=ago(400))]))
        self.assertEqual((both["stage"], both["stage_since"]), ("in_ci", ago(400)))

    def test_no_ci_goes_straight_to_review(self):
        node = one(live_pr(1, "t", ago(900), ready=ago(600)))
        self.assertEqual((node["stage"], node["stage_since"], node["ci"]), ("in_review", ago(600), "none"))

    def test_review_since_is_when_checks_passed(self):
        node = one(live_pr(1, "t", ago(900), checks=[check("a", "COMPLETED", "SUCCESS", ago(800), ago(200))], requested=["bob"]))
        self.assertEqual((node["stage_since"], node["waiting_on"], node["review"]), (ago(200), "review from bob", "requested"))

    def test_approved_by_review(self):
        node = live_pr(1, "t", ago(900), reviews=[("APPROVED", ago(100), "a")], decision="APPROVED")
        self.assertEqual((one(node)["stage"], one(node)["stage_since"]), ("approved", ago(100)))

    def test_approval_check_counts_only_when_configured(self):
        node = live_pr(1, "t", ago(900), checks=[status_context("review-gate", "SUCCESS", ago(50)), check("a", "COMPLETED", "SUCCESS", ago(800), ago(700))])
        self.assertEqual(one(node, "review-gate")["stage"], "approved")
        self.assertEqual(one(node)["stage"], "in_review")

    def test_pending_approval_check_is_not_ci(self):
        node = live_pr(1, "t", ago(900), checks=[status_context("review-gate", "PENDING", ago(50))])
        self.assertEqual(one(node, "review-gate")["stage"], "in_review")

    def test_needs_you(self):
        self.assertEqual(one(live_pr(1, "t", ago(900), requested=["me"]))["needs_you"], "Your review is requested")
        self.assertIsNone(one(live_pr(1, "t", ago(900), requested=["me"]), viewer=None)["needs_you"])
        fresh = live_pr(1, "t", ago(9000), checks=[check("a", "COMPLETED", "FAILURE", ago(800), ago(700))])
        self.assertIsNone(one(fresh)["needs_you"])
        idle = live_pr(1, "t", ago(90000), checks=[check("a", "COMPLETED", "FAILURE", ago(80000), ago(70000))], head_at=ago(80000))
        self.assertEqual(one(idle)["needs_you"], "Failing with no new push")


class Views(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = store.Store(self.directory.name)
        self.config = config.parse({"repos": ["o/a", "o/b"]})

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_nothing_read_is_unknown_never_zero(self):
        status = metrics.build(self.db, self.config, NOW)
        everything = status["views"]["all"]
        self.assertEqual((everything["state"], everything["counts"]["open"], everything["counts"]["merged_today"]), ("unknown", None, None))
        self.assertTrue(all(s["count"] is None for s in everything["stages"]))
        self.assertEqual(everything["status_line"], "Can't read GitHub yet.")
        self.assertEqual(status["sources"]["github"]["state"], "error")

    def test_a_missing_repo_makes_the_total_partial(self):
        put_live(self.db, "o/a", [live_pr(1, "t", ago(100))])
        status = metrics.build(self.db, self.config, NOW)
        self.assertEqual((status["views"]["all"]["counts"]["open"], status["views"]["all"]["partial"]), (1, True))
        self.assertEqual(status["views"]["o/b"]["counts"]["open"], None)
        self.assertEqual(status["views"]["o/a"]["prs"], ["o/a#1"])
        self.assertIn("missing", status["views"]["all"]["status_line"])
        self.assertIsNone(status["views"]["all"]["counts"]["merged_today"])

    def test_old_data_is_stale(self):
        put_live(self.db, "o/a", [], now=NOW - 3600)
        put_live(self.db, "o/b", [])
        status = metrics.build(self.db, self.config, NOW)
        self.assertEqual((status["views"]["o/a"]["state"], status["views"]["all"]["state"]), ("stale", "stale"))

    def test_a_failed_read_after_a_good_one_is_stale_with_the_error(self):
        put_live(self.db, "o/a", [])
        with self.db.tx():
            self.db.read_failed("o/a", "live", "boom", NOW + 1)
        status = metrics.build(self.db, self.config, NOW + 2)
        self.assertEqual((status["repos"][0]["state"], status["repos"][0]["error"]), ("stale", "boom"))

    def test_wait_needs_enough_jobs(self):
        with self.db.tx():
            for i in range(4):
                self.db.db.execute("INSERT INTO ci_jobs(repo, run_id, run_attempt, job_id, queued_at, started_at) VALUES ('o/a', ?, 1, ?, ?, ?)",
                                   (i, i, ago(3600), ago(3600 - 60 * (i + 1))))
        self.assertIsNone(metrics.trends(self.db, ["o/a"], NOW)["ci_wait_p90_min"][-1])
        with self.db.tx():
            self.db.db.execute("INSERT INTO ci_jobs(repo, run_id, run_attempt, job_id, queued_at, started_at) VALUES ('o/a', 9, 1, 9, ?, ?)", (ago(3600), ago(600)))
        trend = metrics.trends(self.db, ["o/a"], NOW)
        self.assertEqual((trend["ci_wait_p90_min"][-1], trend["ci_jobs"][-1]), (50.0, 5))

    def test_every_view_agrees_with_the_pr_list(self):
        status = make_sample.sample()
        ids = {p["id"]: p for p in status["prs"]}
        for key, block in status["views"].items():
            repos = [r["id"] for r in status["repos"]] if key == "all" else [key]
            self.assertEqual(sorted(block["prs"]), sorted(i for i, p in ids.items() if p["repo"] in repos))
            self.assertEqual(sum(s["count"] for s in block["stages"] if s["id"] != "merged"), block["counts"]["open"])
        self.assertEqual(sum(status["views"][r["id"]]["counts"]["open"] for r in status["repos"]), status["views"]["all"]["counts"]["open"])

    def test_sample_file_is_current(self):
        with open(make_sample.TARGET) as f:
            committed = json.load(f)
        self.assertEqual(committed, json.loads(json.dumps(make_sample.sample())), "run python3 tests/make_sample.py")


class Config(unittest.TestCase):
    def test_parse(self):
        parsed = config.parse({"repos": ["o/a", {"name": "o/b", "color": "#123456"}, "O/A", "bad", {"name": "o/c", "color": "red"}], "approval_check": "gate"})
        self.assertEqual([r["name"] for r in parsed["repos"]], ["o/a", "o/b", "o/c"])
        self.assertEqual(parsed["repos"][1]["color"], "#123456")
        self.assertTrue(parsed["repos"][2]["color"].startswith("#"))
        self.assertEqual(parsed["approval_check"], "gate")

    def test_save_keeps_colors_and_options(self):
        with tempfile.TemporaryDirectory() as directory:
            where = os.path.join(directory, "c", "config.json")
            with open(os.path.join(directory, "seed.json"), "w") as f:
                json.dump({"repos": [{"name": "o/a", "color": "#111111"}], "approval_check": "gate"}, f)
            seed = config.load(os.path.join(directory, "seed.json"))[0]
            config.save(["o/a", "o/b"], where, keep=seed)
            loaded, problem = config.load(where)
            self.assertIsNone(problem)
            self.assertEqual(([r["name"] for r in loaded["repos"]], loaded["repos"][0]["color"], loaded["approval_check"]), (["o/a", "o/b"], "#111111", "gate"))

    def test_missing_and_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(config.load(os.path.join(directory, "none.json"))[1], "missing")
            bad = os.path.join(directory, "bad.json")
            with open(bad, "w") as f:
                f.write("{")
            self.assertEqual(config.load(bad)[1], "unreadable")


class FakeGitHub:
    def __init__(self, repos=("o/a",)):
        self.calls, self.repos, self.signed_in = [], set(repos), True
        self.runs = {}
        self.jobs = {}
        self.etag_hits = 0

    def http(self, status, body, headers=None):
        head = f"HTTP/2.0 {status}\n" + "".join(f"{k}: {v}\n" for k, v in (headers or {}).items())
        return collect_answer(0, (head + "\n" + (json.dumps(body) if body is not None else "")).encode())

    def __call__(self, command, limit, timeout):
        self.calls.append(command)
        if not self.signed_in:
            return collect_answer(4, b"")
        if command[1:3] == ["api", "graphql"]:
            query = command[4][6:]
            variables = dict(a.split("=", 1) for a in command[6::2])
            repo = f"{variables['owner']}/{variables['name']}"
            if repo not in self.repos:
                return collect_answer(1, json.dumps({"data": {"repository": None}, "errors": [{"message": "Could not resolve"}]}).encode())
            if "states: OPEN" in query:
                return collect_answer(0, json.dumps({"data": {"rateLimit": {"remaining": 4000}, "viewer": {"login": "me"},
                    "repository": {"nameWithOwner": repo, "isArchived": False, "defaultBranchRef": {"name": "trunk"},
                                   "pullRequests": {"totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [live_pr(7, "Open one", ago(600))]}}}}).encode())
            after = variables.get("after")
            nodes = [{"number": 5 if after else 6, "title": "x", "state": "MERGED", "isDraft": False, "createdAt": ago(90000), "updatedAt": ago(80000),
                      "mergedAt": ago(80000), "closedAt": ago(80000), "author": {"login": "dev"}, "reviews": {"nodes": [{"state": "APPROVED", "submittedAt": ago(85000), "author": {"login": "me"}}]},
                      "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "MergedEvent", "createdAt": ago(80000)}]}}]
            return collect_answer(0, json.dumps({"data": {"rateLimit": {"remaining": 4000}, "repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": not after, "endCursor": "c1"}, "nodes": nodes}}}}).encode())
        path = command[5]
        if path == "rate_limit":
            return self.http(200, {"resources": {"core": {"remaining": 4000}, "graphql": {"remaining": 4000}}})
        if "/actions/runs?" in path:
            if "If-None-Match: e1" in command:
                self.etag_hits += 1
                return self.http(304, None)
            recent = "%3E%3D" in path
            runs = [{"id": 1, "run_attempt": 1, "name": "ci", "status": "completed", "conclusion": "success", "created_at": ago(3000),
                     "run_started_at": ago(2900), "updated_at": ago(2000), "pull_requests": [{"number": 7}]}] if recent else []
            return self.http(200, {"workflow_runs": runs}, {"ETag": "e1"} if recent else {})
        if "/jobs?" in path:
            return self.http(200, {"jobs": [{"id": 11, "run_id": 1, "run_attempt": 1, "name": "test", "status": "completed", "conclusion": "success",
                                             "created_at": ago(3000), "started_at": ago(2940), "completed_at": ago(2000)}]})
        return self.http(404, {})


def collect_answer(code, out):
    from capture import Answer
    return Answer(code, out)


class Collect(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"FLIGHTOPS_DATA_DIR": self.directory.name, "FLIGHTOPS_CONFIG": os.path.join(self.directory.name, "config.json"),
                                                "FLIGHTOPS_STATUS_FILE": os.path.join(self.directory.name, "status.json"), "FLIGHTOPS_GH": "gh"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def status(self):
        with open(os.environ["FLIGHTOPS_STATUS_FILE"]) as f:
            return json.load(f)

    def test_no_config_writes_a_first_run_status(self):
        self.assertEqual(flightops.do_collect(runner=FakeGitHub()), 0)
        status = self.status()
        self.assertEqual((status["config"], status["sources"]["github"]["state"], status["views"]["all"]["counts"]["open"]), ({"state": "missing", "repos": 0}, "never", None))

    def test_a_pass_fills_the_store_and_the_status(self):
        config.save(["o/a"])
        fake = FakeGitHub()
        self.assertEqual(flightops.do_collect(runner=fake), 0)
        db = store.Store()
        self.assertEqual(db.db.execute("SELECT count(*) FROM prs").fetchone()[0], 2)
        self.assertEqual(db.db.execute("SELECT count(*) FROM pr_timeline WHERE type = 'Review'").fetchone()[0], 2)
        self.assertEqual(db.db.execute("SELECT queued_at, started_at FROM ci_jobs").fetchone()[:], (ago(3000), ago(2940)))
        self.assertEqual(db.db.execute("SELECT jobs_read FROM ci_run_attempts").fetchone()[0], 1)
        db.close()
        status = self.status()
        self.assertEqual((status["repos"][0]["default_branch"], status["viewer"], status["views"]["all"]["counts"]["open"]), ("trunk", "me", 1))
        self.assertEqual(status["sources"]["github"]["state"], "ok")
        self.assertTrue(all("mutation" not in " ".join(c) for c in fake.calls))
        flightops.do_collect(runner=fake)
        self.assertGreaterEqual(fake.etag_hits, 1)

    def test_an_unknown_repo_is_an_error_not_an_empty_board(self):
        config.save(["o/a", "o/gone"])
        flightops.do_collect(runner=FakeGitHub())
        status = self.status()
        gone = next(r for r in status["repos"] if r["id"] == "o/gone")
        self.assertEqual((gone["state"], status["views"]["o/gone"]["counts"]["open"], status["sources"]["github"]["state"]), ("unknown", None, "partial"))
        self.assertTrue(gone["error"])

    def test_gh_signed_out_says_so(self):
        config.save(["o/a"])
        fake = FakeGitHub()
        fake.signed_in = False
        self.assertEqual(flightops.do_collect(runner=fake), 1)
        status = self.status()
        self.assertEqual((status["repos"][0]["error"], status["views"]["all"]["state"]), ("gh is not signed in", "unknown"))

    def test_the_client_refuses_writes(self):
        client = github.Client(runner=FakeGitHub())
        with self.assertRaises(ValueError):
            client.graphql_query("mutation { x }")
        with self.assertRaises(ValueError):
            client.rest("repos/o/a/pulls/1/merge -X PUT")


if __name__ == "__main__":
    unittest.main()
