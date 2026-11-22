import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collectors"))
import collect  # noqa: E402
from store import iso  # noqa: E402

NOW = 1795000000
REPOS = [{"name": "acme/api", "color": "#C9A227"}, {"name": "acme/ios", "color": "#3FA45B"}, {"name": "acme/web", "color": "#4682B4"}]


def check(name, status, conclusion=None, started=None, completed=None, queued=None):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion, "startedAt": started,
            "completedAt": completed, "checkSuite": {"createdAt": queued or started}}


def status_context(name, state, at):
    return {"__typename": "StatusContext", "context": name, "state": state, "createdAt": at}


def live_pr(number, title, created, *, draft=False, checks=(), reviews=(), requested=(), decision=None, author="dev", ready=None, head_at=None):
    timeline = [{"__typename": "ReadyForReviewEvent", "createdAt": ready}] if ready else []
    return {"number": number, "title": title, "url": f"https://github.com/example/pull/{number}", "isDraft": draft,
            "createdAt": created, "updatedAt": created, "headRefName": f"work/{number}", "baseRefName": "main",
            "reviewDecision": decision, "author": {"login": author},
            "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": r}} for r in requested]},
            "latestReviews": {"nodes": [{"state": s, "submittedAt": at, "author": {"login": who}} for s, at, who in reviews]},
            "timelineItems": {"nodes": timeline},
            "commits": {"nodes": [{"commit": {"oid": "a" * 40, "committedDate": head_at or created,
                                              "statusCheckRollup": {"state": None, "contexts": {"nodes": list(checks)}}}}]}}


def ago(seconds, now=NOW):
    return iso(now - seconds)


def put_live(store, repo, prs, viewer="me", now=NOW, default_branch="main"):
    body = {"viewer": viewer, "default_branch": default_branch, "archived": False, "total": len(prs), "truncated": False, "prs": prs}
    with store.tx():
        store.db.execute("INSERT INTO snapshots(repo, read_at, body) VALUES (?, ?, ?) ON CONFLICT(repo) DO UPDATE SET read_at = excluded.read_at, body = excluded.body",
                         (repo, iso(now), json.dumps(body)))
        store.read_ok(repo, "live", now)


def put_history(store, repo, seed, now=NOW, waves=(), slow_days=()):
    rng = random.Random(seed)
    number = 1000
    with store.tx():
        for back in range(89, -1, -1):
            day_start = now - back * 86400 - (now % 86400)
            count = rng.choice((0, 1, 2, 3, 4)) + (rng.randint(18, 30) if back in waves else 0)
            for _ in range(count):
                number += 1
                created = day_start + rng.randint(0, 86000)
                merged = created + rng.randint(1800, 3 * 86400) if rng.random() < 0.85 else None
                merged = merged if merged and merged < now else None
                collect.take_pr(store, repo, {"number": number, "title": f"Change {number}", "state": "MERGED" if merged else "OPEN" if back < 3 else "CLOSED",
                                              "isDraft": False, "createdAt": iso(created), "updatedAt": iso(merged or created), "mergedAt": iso(merged) if merged else None,
                                              "closedAt": iso(merged) if merged else (None if back < 3 else iso(created + 86400)), "author": {"login": "dev"},
                                              "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "reviews": {"nodes": []}})
                for job in range(3):
                    queued = created + 60 + job
                    wait = rng.randint(5, 90) if back not in slow_days else rng.randint(600, 9000)
                    run_id = number * 10 + job
                    store.db.execute("""INSERT INTO ci_run_attempts(repo, run_id, run_attempt, workflow_name, status, conclusion, requested_at, jobs_read)
                                        VALUES (?, ?, 1, 'ci', 'completed', 'success', ?, 1)""", (repo, run_id, iso(queued)))
                    store.db.execute("""INSERT INTO ci_jobs(repo, run_id, run_attempt, job_id, name, status, conclusion, queued_at, started_at, completed_at)
                                        VALUES (?, ?, 1, ?, 'test', 'completed', 'success', ?, ?, ?)""",
                                     (repo, run_id, run_id, iso(queued), iso(queued + wait), iso(queued + wait + 300)))
        store.set_cursor(f"inventory:{repo}", {"covered_to": iso(now), "walk": None})
        store.set_cursor(f"runs-back:{repo}", {"next": "2000-01-01"})
        store.read_ok(repo, "history", now)
        store.read_ok(repo, "ci", now)


def sample_live():
    return {
        "acme/api": [
            live_pr(412, "Add rate limits to the public API", ago(5 * 3600), checks=[check("test", "COMPLETED", "SUCCESS", ago(4 * 3600), ago(3 * 3600))], requested=["me"]),
            live_pr(415, "Cache account lookups", ago(9 * 3600), checks=[check("test", "COMPLETED", "FAILURE", ago(8 * 3600), ago(7 * 3600))], head_at=ago(8 * 3600)),
            live_pr(417, "Move billing jobs to the new queue", ago(3 * 3600), checks=[check("test", "IN_PROGRESS", None, ago(600))]),
            live_pr(418, "Retry webhook deliveries", ago(2 * 3600), checks=[check("test", "QUEUED", None, None, None, ago(1500)), check("lint", "QUEUED", None, None, None, ago(1500))]),
            live_pr(419, "Drop the legacy auth path", ago(26 * 3600), checks=[check("test", "COMPLETED", "SUCCESS", ago(25 * 3600), ago(24 * 3600))],
                    reviews=[("APPROVED", ago(5400), "alice")], decision="APPROVED"),
            live_pr(420, "Sketch the export endpoint", ago(30 * 3600), draft=True),
        ],
        "acme/ios": [
            live_pr(88, "Offline mode for the inbox", ago(20 * 3600), checks=[check("build", "COMPLETED", "SUCCESS", ago(19 * 3600), ago(18 * 3600))], requested=["alice"]),
            live_pr(90, "Fix crash on rotate", ago(4 * 3600), checks=[check("build", "IN_PROGRESS", None, ago(1200))]),
            live_pr(91, "Dark mode for settings", ago(7 * 3600), decision="CHANGES_REQUESTED", reviews=[("CHANGES_REQUESTED", ago(3 * 3600), "alice")],
                    checks=[check("build", "COMPLETED", "SUCCESS", ago(6 * 3600), ago(5 * 3600))]),
        ],
        "acme/web": [
            live_pr(1201, "New pricing page", ago(6 * 3600), checks=[check("ci", "COMPLETED", "SUCCESS", ago(5 * 3600), ago(5 * 3600 - 600))]),
            live_pr(1203, "Bump the chart library", ago(50 * 3600), author="dependabot", checks=[check("ci", "COMPLETED", "SUCCESS", ago(49 * 3600), ago(48 * 3600))],
                    reviews=[("APPROVED", ago(47 * 3600), "me")], decision="APPROVED"),
            live_pr(1204, "Speed up the landing page", ago(3600), checks=[check("ci", "QUEUED", None, None, None, ago(900))]),
            live_pr(1205, "Try a new nav", ago(10 * 3600), draft=True),
        ],
    }


def build_sample_store(store, now=NOW):
    live = sample_live()
    for index, repo in enumerate(REPOS):
        put_history(store, repo["name"], seed=index + 1, now=now, waves=(64, 29) if index == 0 else (29,), slow_days=(28, 27, 26) if index == 0 else ())
        put_live(store, repo["name"], live[repo["name"]], now=now)
