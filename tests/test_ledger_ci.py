import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import ledger_ci
import ledger_github
import ledger_store
from ledger_fakes import FakeGitHub, http

NOW = ledger_store.epoch("2026-11-01T12:00:00Z")
SHA = "a" * 40
BASE = "repos/example-org/example-web"
RUNS = f"{BASE}/actions/runs?head_sha={SHA}&per_page=100"
CHECKS = f"{BASE}/commits/{SHA}/check-runs?filter=all&per_page=100"
STATUSES = f"{BASE}/commits/{SHA}/statuses?per_page=100"


def run_body(run_id=7, attempt=1, conclusion="failure", status="completed", **more):
    return dict({"id": run_id, "run_attempt": attempt, "name": "Quality", "status": status, "conclusion": conclusion, "head_sha": SHA, "event": "pull_request",
                 "created_at": "2026-11-01T09:00:00Z", "run_started_at": "2026-11-01T09:00:05Z", "updated_at": f"2026-11-01T09:1{attempt}:00Z",
                 "pull_requests": [{"number": 1}], "head_repository": {"full_name": "example-org/example-web"}, "actor": {"login": "a"}, "triggering_actor": {"login": "b"},
                 "display_title": "SECRET", "head_commit": {"message": "SECRET"}}, **more)


def job_body(job_id=11, run_id=7, attempt=1, conclusion="failure", **more):
    return dict({"id": job_id, "run_id": run_id, "run_attempt": attempt, "name": "test", "workflow_name": "Quality", "head_sha": SHA, "status": "completed", "conclusion": conclusion,
                 "created_at": "2026-11-01T09:00:10Z", "started_at": "2026-11-01T09:01:00Z", "completed_at": "2026-11-01T09:05:00Z", "runner_name": "r", "labels": ["x"],
                 "check_run_url": "https://api.github.com/repos/o/r/check-runs/55",
                 "steps": [{"number": 1, "name": "Run tests", "conclusion": conclusion}, {"number": 2, "name": "Post", "conclusion": "success"}]}, **more)


def check_body(check_id=55, conclusion="failure", status="completed"):
    return {"id": check_id, "name": "test", "head_sha": SHA, "status": status, "conclusion": conclusion, "started_at": "2026-11-01T09:01:00Z", "completed_at": "2026-11-01T09:05:00Z",
            "app": {"slug": "github-actions", "id": 15368, "owner": {"login": "SECRET"}}, "check_suite": {"id": 900}, "details_url": "https://github.com/o/r/actions/runs/7/job/11",
            "output": {"summary": "SECRET", "text": "SECRET"}}


def status_body(status_id=70, state="failure"):
    return {"id": status_id, "context": "review-gate", "state": state, "created_at": "2026-11-01T09:00:00Z", "updated_at": "2026-11-01T09:00:00Z",
            "creator": {"login": "bot"}, "description": "SECRET", "target_url": "https://x/SECRET"}


def jobs_path(run_id=7, attempt=1):
    return f"{BASE}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ledger_store.Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.store.begin_pass(NOW)
        self.head()

    def head(self, sha=SHA, state="OPEN", current=SHA, pushed="2026-11-01T08:00:00Z", number=1):
        with self.store.tx():
            self.store.db.execute("""INSERT OR REPLACE INTO prs(repo, number, state, created_at, updated_at, head_ref, head_sha, head_repo, cross_repo, base_ref, timeline_complete, first_pass, last_pass)
                                     VALUES ('web', ?, ?, '2026-10-30T00:00:00Z', '2026-10-30T00:00:00Z', 'b', ?, 'example-org/example-web', 0, 'main', 1, 1, 1)""", (number, state, current))
            self.store.db.execute("INSERT OR REPLACE INTO ci_heads(repo, sha, number, pushed_at, discovered_pass, state, due_at) VALUES ('web', ?, ?, ?, 1, 'queued', 0)", (sha, number, pushed))

    def routes(self, runs=(), checks=(), statuses=(), **more):
        return dict({RUNS: {"total_count": len(runs), "workflow_runs": list(runs)}, CHECKS: {"total_count": len(checks), "check_runs": list(checks)}, STATUSES: list(statuses)}, **more)

    def run_pass(self, fake, now=NOW, remaining=None):
        client = ledger_github.Client(runner=fake)
        client.start()
        try:
            ledger_ci.run(self.store, client, now)
        except ledger_github.Stop:
            pass
        return client

    def rows(self, sql, *args):
        return [tuple(r) for r in self.store.db.execute(sql, args).fetchall()]

    def state(self):
        return self.rows("SELECT state FROM ci_heads WHERE sha = ?", SHA)[0][0]

    def gaps(self):
        return [r[0] for r in self.rows("SELECT kind FROM gaps WHERE closed_pass IS NULL")]


class Evidence(Base):
    def test_a_failed_run_keeps_every_clock_app_and_sha(self):
        routes = self.routes([run_body()], [check_body()], [status_body()])
        routes[jobs_path()] = {"total_count": 1, "jobs": [job_body()]}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.rows("SELECT run_id, run_attempt, conclusion, tested_sha, requested_at, started_at, event, pr_numbers FROM ci_run_attempts"),
                         [(7, 1, "failure", SHA, "2026-11-01T09:00:00Z", "2026-11-01T09:00:05Z", "pull_request", "[1]")])
        self.assertEqual(self.rows("SELECT conclusion, queued_at, started_at, completed_at, check_run_id FROM ci_jobs"),
                         [("failure", "2026-11-01T09:00:10Z", "2026-11-01T09:01:00Z", "2026-11-01T09:05:00Z", 55)])
        self.assertEqual(self.rows("SELECT name, app_slug, app_id, tested_sha, actions_run_id FROM ci_check_runs"), [("test", "github-actions", 15368, SHA, 7)])
        self.assertEqual(self.rows("SELECT context, state, creator FROM ci_statuses"), [("review-gate", "failure", "bot")])

    def test_passes_are_kept_as_well_as_failures(self):
        routes = self.routes([run_body(conclusion="success")])
        routes[jobs_path()] = {"total_count": 1, "jobs": [job_body(conclusion="success")]}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.rows("SELECT conclusion FROM ci_jobs"), [("success",)])

    def test_the_first_failure_survives_a_green_rerun(self):
        routes = self.routes([run_body()])
        routes[jobs_path()] = {"total_count": 1, "jobs": [job_body()]}
        fake = FakeGitHub(routes)
        self.run_pass(fake)
        routes.update(self.routes([run_body(attempt=2, conclusion="success")]))
        routes[jobs_path(attempt=2)] = {"total_count": 1, "jobs": [job_body(job_id=12, attempt=2, conclusion="success")]}
        with self.store.tx():
            self.store.db.execute("UPDATE ci_heads SET due_at = 0")
        self.run_pass(fake, NOW + 3600)
        self.assertEqual(self.rows("SELECT run_attempt, conclusion FROM ci_attempt_latest ORDER BY run_attempt"), [(1, "failure"), (2, "success")])
        self.assertEqual(self.rows("SELECT run_attempt, conclusion FROM ci_job_latest ORDER BY run_attempt"), [(1, "failure"), (2, "success")])
        self.assertEqual(self.rows("SELECT level, label FROM ci_retry_outcomes ORDER BY level"), [("job", "retry recovered"), ("run", "retry recovered")])

    def test_an_attempt_rerun_between_passes_is_fetched_by_number(self):
        routes = self.routes([run_body(attempt=2, conclusion="success")])
        routes[f"{BASE}/actions/runs/7/attempts/1"] = run_body()
        routes[jobs_path()] = {"total_count": 1, "jobs": [job_body()]}
        routes[jobs_path(attempt=2)] = {"total_count": 0, "jobs": []}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.rows("SELECT run_attempt, conclusion FROM ci_attempt_latest ORDER BY run_attempt"), [(1, "failure"), (2, "success")])
        self.assertEqual(self.rows("SELECT run_attempt FROM ci_jobs"), [(1,)])

    def test_an_attempt_github_no_longer_holds_is_a_gap_not_a_guess(self):
        routes = self.routes([run_body(attempt=2, conclusion="success")])
        routes[jobs_path(attempt=2)] = {"total_count": 0, "jobs": []}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual((self.rows("SELECT count(*) FROM ci_attempt_latest"), self.gaps()), ([(1,)], ["attempt-not-retained"]))

    def test_step_names_keep_only_unsuccessful_steps_and_no_logs(self):
        routes = self.routes([run_body()])
        routes[jobs_path()] = {"total_count": 1, "jobs": [job_body()]}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(json.loads(self.rows("SELECT steps_unsuccessful FROM ci_jobs")[0][0]), [{"number": 1, "name": "Run tests", "conclusion": "failure"}])

    def test_free_text_never_reaches_the_file(self):
        routes = self.routes([run_body()], [check_body()], [status_body()])
        routes[jobs_path()] = {"total_count": 1, "jobs": [job_body()]}
        self.run_pass(FakeGitHub(routes))
        self.store.checkpoint()
        with open(self.store.path, "rb") as f:
            self.assertNotIn(b"SECRET", f.read())

    def test_a_job_finishing_before_it_started_keeps_github_clocks(self):
        routes = self.routes([run_body()])
        routes[jobs_path()] = {"total_count": 1, "jobs": [job_body(started_at="2026-11-01T09:19:32Z", completed_at="2026-11-01T09:19:23Z")]}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.rows("SELECT started_at, completed_at FROM ci_jobs"), [("2026-11-01T09:19:32Z", "2026-11-01T09:19:23Z")])

    def test_an_attempt_clock_is_unknown_until_every_job_finished(self):
        routes = self.routes([run_body()])
        routes[jobs_path()] = {"total_count": 2, "jobs": [job_body(), job_body(job_id=12, status="in_progress", completed_at=None)]}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.rows("SELECT queued_at, completed_at FROM ci_attempt_clocks"), [("2026-11-01T09:00:10Z", None)])


def sweep_path(page=1, repo="web"):
    return f"repos/example-org/example-{repo}/actions/runs?per_page=100&page={page}"


def front_path(since, repo="web"):
    return f"repos/example-org/example-{repo}/actions/runs?created=%3E%3D{since}&per_page=100"


class Feed(Base):
    def state(self):
        return ledger_ci.feed_state(self.store, "web")

    def old_settled_head(self, sha=SHA):
        self.head(sha=sha, state="MERGED", current="b" * 40, pushed="2026-01-02T00:00:00Z")
        with self.store.tx():
            self.store.db.execute("UPDATE ci_heads SET state = 'settled', due_at = 0, last_read_at = '2026-01-15T00:00:00Z' WHERE sha = ?", (sha,))

    def test_a_rerun_of_an_old_head_is_found_by_the_sweep_though_created_at_never_moved(self):
        self.old_settled_head()
        rerun = run_body(attempt=2, conclusion="failure", created_at="2026-01-02T00:10:00Z", run_started_at="2026-11-01T11:00:00Z")
        fake = FakeGitHub(self.routes([rerun]))
        fake.routes[sweep_path()] = {"workflow_runs": [rerun]}
        fake.routes[f"{BASE}/actions/runs/7/attempts/1"] = run_body(conclusion="success", created_at="2026-01-02T00:10:00Z")
        fake.routes[jobs_path()] = {"total_count": 0, "jobs": []}
        fake.routes[jobs_path(attempt=2)] = {"total_count": 1, "jobs": [job_body(job_id=12, attempt=2)]}
        self.run_pass(fake)
        self.assertEqual(self.rows("SELECT run_attempt, conclusion FROM ci_attempt_latest ORDER BY run_attempt"), [(1, "success"), (2, "failure")])
        self.assertEqual(self.rows("SELECT run_attempt, conclusion FROM ci_jobs"), [(2, "failure")])

    def test_the_created_filter_alone_would_miss_that_rerun(self):
        self.old_settled_head()
        rerun = run_body(attempt=2, conclusion="failure", created_at="2026-01-02T00:10:00Z")
        since = ledger_store.iso(NOW - 2 * ledger_ci.FEED_OVERLAP)
        fake = FakeGitHub({front_path(since): {"workflow_runs": []}})
        self.run_pass(fake)
        self.assertEqual(self.rows("SELECT count(*) FROM ci_run_attempts"), [(0,)])
        fake.routes[sweep_path()] = {"workflow_runs": [rerun]}
        self.run_pass(fake, NOW + 900)
        self.assertEqual(self.rows("SELECT run_attempt FROM ci_run_attempts"), [(2,)])

    def test_a_new_run_on_an_old_head_comes_through_the_created_feed(self):
        self.old_settled_head()
        since = ledger_store.iso(NOW - 2 * ledger_ci.FEED_OVERLAP)
        fresh = run_body(created_at="2026-11-01T11:59:00Z")
        fake = FakeGitHub({front_path(since): {"workflow_runs": [fresh]}})
        self.run_pass(fake)
        self.assertEqual((self.rows("SELECT run_id FROM ci_run_attempts"), self.state()["through"]), ([(7,)], "2026-11-01T11:59:00Z"))

    def test_a_run_for_a_finished_head_brings_it_back_for_a_full_read(self):
        self.old_settled_head()
        rerun = run_body(attempt=1, status="in_progress", conclusion=None)
        routes = self.routes([rerun], [check_body()], [status_body()])
        routes[sweep_path()] = {"workflow_runs": [rerun]}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual((self.rows("SELECT state FROM ci_heads"), self.rows("SELECT count(*) FROM ci_check_runs")), ([("live",)], [(1,)]))

    def test_a_not_retained_head_with_new_evidence_closes_its_gap(self):
        self.old_settled_head()
        with self.store.tx():
            self.store.db.execute("UPDATE ci_heads SET state = 'not_retained'")
            self.store.open_gap("ci", "web", SHA, "not-retained", "x", NOW)
        rerun = run_body()
        routes = self.routes([rerun])
        routes[sweep_path()] = {"workflow_runs": [rerun]}
        routes[jobs_path()] = {"total_count": 0, "jobs": []}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.gaps(), [])

    def test_a_run_for_an_unknown_head_is_not_recorded(self):
        fake = FakeGitHub()
        fake.routes[sweep_path()] = {"workflow_runs": [run_body(head_sha="f" * 40)]}
        self.run_pass(fake)
        self.assertEqual(self.rows("SELECT count(*) FROM ci_run_attempts"), [(0,)])

    def test_the_cursor_persists_with_overlap_and_a_quiet_repo_is_conditional(self):
        since = ledger_store.iso(ledger_store.epoch("2026-11-01T11:59:00Z") - ledger_ci.FEED_OVERLAP)
        first = ledger_store.iso(NOW - 2 * ledger_ci.FEED_OVERLAP)
        fake = FakeGitHub({front_path(first): {"workflow_runs": [run_body(created_at="2026-11-01T11:59:00Z", head_sha="f" * 40)]},
                           front_path(since): http(200, {"workflow_runs": []}, {"ETag": 'W/"q"'})})
        self.run_pass(fake)
        self.run_pass(fake, NOW + 900)
        fake.routes[front_path(since)] = lambda p, e: http(304)
        self.run_pass(fake, NOW + 1800)
        self.assertEqual([c[1] for c in fake.calls if c[0] == front_path(since)], [None, 'W/"q"'])

    def test_a_feed_too_busy_to_finish_is_a_gap_until_a_sweep_passes(self):
        since = ledger_store.iso(NOW - 2 * ledger_ci.FEED_OVERLAP)
        page = {"workflow_runs": [run_body(run_id=i, created_at="2026-11-01T11:00:00Z", head_sha="f" * 40) for i in range(100)]}
        routes = self.routes(**{front_path(since): page, f"{front_path(since)}&page=2": page, f"{front_path(since)}&page=3": page})
        routes.update({sweep_path(n): page for n in range(1, 60)})
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.gaps(), ["feed-behind"])
        with self.store.tx():
            self.store.set_cursor("feed:web", dict(self.state(), page=1))
        routes.pop(sweep_path(1))
        self.run_pass(FakeGitHub(routes), NOW + 900)
        self.assertEqual(self.gaps(), [])

    def test_a_sweep_begun_after_the_hole_closes_it(self):
        with self.store.tx():
            self.store.set_cursor("feed:web", dict(ledger_ci.feed_state(self.store, "web"), hole=NOW - 900))
            self.store.open_gap("ci", "web", "hole", "feed-behind", "x", NOW - 900)
        self.run_pass(FakeGitHub(self.routes()))
        self.assertEqual(self.gaps(), [])

    def test_the_sweep_resumes_its_page_across_passes(self):
        page = {"workflow_runs": [run_body(run_id=i, head_sha="f" * 40) for i in range(100)]}
        routes = {sweep_path(n): page for n in range(1, 60)}
        self.run_pass(FakeGitHub(routes))
        self.assertGreater(self.state()["page"], 1)
        held = self.state()["page"]
        self.run_pass(FakeGitHub(routes), NOW + 900)
        self.assertGreater(self.state()["page"], held)

    def test_the_feed_keeps_to_its_cap_under_a_long_history(self):
        page = {"workflow_runs": [run_body(run_id=i, head_sha="f" * 40) for i in range(100)]}
        fake = FakeGitHub({sweep_path(n, repo): page for n in range(1, 60) for repo in ("web", "ios", "backend")})
        client = ledger_github.Client(runner=fake)
        client.start()
        ledger_ci.read_feed(self.store, client, NOW)
        self.assertLessEqual(client.requests, ledger_ci.FEED_REQUESTS)

    def test_a_refused_created_feed_is_a_failure_not_fresh_coverage(self):
        since = ledger_store.iso(NOW - 2 * ledger_ci.FEED_OVERLAP)
        for status in (404, 410, 422):
            with self.subTest(status=status):
                before = self.state()
                fake = FakeGitHub(self.routes(**{front_path(since): http(status, {})}))
                client = ledger_github.Client(runner=fake)
                client.start()
                ledger_ci.read_feed(self.store, client, NOW)
                self.assertIsNone(self.state()["front_read"])
                self.assertEqual(self.state()["through"], before["through"])
                self.assertEqual(self.rows("SELECT subject, kind, detail FROM gaps WHERE closed_pass IS NULL AND repo = 'web'"), [("feed", "read-failed", "status 404")])

    def test_a_refused_created_feed_keeps_the_last_good_clock_and_goes_late(self):
        fake = FakeGitHub(self.routes())
        self.run_pass(fake)
        held = self.state()
        since = ledger_store.iso(ledger_store.epoch(held["through"]) - ledger_ci.FEED_OVERLAP)
        fake.routes[front_path(since)] = http(404, {})
        later = NOW + ledger_ci.FRONT_LATE + 1
        self.run_pass(fake, later)
        ledger_ci.check_feed(self.store, later)
        self.assertEqual((self.state()["front_read"], self.state()["through"]), (held["front_read"], held["through"]))
        self.assertEqual(sorted(self.rows("SELECT subject, kind FROM gaps WHERE closed_pass IS NULL AND repo = 'web'")), [("feed", "read-failed"), ("front", "feed-behind")])

    def test_a_refused_sweep_keeps_its_page_and_clock(self):
        for status in (404, 410, 422):
            with self.subTest(status=status):
                fake = FakeGitHub(self.routes(**{sweep_path(): http(status, {})}))
                client = ledger_github.Client(runner=fake)
                client.start()
                ledger_ci.read_feed(self.store, client, NOW)
                self.assertEqual((self.state()["page"], self.state()["swept"]), (1, None))
                self.assertIn(("feed", "read-failed"), self.rows("SELECT subject, kind FROM gaps WHERE closed_pass IS NULL AND repo = 'web'"))

    def test_a_failed_sweep_is_a_gap_and_the_pass_goes_on(self):
        fake = FakeGitHub(self.routes())
        fake.routes[sweep_path()] = http(502, {})
        self.run_pass(fake)
        self.assertEqual((self.gaps(), self.rows("SELECT reads FROM ci_heads")), (["read-failed"], [(1,)]))

    def test_a_feed_that_stopped_says_it_is_behind(self):
        ledger_ci.check_feed(self.store, NOW)
        self.assertEqual(self.gaps(), [])
        ledger_ci.check_feed(self.store, NOW + ledger_ci.FRONT_LATE + 1)
        self.assertEqual(sorted(self.gaps()), ["feed-behind"] * 3)
        ledger_ci.check_feed(self.store, NOW + ledger_ci.SWEEP_LATE + 1)
        self.assertEqual(self.gaps().count("feed-behind"), 6)

    def test_the_budget_floor_reads_no_feed(self):
        fake = FakeGitHub({sweep_path(): {"workflow_runs": []}}, remaining=999)
        self.run_pass(fake)
        self.assertEqual(fake.calls, [])


class Budgeted(Base):
    def test_unfinished_runs_are_reread_and_jobs_wait_for_the_end(self):
        fake = FakeGitHub(self.routes([run_body(status="in_progress", conclusion=None)]))
        self.run_pass(fake)
        self.assertEqual((self.state(), self.rows("SELECT count(*) FROM ci_fetches")), ("live", [(0,)]))

    def test_rereads_are_conditional(self):
        routes = self.routes([run_body(status="in_progress", conclusion=None)])
        routes[RUNS] = http(200, routes[RUNS], {"ETag": 'W/"r"'})
        fake = FakeGitHub(routes)
        self.run_pass(fake)
        routes[RUNS] = lambda p, e: http(304)
        fake.calls.clear()
        self.run_pass(fake, NOW + ledger_ci.LIVE_EVERY)
        self.assertEqual([c for c in fake.calls if c[0] == RUNS], [(RUNS, 'W/"r"')])

    def settled_head(self):
        self.head(pushed="2026-10-31T00:00:00Z", current="b" * 40, state="MERGED")
        routes = self.routes([run_body(conclusion="success")])
        routes[RUNS] = http(200, routes[RUNS], {"ETag": 'W/"r"'})
        routes[jobs_path()] = {"total_count": 0, "jobs": []}
        fake = FakeGitHub(routes)
        self.run_pass(fake)
        self.assertEqual(self.state(), "settled")
        fake.calls.clear()
        return fake

    def test_a_settled_head_is_not_read_again_by_itself(self):
        fake = self.settled_head()
        self.run_pass(fake, NOW + 86400)
        self.assertEqual(fake.calls, [])

    def test_a_current_head_stays_watched_for_reruns(self):
        fake = FakeGitHub(self.routes([run_body(conclusion="success")], **{jobs_path(): {"total_count": 0, "jobs": []}}))
        self.run_pass(fake)
        self.assertEqual(self.rows("SELECT state, due_at FROM ci_heads"), [("live", NOW + ledger_ci.RECHECK_EVERY)])

    def test_an_expired_head_with_nothing_is_not_retained_not_passed(self):
        self.head(pushed="2026-10-01T00:00:00Z", current="b" * 40)
        self.run_pass(FakeGitHub(self.routes()))
        self.assertEqual((self.state(), self.gaps()), ("not_retained", ["not-retained"]))

    def test_a_refused_head_listing_is_never_evidence_of_nothing(self):
        for path in (RUNS, CHECKS, STATUSES):
            for status in (404, 410, 422):
                with self.subTest(path=path, status=status):
                    self.head(pushed="2026-10-01T00:00:00Z", current="b" * 40)
                    routes = self.routes()
                    routes[path] = http(status, {})
                    self.run_pass(FakeGitHub(routes))
                    self.assertEqual((self.state(), self.gaps()), ("unreadable", ["read-failed"]))
                    with self.store.tx():
                        self.store.close_gap("ci", "web", SHA, "read-failed", NOW)

    def test_a_refused_head_inside_retention_retries_daily_with_its_gap(self):
        routes = self.routes()
        routes[CHECKS] = http(404, {})
        self.run_pass(FakeGitHub(routes))
        self.assertEqual((self.rows("SELECT state, due_at FROM ci_heads"), self.gaps()), ([("queued", NOW + ledger_ci.GONE_RETRY)], ["read-failed"]))

    def test_a_run_found_later_returns_an_unreadable_head(self):
        self.head(pushed="2026-10-01T00:00:00Z", current="b" * 40)
        routes = self.routes()
        routes[RUNS] = http(404, {})
        fake = FakeGitHub(routes)
        self.run_pass(fake)
        self.assertEqual(self.state(), "unreadable")
        routes.update(self.routes([run_body()]))
        routes[jobs_path()] = {"total_count": 0, "jobs": []}
        routes[sweep_path()] = {"workflow_runs": [run_body()]}
        self.run_pass(fake, NOW + 900)
        self.assertEqual((self.state(), self.gaps()), ("settled", []))

    def unreadable_head(self):
        self.head(pushed="2026-10-01T00:00:00Z", current="b" * 40)
        routes = self.routes()
        routes[CHECKS] = http(404, {})
        self.run_pass(FakeGitHub(routes))
        self.assertEqual((self.state(), self.gaps()), ("unreadable", ["read-failed"]))
        return routes

    def test_a_feed_observation_alone_never_closes_a_read_failure(self):
        self.unreadable_head()
        with self.store.tx():
            ledger_ci.take_feed_rows(self.store, "web", [run_body()], NOW)
        self.assertEqual((self.state(), self.gaps(), self.rows("SELECT count(*) FROM ci_check_runs")), ("live", ["read-failed"], [(0,)]))

    def test_a_pass_stopped_after_the_feed_leaves_the_read_failure_open(self):
        routes = self.unreadable_head()
        routes[sweep_path()] = http(200, {"workflow_runs": [run_body()]}, remaining=999)
        fake = FakeGitHub(routes)
        fake.calls.clear()
        self.run_pass(fake, NOW + 900)
        self.assertEqual((self.state(), self.gaps(), [c for c in fake.calls if c[0] in (RUNS, CHECKS, STATUSES)]), ("live", ["read-failed"], []))

    def test_a_reread_that_still_fails_a_listing_leaves_the_read_failure_open(self):
        routes = self.unreadable_head()
        routes[RUNS] = {"workflow_runs": [run_body()]}
        routes[sweep_path()] = {"workflow_runs": [run_body()]}
        routes[jobs_path()] = {"total_count": 0, "jobs": []}
        self.run_pass(FakeGitHub(routes), NOW + 900)
        self.assertEqual((self.state(), self.gaps()), ("unreadable", ["read-failed"]))

    def test_a_whole_reread_closes_the_read_failure(self):
        routes = self.unreadable_head()
        routes.update(self.routes([run_body()], [check_body()]))
        routes[sweep_path()] = {"workflow_runs": [run_body()]}
        routes[jobs_path()] = {"total_count": 0, "jobs": []}
        self.run_pass(FakeGitHub(routes), NOW + 900)
        self.assertEqual((self.state(), self.gaps(), self.rows("SELECT count(*) FROM ci_check_runs")), ("settled", [], [(1,)]))

    def test_a_new_head_with_nothing_yet_stays_live(self):
        self.run_pass(FakeGitHub(self.routes()))
        self.assertEqual((self.state(), self.gaps()), ("live", []))

    def test_the_budget_floor_leaves_the_queue_intact(self):
        fake = FakeGitHub(self.routes(), remaining=999)
        self.run_pass(fake)
        self.assertEqual((fake.calls, self.state()), ([], "queued"))

    def test_a_pass_cannot_exceed_its_limit_and_resumes_next_pass(self):
        for n in range(ledger_github.PASS_REQUESTS):
            self.head(sha=f"{n:040x}", current="b" * 40, pushed="2026-10-30T00:00:00Z", number=n + 2)
        fake = FakeGitHub()
        def anything(command, limit, timeout):
            return fake(command, limit, timeout) if command[-1] == "rate_limit" else ledger_ci.ledger_github.capture.Answer(0, http(200, {"workflow_runs": [], "check_runs": []}))
        client = self.run_pass(anything)
        self.assertEqual(client.requests, ledger_github.PASS_REQUESTS)
        self.assertGreater(self.rows("SELECT count(*) FROM ci_heads WHERE state = 'queued'")[0][0], 0)

    def test_job_reads_get_a_share_of_a_pass_that_starts_with_no_backlog(self):
        for n in range(ledger_github.PASS_REQUESTS):
            self.head(sha=f"{n:040x}", current="b" * 40, pushed="2026-10-30T00:00:00Z", number=n + 2)
        fake = FakeGitHub()
        serial = iter(range(1000, 9000))
        def anything(command, limit, timeout):
            path = command[command.index("GET") + 1]
            if path == "rate_limit":
                return fake(command, limit, timeout)
            run = run_body(run_id=next(serial))
            return ledger_github.capture.Answer(0, http(200, {"workflow_runs": [run] if "head_sha" in path else [], "check_runs": [], "jobs": [job_body()] if "/jobs" in path else [], "total_count": 1}))
        self.run_pass(anything)
        self.assertGreater(self.rows("SELECT count(*) FROM ci_jobs")[0][0], 0)

    def test_a_failed_read_retries_later_with_a_gap(self):
        routes = self.routes()
        routes[RUNS] = http(502, {})
        self.run_pass(FakeGitHub(routes))
        self.assertEqual((self.state(), self.gaps()), ("queued", ["read-failed"]))

    def test_the_oldest_head_inside_retention_goes_before_a_newer_one(self):
        self.head(sha="b" * 40, current="c" * 40, pushed="2026-10-30T00:00:00Z", number=2)
        self.head(sha="d" * 40, current="c" * 40, pushed="2026-10-19T00:00:00Z", number=3)
        self.head(sha="e" * 40, current="c" * 40, pushed="2026-10-31T00:00:00Z", number=4)
        order = [h["sha"][0] for h in ledger_ci.due_heads(self.store, NOW)]
        self.assertEqual(order, ["a", "d", "b", "e"])

    def test_a_truncated_listing_is_explicit(self):
        routes = self.routes()
        routes[CHECKS] = {"check_runs": [check_body(i) for i in range(100)]}
        for page in (2, 3):
            routes[f"{CHECKS}&page={page}"] = {"check_runs": [check_body(page * 1000 + i) for i in range(100)]}
        self.run_pass(FakeGitHub(routes))
        self.assertEqual(self.gaps(), ["checks-truncated"])


if __name__ == "__main__":
    unittest.main()
