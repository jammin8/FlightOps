import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bottlenecks  # noqa: E402
from collect import FAILED, HISTORY_DAYS  # noqa: E402
from store import epoch, iso  # noqa: E402

SCHEMA = "flightops-status/2"
STALE_AFTER = 600
IDLE_FAILING = 6 * 3600
WAIT_SAMPLES = 5
STAGES = (("draft", "Draft"), ("fixing", "Fixing"), ("waiting_ci", "Waiting for CI"), ("in_ci", "In CI"),
          ("in_review", "In review"), ("approved", "Approved"), ("merged", "Merged today"))
ORDER = {key: index for index, (key, _) in enumerate(STAGES)}
QUEUED = ("QUEUED", "WAITING", "PENDING", "REQUESTED")


def latest(values):
    known = [v for v in values if epoch(v) is not None]
    return max(known, key=epoch) if known else None


def earliest(values):
    known = [v for v in values if epoch(v) is not None]
    return min(known, key=epoch) if known else None


def checks_of(node, approval_check):
    commits = (node.get("commits") or {}).get("nodes") or []
    commit = (commits[-1] or {}).get("commit") if commits else None
    rollup = (commit or {}).get("statusCheckRollup") or {}
    found = {"failed": [], "running": [], "queued": [], "passed": [], "approval": None, "head_at": (commit or {}).get("committedDate")}
    for item in (rollup.get("contexts") or {}).get("nodes") or []:
        if not isinstance(item, dict):
            continue
        if item.get("__typename") == "StatusContext":
            name, state, at = item.get("context"), item.get("state"), item.get("createdAt")
            if approval_check and name == approval_check:
                found["approval"] = at if state == "SUCCESS" else found["approval"]
                continue
            bucket = "failed" if state in FAILED else "running" if state in ("PENDING", "EXPECTED") else "passed" if state == "SUCCESS" else None
            if bucket:
                found[bucket].append(at)
            continue
        name, status, conclusion = item.get("name"), item.get("status"), item.get("conclusion")
        if approval_check and name == approval_check:
            found["approval"] = item.get("completedAt") if conclusion == "SUCCESS" else found["approval"]
            continue
        if status == "COMPLETED":
            if conclusion in FAILED:
                found["failed"].append(item.get("completedAt"))
            elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                found["passed"].append(item.get("completedAt"))
        elif status == "IN_PROGRESS":
            found["running"].append(item.get("startedAt"))
        elif status in QUEUED:
            found["queued"].append(((item.get("checkSuite") or {}).get("createdAt")) or item.get("startedAt"))
    return found


def reviewers(node):
    names = []
    for request in (node.get("reviewRequests") or {}).get("nodes") or []:
        who = (request or {}).get("requestedReviewer") or {}
        name = who.get("login") or who.get("slug")
        if name:
            names.append(name)
    return names


def classify(node, approval_check, viewer, now):
    checks = checks_of(node, approval_check)
    reviews = [r for r in (node.get("latestReviews") or {}).get("nodes") or [] if isinstance(r, dict)]
    approvals = [r.get("submittedAt") for r in reviews if r.get("state") == "APPROVED"]
    changes = [r.get("submittedAt") for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    decision = node.get("reviewDecision")
    marks = [(i.get("__typename"), i.get("createdAt")) for i in (node.get("timelineItems") or {}).get("nodes") or [] if isinstance(i, dict)]
    ready_at = latest([at for kind, at in marks if kind == "ReadyForReviewEvent"]) or node.get("createdAt")
    drafted_at = latest([at for kind, at in marks if kind == "ConvertToDraftEvent"]) or node.get("createdAt")
    asked = reviewers(node)
    approved = decision == "APPROVED" or checks["approval"] is not None or (decision is None and approvals and not changes)
    if node.get("isDraft"):
        stage, since, waiting = "draft", drafted_at, "the author"
    elif checks["failed"] or decision == "CHANGES_REQUESTED":
        stage, since = "fixing", latest(checks["failed"] + (changes if decision == "CHANGES_REQUESTED" else []))
        waiting = "a fix for failing CI" if checks["failed"] else "changes asked for in review"
    elif checks["running"]:
        stage, since, waiting = "in_ci", earliest(checks["running"]), "CI to finish"
    elif checks["queued"]:
        stage, since, waiting = "waiting_ci", earliest(checks["queued"]), "a CI runner"
    elif approved:
        stage, since, waiting = "approved", latest(approvals + [checks["approval"]]), "a merge"
    else:
        stage, since = "in_review", latest([ready_at] + checks["passed"])
        waiting = "review from " + ", ".join(asked[:3]) if asked else "a review"
    since = since or node.get("updatedAt")
    ci = "failing" if checks["failed"] else "running" if checks["running"] else "queued" if checks["queued"] else "passed" if checks["passed"] else "none"
    review = "approved" if approved else "changes_requested" if decision == "CHANGES_REQUESTED" else "requested" if asked else "none"
    needs = None
    if viewer and viewer in asked:
        needs = "Your review is requested"
    elif stage == "approved":
        needs = "Approved, not merged"
    elif stage == "fixing" and checks["failed"] and now - (epoch(since) or now) >= IDLE_FAILING and now - (epoch(checks["head_at"]) or now) >= IDLE_FAILING:
        needs = "Failing with no new push"
    return {"stage": stage, "stage_since": since, "waiting_on": waiting, "ci": ci, "review": review, "needs_you": needs}


def local_midnight(now):
    moment = datetime.datetime.fromtimestamp(now).astimezone()
    return int(moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def percentile(values, share):
    if len(values) < WAIT_SAMPLES:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)]


def days_back(now, count):
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    return [(today - datetime.timedelta(days=count - 1 - i)) for i in range(count)]


def trends(store, repos, now):
    days = days_back(now, HISTORY_DAYS)
    first = iso(datetime.datetime.combine(days[0], datetime.time(), datetime.timezone.utc).timestamp())
    marks = ",".join("?" * len(repos))
    prs = store.db.execute(f"SELECT created_at, merged_at, closed_at FROM prs WHERE repo IN ({marks})", repos).fetchall() if repos else []
    jobs = store.db.execute(f"""SELECT queued_at, started_at FROM ci_jobs WHERE repo IN ({marks}) AND queued_at >= ? AND started_at IS NOT NULL
                                AND conclusion IS NOT 'skipped'""", (*repos, first)).fetchall() if repos else []
    index = {d.isoformat(): i for i, d in enumerate(days)}
    opened, merged, waits = [0] * len(days), [0] * len(days), [[] for _ in days]
    for row in prs:
        for column, series in (("created_at", opened), ("merged_at", merged)):
            at = row[column]
            if at and at[:10] in index:
                series[index[at[:10]]] += 1
    for row in jobs:
        start, queued = epoch(row["started_at"]), epoch(row["queued_at"])
        if start is not None and queued is not None and row["queued_at"][:10] in index:
            waits[index[row["queued_at"][:10]]].append(max(0, start - queued) / 60)
    ends = [int(datetime.datetime.combine(d + datetime.timedelta(days=1), datetime.time(), datetime.timezone.utc).timestamp()) for d in days]
    spans = [(epoch(r["created_at"]), epoch(r["merged_at"] or r["closed_at"])) for r in prs]
    still_open = [sum(1 for made, ended in spans if made is not None and made < end and (ended is None or ended >= end)) for end in ends]
    p90 = [None if w is None else round(w, 1) for w in (percentile(x, 0.9) for x in waits)]
    return {"days": list(index), "opened": opened, "merged": merged, "open": still_open, "ci_wait_p90_min": p90, "ci_jobs": [len(x) for x in waits]}


def history_state(store, repos, now):
    floor = (datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    complete, oldest = True, None
    for repo in repos:
        inventory = store.cursor(f"inventory:{repo}", {}) or {}
        back = store.cursor(f"runs-back:{repo}", {}) or {}
        if not inventory.get("covered_to") or (back.get("next") or "9999") >= floor:
            complete = False
        pending = store.db.execute("SELECT count(*) FROM ci_run_attempts WHERE repo = ? AND status = 'completed' AND jobs_read = 0", (repo,)).fetchone()[0]
        complete = complete and pending == 0
        row = store.db.execute("SELECT min(created_at) FROM prs WHERE repo = ?", (repo,)).fetchone()[0]
        oldest = row if oldest is None or (row and row < oldest) else oldest
    return {"complete": complete and bool(repos), "oldest": oldest[:10] if oldest else None}


def repo_state(store, repo, now):
    reads = store.reads(repo)
    live = reads.get("live") or {}
    snap = store.db.execute("SELECT read_at, body FROM snapshots WHERE repo = ?", (repo,)).fetchone()
    body = json.loads(snap["body"]) if snap else None
    age = now - epoch(snap["read_at"]) if snap else None
    failed_after = epoch(live.get("failed_at")) and (not live.get("ok_at") or epoch(live["failed_at"]) > epoch(live["ok_at"]))
    state = "unknown" if body is None else "stale" if age > STALE_AFTER or failed_after else "ok"
    history = reads.get("history") or {}
    return {"body": body, "state": state, "read_at": snap["read_at"] if snap else None, "error": live.get("error") if failed_after or body is None else None,
            "history_ok": bool(history.get("ok_at"))}


def stage_table(prs):
    rows = []
    for key, label in STAGES:
        if key == "merged":
            continue
        inside = [p for p in prs if p["stage"] == key]
        oldest = min(inside, key=lambda p: epoch(p["stage_since"]) or 0) if inside else None
        rows.append({"id": key, "label": label, "count": len(inside), "oldest_since": oldest["stage_since"] if oldest else None,
                     "oldest_pr": oldest["id"] if oldest else None, "verdict": None})
    return rows


def status_line(open_count, merged, needs, partial, state):
    if state == "unknown":
        return "Can't read GitHub yet."
    parts = [f"{open_count} open"] + ([f"{merged} merged today"] if merged is not None else [])
    line = ", ".join(parts) + "."
    if needs:
        line += f" {needs} need{'s' if needs == 1 else ''} you."
    if state == "stale":
        line += " Data is out of date."
    elif partial:
        line += " Some repos are missing."
    return line


def view(store, repos, records, states, now, midnight):
    known = [r for r in repos if states[r]["state"] != "unknown"]
    prs = sorted([p for p in records if p["repo"] in known], key=lambda p: (ORDER[p["stage"]], epoch(p["stage_since"]) or 0))
    state = "unknown" if not known else "stale" if any(states[r]["state"] == "stale" for r in known) else "ok"
    partial = len(known) < len(repos)
    merged = None
    if all(states[r]["history_ok"] for r in repos) and repos:
        marks = ",".join("?" * len(repos))
        merged = store.db.execute(f"SELECT count(*) FROM prs WHERE repo IN ({marks}) AND merged_at >= ?", (*repos, iso(midnight))).fetchone()[0]
    needs = [p["id"] for p in prs if p["needs_you"]]
    stages = stage_table(prs) if known else [{"id": k, "label": l, "count": None, "oldest_since": None, "oldest_pr": None, "verdict": None} for k, l in STAGES[:-1]]
    stages.append({"id": "merged", "label": "Merged today", "count": merged, "oldest_since": None, "oldest_pr": None, "verdict": None})
    log = []
    if known:
        verdicts, log = bottlenecks.assess(store, known, now, prs)
        for stage in stages:
            stage["verdict"] = verdicts.get(stage["id"])
    open_count = len(prs) if known else None
    return {"state": state, "partial": partial, "status_line": status_line(open_count, merged, len(needs), partial, state),
            "counts": {"open": open_count, "merged_today": merged, "needs_you": len(needs) if known else None},
            "stages": stages, "bottlenecks": log[:20], "prs": [p["id"] for p in prs], "needs_you": needs,
            "trends": trends(store, repos, now), "history": history_state(store, repos, now)}


def build(store, config, now, config_state="ok"):
    repos = [r["name"] for r in config["repos"]]
    states = {r: repo_state(store, r, now) for r in repos}
    viewer = next((s["body"].get("viewer") for s in states.values() if s["body"] and s["body"].get("viewer")), None)
    records = []
    for repo in repos:
        body = states[repo]["body"]
        for node in (body or {}).get("prs") or []:
            if not isinstance(node, dict) or type(node.get("number")) is not int:
                continue
            record = {"id": f"{repo}#{node['number']}", "repo": repo, "number": node["number"], "title": node.get("title") or "",
                      "url": node.get("url"), "author": (node.get("author") or {}).get("login"), "draft": bool(node.get("isDraft")),
                      "created_at": node.get("createdAt"), "updated_at": node.get("updatedAt")}
            record.update(classify(node, config["approval_check"], viewer, now))
            records.append(record)
    midnight = local_midnight(now)
    views = {"all": view(store, repos, records, states, now, midnight)}
    for repo in repos:
        views[repo] = view(store, [repo], records, states, now, midnight)
    ok = [r for r in repos if states[r]["state"] == "ok"]
    errors = sorted({states[r]["error"] for r in repos if states[r]["error"]})
    github = {"state": "never" if not repos else "ok" if len(ok) == len(repos) else "error" if not any(states[r]["body"] for r in repos) else "partial",
              "read_at": max((states[r]["read_at"] for r in repos if states[r]["read_at"]), default=None), "error": errors[0] if errors else None}
    return {"schema": SCHEMA, "generated_at": iso(now), "viewer": viewer,
            "config": {"state": config_state, "repos": len(repos)},
            "repos": [{"id": r["name"], "name": r["name"].split("/", 1)[1], "color": r["color"], "state": states[r["name"]]["state"],
                       "read_at": states[r["name"]]["read_at"], "error": states[r["name"]]["error"],
                       "default_branch": (states[r["name"]]["body"] or {}).get("default_branch")} for r in config["repos"]],
            "sources": {"github": github, "agent_logs": {"state": "not_connected"}, "machines": {"state": "not_connected"}},
            "views": views, "prs": records, "usage": None, "machines": None}


def write(status, where):
    os.makedirs(os.path.dirname(where), exist_ok=True)
    temp = where + ".tmp"
    with open(temp, "w") as f:
        json.dump(status, f, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(temp, where)
