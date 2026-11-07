import fcntl
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import ledger
import ledger_activity
import ledger_ci
import ledger_github
import ledger_import
import ledger_prs
import ledger_store
from ledger_fakes import FakeGitHub, http

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = ledger_store.epoch("2026-11-01T12:00:00Z")
SHA = "a" * 40
ZERO = ledger_store.ZERO


def activity(n, at, before, after):
    return {"id": n, "ref": "refs/heads/b", "before": before, "after": after, "timestamp": at, "activity_type": "push", "actor": {"login": "jammin8", "id": 1}}


def pr_answer(query):
    nodes = [{"number": 1, "state": "OPEN", "isDraft": False, "createdAt": "2026-11-01T09:00:00Z", "updatedAt": "2026-11-01T09:30:00Z", "headRefName": "b", "headRefOid": SHA,
              "isCrossRepository": False, "baseRefName": "main", "headRepository": {"nameWithOwner": "example-org/example-web"}, "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": []}}]
    data = {"rateLimit": {"cost": 1, "remaining": 4000}}
    if "pullRequests" in query:
        for repo in ("backend", "web", "ios"):
            if f"{repo}: repository" in query:
                data[repo] = {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": nodes if repo == "web" else []}}
    else:
        rules = {"branchProtectionRules": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "rulesets": {"pageInfo": {"hasNextPage": False}, "nodes": []}}
        data.update({repo: rules for repo in ("backend", "web", "ios")})
    return {"data": data}


def blocked_ci():
    fake = world()
    fake.routes.pop(ledger_activity.page_path("ios"))
    original = fake.routes[ledger_activity.page_path("web")]
    fake.routes[ledger_activity.page_path("web")] = http(200, original, remaining=1000)
    fake.routes[f"repos/example-org/example-web/actions/runs?head_sha={SHA}&per_page=100"] = http(200, {"workflow_runs": []}, remaining=999)
    return fake


def quiet(answer):
    if "web" in answer["data"] and "pullRequests" in answer["data"]["web"]:
        answer["data"]["web"]["pullRequests"]["nodes"] = []
    return answer


def world():
    base = "repos/example-org/example-web"
    run = {"id": 7, "run_attempt": 1, "name": "Q", "status": "completed", "conclusion": "failure", "head_sha": SHA, "created_at": "2026-11-01T09:01:00Z", "updated_at": "2026-11-01T09:05:00Z"}
    routes = {ledger_activity.page_path("web"): [activity(1, "2026-11-01T08:50:00Z", ZERO, SHA)],
              ledger_activity.page_path("backend"): [], ledger_activity.page_path("ios"): [],
              f"{base}/actions/runs?head_sha={SHA}&per_page=100": {"workflow_runs": [run]},
              f"{base}/commits/{SHA}/check-runs?filter=all&per_page=100": {"check_runs": []},
              f"{base}/commits/{SHA}/statuses?per_page=100": [],
              f"{base}/actions/runs/7/attempts/1/jobs?per_page=100": {"total_count": 0, "jobs": []}}
    return FakeGitHub(routes, graphql=pr_answer)


class WholePass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "data")
        self.root = os.path.join(self.tmp.name, "daily")
        os.makedirs(os.path.join(self.root, "2026-10-24", "example-web"))
        with open(os.path.join(self.root, "2026-10-24", "example-web", "runs.json"), "w") as f:
            json.dump([{"id": 1, "run_attempt": 1, "status": "completed", "conclusion": "failure", "head_sha": "b" * 40, "updated_at": "2026-10-05T00:00:00Z"}], f)

    def run_pass(self, fake=None, now=NOW):
        return ledger.run_pass(self.dir, seconds=40, runner=fake or world(), now=now, root=self.root)

    def test_one_pass_connects_activity_prs_ci_and_the_import(self):
        result = self.run_pass()
        self.assertEqual((result["rows"]["activity_events"], result["rows"]["prs"], result["rows"]["ci_run_attempts"]), (1, 1, 2))
        self.assertEqual(result["prs"]["head_at_open"], {"exact": 1})
        self.assertEqual(result["ci"]["run_sources"], {"ci-program-daily": 1, "github-rest": 1})
        self.assertEqual(result["last_pass"]["outcome"], "complete")

    def test_late_activity_revises_a_pr_already_resolved(self):
        fake = world()
        self.assertEqual(self.run_pass(fake)["prs"]["head_at_open"], {"exact": 1})
        late = "c" * 40
        fake.routes[ledger_activity.page_path("web")] = [activity(2, "2026-11-01T08:55:00Z", SHA, late), activity(1, "2026-11-01T08:50:00Z", ZERO, SHA)]
        fake.graphql = lambda query: quiet(pr_answer(query))
        self.run_pass(fake, NOW + 900)
        store = ledger_store.Store(self.dir, readonly=True)
        self.addCleanup(store.close)
        self.assertEqual(store.db.execute("SELECT head_at_open FROM pr_witness").fetchone()[0], late)
        self.assertEqual(store.db.execute("SELECT count(*) FROM ci_heads WHERE sha = ?", (late,)).fetchone()[0], 1)

    def test_progress_counts_the_backfill_and_the_retention_window(self):
        first = self.run_pass(world())["progress"]
        self.assertEqual((first["heads_in_window"], first["heads_in_window_unread"], first["backfill_heads_remaining"]), (1, 0, 0))
        self.assertEqual(first["last_pass"]["outcome"], "complete")

    def test_an_unread_head_inside_the_window_is_counted_with_its_age(self):
        result = self.run_pass(blocked_ci())
        self.assertEqual((result["progress"]["heads_in_window_unread"], result["progress"]["backfill_heads_remaining"]), (1, 1))
        self.assertEqual(result["progress"]["oldest_unread_in_window"], "2026-11-01T08:50:00Z")

    def test_a_head_outside_the_window_is_not_counted_in_it(self):
        result = self.run_pass(blocked_ci(), NOW + 15 * 86400)
        self.assertEqual((result["progress"]["heads_in_window"], result["progress"]["backfill_heads_remaining"]), (0, 1))

    def test_a_step_error_is_reported_in_the_output_with_exit_zero(self):
        with mock.patch.object(ledger, "run_pass", return_value={"errors": ["rules: RuntimeError"]}), mock.patch("sys.stdout") as out:
            self.assertEqual(ledger.main([self.dir]), 0)
        self.assertIn("rules: RuntimeError", "".join(c.args[0] for c in out.write.call_args_list))

    def test_a_fractional_wall_clock_is_accepted(self):
        self.assertEqual(self.run_pass(now=NOW + 0.5)["errors"], [])

    def test_api_spend_is_recorded_with_the_pass(self):
        result = self.run_pass()
        self.assertEqual((result["last_pass"]["rest_requests"], result["last_pass"]["graphql_requests"]), (13, 2))

    def test_the_budget_floor_makes_no_github_read_and_still_imports(self):
        fake = world()
        fake.remaining = 999
        result = self.run_pass(fake)
        self.assertEqual((fake.calls, fake.queries, result["stopped"]), ([], [], "budget-floor"))
        self.assertEqual(result["import"]["files"], {"done": 1})

    def test_a_failed_step_does_not_stop_the_others(self):
        with mock.patch.object(ledger_prs, "read_policy", side_effect=RuntimeError("boom")):
            result = self.run_pass()
        self.assertEqual(result["errors"], ["rules: RuntimeError"])
        self.assertEqual((result["rows"]["activity_events"], result["ci"]["run_sources"]["github-rest"], result["import"]["files"]), (1, 1, {"done": 1}))

    def test_errors_carry_no_exception_text(self):
        with mock.patch.object(ledger_import, "run", side_effect=RuntimeError("token-like-text")):
            result = self.run_pass()
        self.assertNotIn("token-like-text", json.dumps(result))

    def test_a_stop_mid_read_still_reconciles_what_was_stored(self):
        fake = world()
        fake.routes[ledger_activity.page_path("web")] = http(200, [activity(1, "2026-11-01T08:50:00Z", ZERO, SHA)], remaining=999)
        result = self.run_pass(fake)
        self.assertEqual((result["rows"]["activity_events"], result["rows"]["branch_incarnations"]), (1, 1))

    def test_a_second_pass_repeats_nothing(self):
        fake = world()
        first = self.run_pass(fake)
        second = self.run_pass(fake, NOW + 900)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(second["import"]["rows_read"], first["import"]["rows_read"])

    def test_an_interrupted_pass_leaves_a_marked_row_and_resumes(self):
        fake = world()
        with mock.patch.object(ledger_ci, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_pass(fake)
        store = ledger_store.Store(self.dir, readonly=True)
        self.assertEqual(store.db.execute("SELECT outcome FROM passes").fetchone()[0], "interrupted")
        store.close()
        self.assertEqual(self.run_pass(fake, NOW + 900)["last_pass"]["outcome"], "complete")

    def test_gaps_are_reported_by_source_and_kind(self):
        fake = world()
        fake.routes[ledger_activity.page_path("ios")] = http(502, {})
        self.assertEqual(self.run_pass(fake)["gaps_open"], {"activity/read-failed": 1})

    def test_the_report_opens_read_only(self):
        self.run_pass()
        with mock.patch("sys.stdout") as out:
            self.assertEqual(ledger.main([self.dir, "--report"]), 0)
        self.assertIn("flightops-ledger-pass/1", "".join(c.args[0] for c in out.write.call_args_list))

    def test_a_stand_alone_pass_refuses_while_collect_holds_the_lock(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "collect.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch("sys.stdout"):
                self.assertEqual(ledger.main([self.dir]), ledger.LOCKED)


class OldLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, ledger_store.FILE)
        made = sqlite3.connect(path)
        made.execute("CREATE TABLE gaps(id INTEGER PRIMARY KEY)")
        made.execute("PRAGMA user_version=1")
        made.commit()
        made.close()
        with open(path, "rb") as f:
            self.bytes = f.read()
        self.path = path

    def output(self, *flags):
        with mock.patch("sys.stdout") as out:
            code = ledger.main([self.tmp.name, *flags])
        return code, json.loads("".join(c.args[0] for c in out.write.call_args_list))

    def test_a_pass_reports_the_refusal_as_a_source_error_and_spends_nothing(self):
        with mock.patch.object(ledger_github, "run_command", side_effect=AssertionError):
            code, report = self.output()
        self.assertEqual(code, 0)
        self.assertIn("schema 1", report["error"])
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), self.bytes)

    def test_the_report_names_the_refusal(self):
        self.assertIn("schema 1", self.output("--report")[1]["error"])


class Language(unittest.TestCase):
    FILES = sorted((ROOT / "collectors").glob("ledger*.py"))

    def test_no_retry_is_ever_called_a_flake(self):
        self.assertEqual(len(self.FILES), 7)
        for path in self.FILES:
            self.assertNotRegex(path.read_text(), re.compile("flak", re.I), path.name)

    def test_no_ranking_or_model_label_is_produced(self):
        for path in self.FILES:
            self.assertNotRegex(path.read_text(), re.compile(r"\b(sonnet|opus|ranking|leaderboard|best_model)\b", re.I), path.name)

    def test_no_comments_or_docstrings(self):
        for path in self.FILES:
            text = path.read_text()
            self.assertEqual([l for l in text.splitlines()[1:] if l.lstrip().startswith("#")], [], path.name)

    def test_the_protected_packages_are_not_imported(self):
        for path in self.FILES:
            self.assertNotRegex(path.read_text(), r"\b(model_analysis|model_evidence)\b", path.name)


if __name__ == "__main__":
    unittest.main()
