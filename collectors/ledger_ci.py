import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_github
from ledger_github import OWNER, REPOS, Unreadable
from ledger_store import digest, epoch, iso, normal_run

RETENTION = 14 * 86400
LIVE_EVERY = 900
RECHECK_EVERY = 3600
SETTLE = 6 * 3600
FEED_REQUESTS = 12
FEED_OVERLAP = 1800
FRONT_LATE = 3600
SWEEP_LATE = 86400
GONE_RETRY = 86400
FINISHED_HEAD = ("settled", "none_seen", "not_retained", "unreadable")
PER_PAGE = 100
MAX_PAGES = 3
MAX_TRIES = 3
SOURCE = "ci"
LIVE_SOURCE = "github-rest"
ACTIONS_RUN = re.compile(r"/actions/runs/(\d+)")
UNSUCCESSFUL_STEP = ("failure", "cancelled", "timed_out", "action_required", "startup_failure")
GONE = (404, 410, 422)


class Failed(Exception):
    def __init__(self, reason, gone=False):
        super().__init__(reason)
        self.gone = gone


def base(repo):
    return f"repos/{OWNER}/{REPOS[repo]}"


def normal_job(repo, job, observed_at):
    job_id, attempt, run_id = job.get("id"), job.get("run_attempt"), job.get("run_id")
    if type(job_id) is not int or type(attempt) is not int or type(run_id) is not int:
        return None
    steps = job.get("steps") if isinstance(job.get("steps"), list) else []
    unsuccessful = [{"number": s.get("number"), "name": s.get("name"), "conclusion": s.get("conclusion")}
                    for s in steps if isinstance(s, dict) and s.get("conclusion") in UNSUCCESSFUL_STEP]
    link = re.search(r"/check-runs/(\d+)$", job.get("check_run_url") or "")
    fields = {"repo": repo, "run_id": run_id, "run_attempt": attempt, "job_id": job_id, "name": job.get("name"), "workflow_name": job.get("workflow_name"),
              "tested_sha": job.get("head_sha"), "head_branch": job.get("head_branch"), "status": job.get("status"), "conclusion": job.get("conclusion"),
              "queued_at": job.get("created_at"), "started_at": job.get("started_at"), "completed_at": job.get("completed_at"), "runner_name": job.get("runner_name"),
              "runner_group": job.get("runner_group_name"), "labels": json.dumps(job.get("labels") or []), "steps_total": len(steps),
              "steps_unsuccessful": json.dumps(unsuccessful), "check_run_id": int(link.group(1)) if link else None}
    return dict(fields, digest=digest(fields), observed_at=observed_at)


def normal_check(repo, check, observed_at):
    check_id = check.get("id")
    if type(check_id) is not int:
        return None
    app = check.get("app") if isinstance(check.get("app"), dict) else {}
    suite = check.get("check_suite") if isinstance(check.get("check_suite"), dict) else {}
    run = ACTIONS_RUN.search(check.get("details_url") or "")
    fields = {"repo": repo, "check_run_id": check_id, "name": check.get("name"), "tested_sha": check.get("head_sha"), "status": check.get("status"),
              "conclusion": check.get("conclusion"), "started_at": check.get("started_at"), "completed_at": check.get("completed_at"), "app_slug": app.get("slug"),
              "app_id": app.get("id"), "check_suite_id": suite.get("id"), "actions_run_id": int(run.group(1)) if run else None}
    return dict(fields, digest=digest(fields), observed_at=observed_at)


def insert(store, table, row):
    names = list(row) + ["recorded_pass"]
    marks = ", ".join(f":{n}" for n in names)
    return store.db.execute(f"INSERT OR IGNORE INTO {table}({', '.join(names)}) VALUES ({marks})", dict(row, recorded_pass=store.pass_id)).rowcount


def add_status(store, repo, sha, status, observed_at):
    status_id = status.get("id")
    if type(status_id) is not int:
        return 0
    return store.db.execute(
        "INSERT OR IGNORE INTO ci_statuses(repo, status_id, tested_sha, context, state, created_at, updated_at, creator, observed_at, recorded_pass) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (repo, status_id, sha, status.get("context"), status.get("state"), status.get("created_at"), status.get("updated_at"),
         (status.get("creator") or {}).get("login"), observed_at, store.pass_id)).rowcount


def want(store, repo, run_id, attempt, kind, requested_at):
    store.db.execute("INSERT OR IGNORE INTO ci_fetches(repo, run_id, attempt, kind, state, requested_at) VALUES (?, ?, ?, ?, 'todo', ?)", (repo, run_id, attempt, kind, requested_at))


def plan_run(store, row):
    repo, run_id, attempt, requested = row["repo"], row["run_id"], row["run_attempt"], row["requested_at"]
    if row["status"] == "completed":
        want(store, repo, run_id, attempt, "jobs", requested)
    for earlier in range(1, attempt):
        known = store.db.execute("SELECT status FROM ci_attempt_latest WHERE repo = ? AND run_id = ? AND run_attempt = ?", (repo, run_id, earlier)).fetchone()
        if known is None:
            want(store, repo, run_id, earlier, "attempt", requested)
        elif known[0] == "completed":
            want(store, repo, run_id, earlier, "jobs", requested)


def listing(store, client, path, key):
    first = f"{path}{'&' if '?' in path else '?'}per_page={PER_PAGE}"
    etag = store.etag(first)
    rows, kept_etag = [], None
    for page in range(1, MAX_PAGES + 1):
        url = first if page == 1 else f"{first}&page={page}"
        reply = client.rest(url, etag if page == 1 else None)
        if reply.not_modified:
            return None, None, True
        if not reply.ok:
            raise Failed(f"status {reply.status}", reply.status in GONE)
        body = reply.body.get(key) if key and isinstance(reply.body, dict) else reply.body
        if not isinstance(body, list):
            raise Failed("unreadable listing")
        if page == 1:
            kept_etag = reply.etag
        rows += body
        if len(body) < PER_PAGE:
            return rows, (first, kept_etag), True
    return rows, (first, kept_etag), False


def take_runs(store, client, repo, sha, now):
    stamp = iso(now)
    runs, tag, whole = listing(store, client, f"{base(repo)}/actions/runs?head_sha={sha}", "workflow_runs")
    added = 0
    with store.tx():
        for run in runs or []:
            row = normal_run(repo, run) if isinstance(run, dict) else None
            if row is None:
                store.open_gap(SOURCE, repo, sha, "row-malformed", "a run without identity was refused", now)
                continue
            added += store.add_run(row, LIVE_SOURCE, None, stamp)
            plan_run(store, row)
        if tag:
            store.set_etag(*tag)
        truncation(store, repo, sha, "runs", whole, now)
    return added


def take_head(store, client, head, now):
    repo, sha = head["repo"], head["sha"]
    stamp = iso(now)
    take_runs(store, client, repo, sha, now)
    checks, tag, whole = listing(store, client, f"{base(repo)}/commits/{sha}/check-runs?filter=all", "check_runs")
    with store.tx():
        for check in checks or []:
            row = normal_check(repo, check, stamp) if isinstance(check, dict) else None
            if row:
                insert(store, "ci_check_runs", row)
        if tag:
            store.set_etag(*tag)
        truncation(store, repo, sha, "checks", whole, now)
    statuses, tag, whole = listing(store, client, f"{base(repo)}/commits/{sha}/statuses", None)
    with store.tx():
        for status in statuses or []:
            if isinstance(status, dict):
                add_status(store, repo, sha, status, stamp)
        if tag:
            store.set_etag(*tag)
        truncation(store, repo, sha, "statuses", whole, now)


def truncation(store, repo, sha, what, whole, now):
    if whole:
        store.close_gap(SOURCE, repo, sha, f"{what}-truncated", now)
    else:
        store.open_gap(SOURCE, repo, sha, f"{what}-truncated", f"first {MAX_PAGES * PER_PAGE} kept", now)


def unfinished(store, repo, sha):
    run = store.db.execute("SELECT 1 FROM ci_attempt_latest WHERE repo = ? AND tested_sha = ? AND source = ? AND status <> 'completed' LIMIT 1", (repo, sha, LIVE_SOURCE)).fetchone()
    check = store.db.execute("""SELECT 1 FROM (SELECT status, row_number() OVER (PARTITION BY check_run_id ORDER BY id DESC) AS turn FROM ci_check_runs WHERE repo = ? AND tested_sha = ?)
                                WHERE turn = 1 AND status <> 'completed' LIMIT 1""", (repo, sha)).fetchone()
    status = store.db.execute("""SELECT 1 FROM (SELECT state, row_number() OVER (PARTITION BY context ORDER BY created_at DESC, status_id DESC) AS turn FROM ci_statuses WHERE repo = ? AND tested_sha = ?)
                                 WHERE turn = 1 AND state = 'pending' LIMIT 1""", (repo, sha)).fetchone()
    return bool(run or check or status)


def evidenced(store, repo, sha):
    return any(store.db.execute(query, (repo, sha)).fetchone() for query in (
        "SELECT 1 FROM ci_run_attempts WHERE repo = ? AND tested_sha = ? AND source = 'github-rest' LIMIT 1",
        "SELECT 1 FROM ci_check_runs WHERE repo = ? AND tested_sha = ? LIMIT 1",
        "SELECT 1 FROM ci_statuses WHERE repo = ? AND tested_sha = ? LIMIT 1"))


def forget_etags(store, repo, sha):
    store.db.execute("DELETE FROM etags WHERE path LIKE ?", (f"{base(repo)}/%{sha}%",))


def settle(store, head, now):
    repo, sha = head["repo"], head["sha"]
    age = now - (epoch(head["pushed_at"]) or epoch(head["created_at"]) or now)
    current = head["pr_state"] == "OPEN" and head["current"] == sha
    if unfinished(store, repo, sha):
        return "live", now + LIVE_EVERY
    if evidenced(store, repo, sha):
        if current:
            return "live", now + RECHECK_EVERY
        return ("settled", 0) if age >= SETTLE else ("live", now + RECHECK_EVERY)
    if age >= RETENTION:
        kept = store.db.execute("SELECT count(*) FROM ci_run_attempts WHERE repo = ? AND tested_sha = ?", (repo, sha)).fetchone()[0]
        store.open_gap(SOURCE, repo, sha, "not-retained", f"GitHub holds nothing; {kept} imported run rows", now)
        return "not_retained", 0
    if current or age < SETTLE:
        return "live", now + LIVE_EVERY
    store.open_gap(SOURCE, repo, sha, "none-seen", "no run, check or status inside retention", now)
    return "none_seen", 0


def due_heads(store, now):
    rows = store.db.execute("""SELECT h.*, p.state AS pr_state, p.head_sha AS current, p.created_at AS created_at FROM ci_heads h JOIN prs p ON p.repo = h.repo AND p.number = h.number
                               WHERE h.state IN ('queued', 'live') AND h.due_at <= ?""", (now,)).fetchall()
    def rank(head):
        moment = epoch(head["pushed_at"]) or epoch(head["created_at"]) or 0
        if head["pr_state"] == "OPEN" and head["current"] == head["sha"]:
            return (0, head["reads"] > 0, moment)
        if now - moment < RETENTION:
            return (1, head["reads"] > 0, moment)
        return (2, head["reads"] > 0, -moment)
    return sorted(rows, key=rank)


def read_heads(store, client, now, limit):
    done = 0
    for head in due_heads(store, now):
        if client.requests >= limit:
            break
        try:
            take_head(store, client, head, now)
        except (Failed, Unreadable) as problem:
            gone = getattr(problem, "gone", False)
            ended = gone and now - (epoch(head["pushed_at"]) or epoch(head["created_at"]) or now) >= RETENTION
            with store.tx():
                store.open_gap(SOURCE, head["repo"], head["sha"], "read-failed", str(problem), now)
                store.db.execute("UPDATE ci_heads SET state = ?, due_at = ? WHERE repo = ? AND sha = ?",
                                 ("unreadable" if ended else head["state"], 0 if ended else now + (GONE_RETRY if gone else LIVE_EVERY), head["repo"], head["sha"]))
            continue
        with store.tx():
            state, due = settle(store, head, now)
            store.db.execute("UPDATE ci_heads SET state = ?, due_at = ?, reads = reads + 1, last_read_at = ? WHERE repo = ? AND sha = ?",
                             (state, due, iso(now), head["repo"], head["sha"]))
            store.close_gap(SOURCE, head["repo"], head["sha"], "read-failed", now)
            if state != "live":
                forget_etags(store, head["repo"], head["sha"])
        done += 1
    return done


def feed_state(store, repo):
    return store.cursor(f"feed:{repo}", {"through": None, "front_read": None, "page": 1, "sweep_started": None, "swept": None, "hole": None, "began": None})


def take_feed_rows(store, repo, runs, now):
    for run in runs:
        head = store.db.execute("SELECT state FROM ci_heads WHERE repo = ? AND sha = ?", (repo, run.get("head_sha"))).fetchone() if isinstance(run, dict) else None
        if head is None:
            continue
        row = normal_run(repo, run)
        if row is None:
            store.open_gap(SOURCE, repo, "feed", "row-malformed", "a run without identity was refused", now)
        elif store.add_run(row, LIVE_SOURCE, None, iso(now)):
            plan_run(store, row)
            if head[0] in FINISHED_HEAD:
                store.db.execute("UPDATE ci_heads SET state = 'live', due_at = 0 WHERE repo = ? AND sha = ?", (repo, row["tested_sha"]))
                store.close_gap(SOURCE, repo, row["tested_sha"], "none-seen", now)
                store.close_gap(SOURCE, repo, row["tested_sha"], "not-retained", now)


def read_front(store, client, repo, state, now):
    through = state["through"] or iso(now - FEED_OVERLAP)
    since = iso(epoch(through) - FEED_OVERLAP)
    runs, tag, whole = listing(store, client, f"{base(repo)}/actions/runs?created=%3E%3D{since}", "workflow_runs")
    with store.tx():
        take_feed_rows(store, repo, runs or [], now)
        created = [r["created_at"] for r in runs or [] if isinstance(r, dict) and epoch(r.get("created_at"))]
        state["through"] = max(created + [through])
        state["front_read"] = now
        if tag:
            store.db.execute("DELETE FROM etags WHERE path LIKE ? AND path <> ?", (f"{base(repo)}/actions/runs?created=%", tag[0]))
            store.set_etag(*tag)
        if not whole:
            state["hole"] = now
            store.open_gap(SOURCE, repo, "hole", "feed-behind", f"runs created between {since} and {min(created, default='unknown')} unread until a sweep passes", now)
        store.set_cursor(f"feed:{repo}", state)


def read_sweep(store, client, repo, state, now, allowed):
    for _ in range(allowed):
        if state["page"] == 1:
            state["sweep_started"] = now
        reply = client.rest(f"{base(repo)}/actions/runs?per_page={PER_PAGE}&page={state['page']}")
        runs = reply.body.get("workflow_runs") if reply.ok and isinstance(reply.body, dict) else None
        if not isinstance(runs, list):
            raise Failed(f"status {reply.status}")
        with store.tx():
            take_feed_rows(store, repo, runs, now)
            ended = len(runs) < PER_PAGE
            if ended:
                state["swept"], state["page"] = state["sweep_started"], 1
                if state["hole"] and state["hole"] <= state["swept"]:
                    state["hole"] = None
                    store.close_gap(SOURCE, repo, "hole", "feed-behind", now)
            else:
                state["page"] += 1
            store.set_cursor(f"feed:{repo}", state)
        if ended:
            return


def read_feed(store, client, now):
    opened = client.requests
    states = {repo: feed_state(store, repo) for repo in REPOS}
    for repo, state in states.items():
        try:
            read_front(store, client, repo, state, now)
            with store.tx():
                store.close_gap(SOURCE, repo, "feed", "read-failed", now)
        except (Failed, Unreadable) as problem:
            with store.tx():
                store.open_gap(SOURCE, repo, "feed", "read-failed", str(problem), now)
    for order, (repo, state) in enumerate(states.items()):
        left = FEED_REQUESTS - (client.requests - opened)
        try:
            read_sweep(store, client, repo, state, now, max(0, left // (len(states) - order)))
        except (Failed, Unreadable) as problem:
            with store.tx():
                store.open_gap(SOURCE, repo, "feed", "read-failed", str(problem), now)


def check_feed(store, now):
    with store.tx():
        for repo in REPOS:
            state = feed_state(store, repo)
            if state["began"] is None:
                state["began"] = now
                store.set_cursor(f"feed:{repo}", state)
            for subject, seen, late in (("front", state["front_read"], FRONT_LATE), ("sweep", state["swept"], SWEEP_LATE)):
                if now - (seen or state["began"]) > late:
                    store.open_gap(SOURCE, repo, subject, "feed-behind", f"last covered {iso(seen) if seen else 'never'}", now)
                else:
                    store.close_gap(SOURCE, repo, subject, "feed-behind", now)


def finish(store, todo, state, now, gap=None):
    store.db.execute("UPDATE ci_fetches SET state = ?, tries = tries + 1, done_pass = ? WHERE repo = ? AND run_id = ? AND attempt = ? AND kind = ?",
                     (state, store.pass_id, todo["repo"], todo["run_id"], todo["attempt"], todo["kind"]))
    if gap:
        store.open_gap(SOURCE, todo["repo"], f"run:{todo['run_id']}:{todo['attempt']}:{todo['kind']}", gap[0], gap[1], now)


def retry(store, todo, why, now):
    if todo["tries"] + 1 >= MAX_TRIES:
        finish(store, todo, "failed", now, ("fetch-failed", why))
    else:
        store.db.execute("UPDATE ci_fetches SET tries = tries + 1 WHERE repo = ? AND run_id = ? AND attempt = ? AND kind = ?",
                         (todo["repo"], todo["run_id"], todo["attempt"], todo["kind"]))


def take_attempt(store, client, todo, now):
    reply = client.rest(f"{base(todo['repo'])}/actions/runs/{todo['run_id']}/attempts/{todo['attempt']}")
    with store.tx():
        if reply.status in GONE:
            finish(store, todo, "not_retained", now, ("attempt-not-retained", f"status {reply.status}"))
            return
        row = normal_run(todo["repo"], reply.body) if reply.ok and isinstance(reply.body, dict) else None
        if row is None:
            retry(store, todo, f"status {reply.status}", now)
            return
        store.add_run(row, LIVE_SOURCE, None, iso(now))
        plan_run(store, row)
        finish(store, todo, "done", now)


def take_jobs(store, client, todo, now):
    path = f"{base(todo['repo'])}/actions/runs/{todo['run_id']}/attempts/{todo['attempt']}/jobs?per_page={PER_PAGE}"
    jobs, total = [], None
    for page in range(1, MAX_PAGES + 1):
        reply = client.rest(path if page == 1 else f"{path}&page={page}")
        if reply.status in GONE:
            with store.tx():
                finish(store, todo, "not_retained", now, ("jobs-not-retained", f"status {reply.status}"))
            return
        listed = reply.body.get("jobs") if reply.ok and isinstance(reply.body, dict) else None
        if not isinstance(listed, list):
            with store.tx():
                retry(store, todo, f"status {reply.status}", now)
            return
        total = reply.body.get("total_count")
        jobs += listed
        if len(listed) < PER_PAGE:
            break
    with store.tx():
        for job in jobs:
            row = normal_job(todo["repo"], job, iso(now)) if isinstance(job, dict) else None
            if row:
                insert(store, "ci_jobs", row)
        gap = ("jobs-truncated", f"{len(jobs)} of {total}") if type(total) is int and total > len(jobs) else None
        finish(store, todo, "done", now, gap)


def read_fetches(store, client, now):
    seen = set()
    while True:
        rows = [r for r in store.db.execute("SELECT * FROM ci_fetches WHERE state = 'todo' ORDER BY requested_at, run_id, attempt").fetchall()
                if (r["repo"], r["run_id"], r["attempt"], r["kind"]) not in seen]
        if not rows:
            return len(seen)
        for todo in rows:
            seen.add((todo["repo"], todo["run_id"], todo["attempt"], todo["kind"]))
            try:
                (take_attempt if todo["kind"] == "attempt" else take_jobs)(store, client, todo, now)
            except Unreadable as problem:
                with store.tx():
                    retry(store, todo, str(problem), now)


def run(store, client, now):
    read_feed(store, client, now)
    heads = read_heads(store, client, now, ledger_github.PASS_REQUESTS // 2)
    fetched = read_fetches(store, client, now)
    heads += read_heads(store, client, now, ledger_github.PASS_REQUESTS)
    return heads, fetched
