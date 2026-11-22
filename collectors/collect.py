import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from github import Stop, Unreadable  # noqa: E402
from store import epoch, iso  # noqa: E402

HISTORY_DAYS = 90
INVENTORY_PAGE = 50
INVENTORY_PAGES = 4
OVERLAP = 1800
RUN_DAYS_PER_PASS = 6
RECENT_RUN_PAGES = 3
JOB_READS_PER_PASS = 30
FAILED = ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR")
TIMELINE = ("READY_FOR_REVIEW_EVENT", "CONVERT_TO_DRAFT_EVENT", "REVIEW_REQUESTED_EVENT", "MERGED_EVENT", "CLOSED_EVENT", "REOPENED_EVENT")

CHECKS = """statusCheckRollup { state contexts(first: 60) { nodes { __typename
  ... on CheckRun { name status conclusion startedAt completedAt checkSuite { createdAt } }
  ... on StatusContext { context state createdAt } } } }"""

LIVE = """query($owner: String!, $name: String!) { rateLimit { remaining } viewer { login }
repository(owner: $owner, name: $name) { nameWithOwner isArchived defaultBranchRef { name }
pullRequests(states: OPEN, first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) { totalCount pageInfo { hasNextPage } nodes {
  number title url isDraft createdAt updatedAt headRefName baseRefName reviewDecision author { login }
  reviewRequests(first: 10) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } } } }
  latestReviews(first: 20) { nodes { state submittedAt author { login } } }
  timelineItems(last: 5, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT]) { nodes { __typename
    ... on ReadyForReviewEvent { createdAt } ... on ConvertToDraftEvent { createdAt } } }
  commits(last: 1) { nodes { commit { oid committedDate %s } } } } } } }""" % CHECKS

INVENTORY = """query($owner: String!, $name: String!, $first: Int!%s) { rateLimit { remaining }
repository(owner: $owner, name: $name) { pullRequests(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}%s) {
  pageInfo { hasNextPage endCursor } nodes {
  number title state isDraft createdAt updatedAt mergedAt closedAt headRefName headRefOid baseRefName author { login }
  reviews(first: 40) { nodes { state submittedAt author { login } } }
  timelineItems(first: 60, itemTypes: [%s]) { pageInfo { hasNextPage } nodes { __typename
    ... on ReadyForReviewEvent { createdAt } ... on ConvertToDraftEvent { createdAt } ... on ReviewRequestedEvent { createdAt }
    ... on MergedEvent { createdAt } ... on ClosedEvent { createdAt } ... on ReopenedEvent { createdAt } } } } } } }"""


def owner_name(repo):
    owner, _, name = repo.partition("/")
    return {"owner": owner, "name": name}


def read_live(store, client, repo, now):
    data, errors = client.graphql_query(LIVE, owner_name(repo))
    found = data.get("repository")
    if not isinstance(found, dict):
        raise Unreadable("repository not found or not visible to gh")
    prs = found.get("pullRequests") or {}
    if not isinstance(prs.get("nodes"), list):
        raise Unreadable("pull requests missing from the answer")
    body = {"viewer": (data.get("viewer") or {}).get("login"), "default_branch": (found.get("defaultBranchRef") or {}).get("name"),
            "archived": bool(found.get("isArchived")), "total": prs.get("totalCount"),
            "truncated": (prs.get("pageInfo") or {}).get("hasNextPage") is True or bool(errors), "prs": prs["nodes"]}
    with store.tx():
        store.db.execute("INSERT INTO snapshots(repo, read_at, body) VALUES (?, ?, ?) ON CONFLICT(repo) DO UPDATE SET read_at = excluded.read_at, body = excluded.body",
                         (repo, iso(now), json.dumps(body)))
        store.read_ok(repo, "live", now)
    return body


def take_pr(store, repo, node):
    number, created, updated = node.get("number"), node.get("createdAt"), node.get("updatedAt")
    if type(number) is not int or epoch(created) is None or epoch(updated) is None:
        return None
    timeline = node.get("timelineItems") or {}
    whole = 1 if (timeline.get("pageInfo") or {}).get("hasNextPage") is False else 0
    store.db.execute(
        """INSERT INTO prs(repo, number, state, is_draft, created_at, updated_at, merged_at, closed_at, head_ref, head_sha, base_ref, author, title, timeline_complete)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(repo, number) DO UPDATE SET state = excluded.state, is_draft = excluded.is_draft, updated_at = excluded.updated_at,
           merged_at = excluded.merged_at, closed_at = excluded.closed_at, head_ref = excluded.head_ref, head_sha = excluded.head_sha,
           base_ref = excluded.base_ref, author = excluded.author, title = excluded.title, timeline_complete = excluded.timeline_complete""",
        (repo, number, node.get("state") or "UNKNOWN", None if node.get("isDraft") is None else int(node["isDraft"]), created, updated,
         node.get("mergedAt"), node.get("closedAt"), node.get("headRefName"), node.get("headRefOid"), node.get("baseRefName"),
         (node.get("author") or {}).get("login"), node.get("title"), whole))
    for item in timeline.get("nodes") or []:
        if isinstance(item, dict) and item.get("createdAt"):
            store.db.execute("INSERT OR IGNORE INTO pr_timeline(repo, number, type, at) VALUES (?, ?, ?, ?)", (repo, number, item.get("__typename"), item["createdAt"]))
    for review in (node.get("reviews") or {}).get("nodes") or []:
        if isinstance(review, dict) and review.get("submittedAt"):
            store.db.execute("INSERT OR IGNORE INTO pr_timeline(repo, number, type, at, actor, detail) VALUES (?, ?, 'Review', ?, ?, ?)",
                             (repo, number, review["submittedAt"], (review.get("author") or {}).get("login") or "", review.get("state") or ""))
    return number


def read_inventory(store, client, repo, now):
    state = store.cursor(f"inventory:{repo}", {"covered_to": None, "walk": None})
    for _ in range(INVENTORY_PAGES):
        if state["walk"] is None:
            stop = epoch(state["covered_to"]) - OVERLAP if state["covered_to"] else now - HISTORY_DAYS * 86400
            state["walk"] = {"after": None, "stop_at": iso(stop), "top_at": iso(now)}
        walk = state["walk"]
        after = walk["after"]
        query = INVENTORY % (", $after: String" if after else "", ", after: $after" if after else "", ", ".join(TIMELINE))
        variables = dict(owner_name(repo), first=INVENTORY_PAGE, **({"after": after} if after else {}))
        data, _ = client.graphql_query(query, variables)
        connection = ((data.get("repository") or {}).get("pullRequests")) if isinstance(data.get("repository"), dict) else None
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise Unreadable("pull request history missing from the answer")
        oldest = None
        with store.tx():
            for node in connection["nodes"]:
                if isinstance(node, dict) and take_pr(store, repo, node) is not None:
                    updated = epoch(node["updatedAt"])
                    oldest = updated if oldest is None else min(oldest, updated)
            page = connection.get("pageInfo") or {}
            more, cursor = page.get("hasNextPage") is True, page.get("endCursor")
            if not more or (oldest is not None and oldest <= epoch(walk["stop_at"])) or not cursor or cursor == after:
                state = {"covered_to": walk["top_at"], "walk": None}
                store.set_cursor(f"inventory:{repo}", state)
                store.read_ok(repo, "history", now)
                return True
            walk["after"] = cursor
            store.set_cursor(f"inventory:{repo}", state)
    return False


def take_run(store, repo, run):
    run_id, attempt = run.get("id"), run.get("run_attempt")
    if type(run_id) is not int or type(attempt) is not int:
        return
    prs = sorted({p.get("number") for p in run.get("pull_requests") or [] if isinstance(p, dict) and type(p.get("number")) is int})
    store.db.execute(
        """INSERT INTO ci_run_attempts(repo, run_id, run_attempt, workflow_name, event, status, conclusion, tested_sha, head_branch, pr_numbers,
           requested_at, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(repo, run_id, run_attempt) DO UPDATE SET status = excluded.status, conclusion = excluded.conclusion,
           started_at = excluded.started_at, updated_at = excluded.updated_at, pr_numbers = excluded.pr_numbers""",
        (repo, run_id, attempt, run.get("name"), run.get("event"), run.get("status"), run.get("conclusion"), run.get("head_sha"), run.get("head_branch"),
         json.dumps(prs), run.get("created_at"), run.get("run_started_at"), run.get("updated_at")))


def runs_page(store, client, repo, created, page, use_etag):
    path = f"repos/{repo}/actions/runs?per_page=100&created={created}" + (f"&page={page}" if page > 1 else "")
    reply = client.rest(path, store.etag(path) if use_etag else None)
    if reply.not_modified:
        return None
    if reply.status == 404:
        return []
    if not reply.ok or not isinstance(reply.body, dict) or not isinstance(reply.body.get("workflow_runs"), list):
        raise Unreadable(f"CI runs unreadable (status {reply.status})")
    runs = reply.body["workflow_runs"]
    with store.tx():
        for run in runs:
            if isinstance(run, dict):
                take_run(store, repo, run)
        if use_etag:
            store.set_etag(path, reply.etag if len(runs) < 100 else None)
    return runs


def day(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%d")


def read_runs(store, client, repo, now):
    for page in range(1, RECENT_RUN_PAGES + 1):
        runs = runs_page(store, client, repo, "%3E%3D" + day(now - 86400), page, page == 1)
        if runs is None or len(runs) < 100:
            break
    back = store.cursor(f"runs-back:{repo}", {"next": day(now - 2 * 86400)})
    floor = day(now - HISTORY_DAYS * 86400)
    for _ in range(RUN_DAYS_PER_PASS):
        if back["next"] is None or back["next"] < floor:
            break
        page = 1
        while True:
            runs = runs_page(store, client, repo, back["next"], page, False)
            if runs is None or len(runs) < 100 or page >= 10:
                break
            page += 1
        previous = datetime.date.fromisoformat(back["next"]) - datetime.timedelta(days=1)
        back = {"next": previous.isoformat()}
        with store.tx():
            store.set_cursor(f"runs-back:{repo}", back)
    with store.tx():
        store.read_ok(repo, "ci", now)


def take_job(store, repo, job):
    job_id, attempt, run_id = job.get("id"), job.get("run_attempt"), job.get("run_id")
    if type(job_id) is not int or type(attempt) is not int or type(run_id) is not int:
        return
    store.db.execute(
        """INSERT INTO ci_jobs(repo, run_id, run_attempt, job_id, name, workflow_name, tested_sha, head_branch, status, conclusion,
           queued_at, started_at, completed_at, runner_name, labels) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(repo, job_id, run_attempt) DO UPDATE SET status = excluded.status, conclusion = excluded.conclusion,
           started_at = excluded.started_at, completed_at = excluded.completed_at, runner_name = excluded.runner_name""",
        (repo, run_id, attempt, job_id, job.get("name"), job.get("workflow_name"), job.get("head_sha"), job.get("head_branch"), job.get("status"),
         job.get("conclusion"), job.get("created_at"), job.get("started_at"), job.get("completed_at"), job.get("runner_name"), json.dumps(job.get("labels") or [])))


def read_jobs(store, client, repos, limit=JOB_READS_PER_PASS):
    marks = ",".join("?" * len(repos))
    todo = store.db.execute(f"""SELECT repo, run_id, run_attempt FROM ci_run_attempts WHERE status = 'completed' AND jobs_read = 0 AND repo IN ({marks})
                               ORDER BY requested_at DESC LIMIT ?""", (*repos, limit)).fetchall()
    for row in todo:
        reply = client.rest(f"repos/{row['repo']}/actions/runs/{row['run_id']}/attempts/{row['run_attempt']}/jobs?per_page=100")
        if reply.ok and isinstance(reply.body, dict) and isinstance(reply.body.get("jobs"), list):
            with store.tx():
                for job in reply.body["jobs"]:
                    if isinstance(job, dict):
                        take_job(store, row["repo"], job)
                store.db.execute("UPDATE ci_run_attempts SET jobs_read = 1 WHERE repo = ? AND run_id = ? AND run_attempt = ?", (row["repo"], row["run_id"], row["run_attempt"]))
        elif reply.status in (404, 410):
            with store.tx():
                store.db.execute("UPDATE ci_run_attempts SET jobs_read = -1 WHERE repo = ? AND run_id = ? AND run_attempt = ?", (row["repo"], row["run_id"], row["run_attempt"]))


def run_pass(store, client, repos, now):
    stopped, errors = None, []
    try:
        client.start()
    except Unreadable as problem:
        with store.tx():
            for repo in repos:
                store.read_failed(repo, "live", problem, now)
        return {"stopped": "github", "errors": [str(problem)]}
    steps = [(repo, "live", read_live) for repo in repos] + [(repo, "history", read_inventory) for repo in repos] + [(repo, "ci", read_runs) for repo in repos]
    for repo, part, step in steps:
        if stopped:
            break
        try:
            step(store, client, repo, now)
        except Stop as reason:
            stopped = str(reason)
        except Unreadable as problem:
            errors.append(f"{repo} {part}: {problem}")
            with store.tx():
                store.read_failed(repo, part, problem, now)
    if not stopped:
        try:
            read_jobs(store, client, repos)
        except Stop as reason:
            stopped = str(reason)
        except Unreadable as problem:
            errors.append(f"jobs: {problem}")
    return {"stopped": stopped, "errors": errors}
