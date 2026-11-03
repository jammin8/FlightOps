#!/usr/bin/env python3









import datetime
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import capture

GH = next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")

def cache_path(environ=os.environ):
    data = environ.get("FLIGHTOPS_DATA_DIR")
    return os.path.join(data, "cache", "repos.json") if data else os.path.expanduser("~/Library/Caches/flightops/repos.json")


CACHE = cache_path()
CONFIG = os.path.expanduser("~/.config/flightops/repos.json")
TTL = 900
REPOS = settings.REPOS
CONTEXTS = """contexts(first: 60) { pageInfo { hasNextPage } nodes { __typename
  ... on CheckRun { databaseId name status conclusion startedAt completedAt detailsUrl }
  ... on StatusContext { context state createdAt } } }"""
FRAGMENT = """fragment R on Repository {
  open: pullRequests(states: OPEN, first: 100) { totalCount nodes { number title headRefName headRefOid isDraft createdAt updatedAt baseRefName mergeable MERGE_STATE
    commits(last: 1) { nodes { commit { statusCheckRollup { state CONTEXTS } } } } } }
  merged: pullRequests(states: MERGED, first: 60, orderBy: {field: UPDATED_AT, direction: DESC}) { pageInfo { hasNextPage } nodes { number title headRefName headRefOid mergedAt createdAt updatedAt } }
  defaultBranchRef { target { ... on Commit { statusCheckRollup { state CONTEXTS } } } }
}"""

QUEUED = ("QUEUED", "PENDING", "WAITING", "REQUESTED", "EXPECTED")
MAC_LANES = ("ios",)
FULL_TIER = re.compile(r"integration|verify", re.I)
RUNNING = ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED")
FAILED = ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED")
IGNORED = ("SKIPPED", "NEUTRAL")
GATES = ("review-gate", "evaluate")
BLOCKERS = {"DIRTY": "conflicts", "BEHIND": "behind main", "BLOCKED": "blocked by a required check or review",
            "UNSTABLE": "optional checks not passing", "DRAFT": "draft", "CLEAN": "mergeable", "HAS_HOOKS": "mergeable"}
READY_STATES = ("CLEAN", "HAS_HOOKS")


def latest_attempts(roll):




    runs = {}
    for c in ((roll or {}).get("contexts") or {}).get("nodes", []) or []:
        if c.get("__typename") == "CheckRun":

            status = c.get("status")
            state = (c.get("conclusion") or "PENDING") if status == "COMPLETED" else (status if status in RUNNING else "PENDING")
            name, started, done = c.get("name"), c.get("startedAt"), c.get("completedAt")
        else:
            name, state, started = c.get("context"), c.get("state") or "PENDING", c.get("createdAt")
            done = started if state not in RUNNING else None
        if name:
            runs.setdefault((name, c.get("__typename") == "CheckRun"), []).append(
                {"state": state, "started": started, "done": done, "id": c.get("databaseId")})
    out = {}
    for (name, _), attempts in runs.items():
        real = [a for a in attempts if a["state"] not in ("CANCELLED",) + IGNORED] or attempts

        latest = max(real, key=lambda a: (a["started"] or ("9999" if a["state"] in RUNNING else ""), a["id"] or 0, severity(a["state"])))
        got = {"state": latest["state"], "done": latest["done"], "started": latest["started"], "id": latest.get("id")}

        if name not in out or severity(got["state"]) > severity(out[name]["state"]):
            out[name] = got
    return out


def severity(state):
    return 3 if state in FAILED else 2 if state in RUNNING else 1 if state not in ("SUCCESS",) + IGNORED else 0


def check_runs(roll):
    rows = []
    for c in ((roll or {}).get("contexts") or {}).get("nodes", []) or []:
        if c.get("__typename") == "CheckRun":
            rows.append({"kind": "check_run", "name": c.get("name"), "id": c.get("databaseId"), "status": c.get("status"), "conclusion": c.get("conclusion"),
                         "started": c.get("startedAt"), "done": c.get("completedAt")})
        else:
            rows.append({"kind": "status", "name": c.get("context"), "id": None, "status": c.get("state"), "conclusion": None,
                         "started": c.get("createdAt"), "done": None})
    return [r for r in rows if isinstance(r["name"], str)]


def complete(roll):

    conn = (roll or {}).get("contexts")
    return isinstance(conn, dict) and isinstance(conn.get("pageInfo"), dict) and conn["pageInfo"].get("hasNextPage") is False \
        and isinstance(conn.get("nodes"), list)


def ci_of(checks, rollup_state=None, whole=True):




    ci = {k: v["state"] for k, v in checks.items() if k not in GATES and v["state"] not in IGNORED}
    failed = sum(1 for v in ci.values() if v in FAILED)
    running = any(v in RUNNING for v in ci.values())
    if failed:
        return "failing", failed, running
    if not whole and rollup_state in FAILED:
        return "failing", 1, running
    if running or (not whole and rollup_state in RUNNING):
        return "running", 0, True
    if any(v == "CANCELLED" for v in ci.values()):
        return "cancelled", 0, False
    return ("passed" if ci and whole and all(v == "SUCCESS" for v in ci.values()) else "none"), 0, False


def flaky_checks(roll):

    runs = {}
    for c in ((roll or {}).get("contexts") or {}).get("nodes", []) or []:
        if c.get("__typename") != "CheckRun" or c.get("status") != "COMPLETED":
            continue
        runs.setdefault(c.get("name"), []).append((c.get("startedAt") or "", c.get("databaseId") or 0, c.get("conclusion") or ""))
    out = []
    for name, attempts in runs.items():
        seq = [state for _, _, state in sorted(attempts)]
        if name and name not in GATES and any(s in FAILED for s in seq) and \
                any(s == "SUCCESS" for s in seq[max(i for i, s in enumerate(seq) if s in FAILED):]):
            out.append(name)
    return sorted(out)


def queued_jobs(checks):
    q = [{"job": n, "queued_at": a.get("started"), "id": a.get("id")} for n, a in checks.items()
         if n not in GATES and a["state"] in QUEUED]
    return sorted(q, key=lambda j: (j["queued_at"] or "9999", j["id"] or 0, j["job"]))


def queue_place(key, checks):
    q = queued_jobs(checks)
    if not q:
        return None, None
    at = min((j["queued_at"] for j in q if j["queued_at"]), default=None)
    if key not in MAC_LANES:
        return None, at
    return (1 if all(FULL_TIER.search(j["job"]) for j in q) else 0), at


def ci_waiting(checks):

    ci = [v["state"] for k, v in checks.items() if k not in GATES and v["state"] in RUNNING]
    return bool(ci) and "IN_PROGRESS" not in ci


def review_of(checks):
    gate = (checks.get("review-gate") or {}).get("state")
    return "approved" if gate == "SUCCESS" else "pending" if gate in ("FAILURE", "ERROR") + RUNNING else "none"


def ready_since(checks):

    done = [v["done"] for v in checks.values() if v["done"] and v["state"] not in IGNORED]
    return max(done) if done else None


def failing_since(checks):

    done = [v["done"] for k, v in checks.items() if k not in GATES and v["state"] in FAILED and v["done"]]
    return min(done) if done else None


def running_runs(roll):


    ids = set()
    for c in ((roll or {}).get("contexts") or {}).get("nodes", []) or []:
        if c.get("__typename") == "CheckRun" and c.get("status") != "COMPLETED":
            m = re.search(r"/actions/runs/(\d+)", c.get("detailsUrl") or "")
            if m:
                ids.add(int(m.group(1)))
    return sorted(ids)


def issue_id(pr):

    key = settings.CONFIG["linear_team_key"]
    m = re.search(r"\b" + re.escape(key) + r"-(\d+)", pr.get("title") or "", re.I) or re.search(re.escape(key) + r"-?(\d+)", pr.get("headRefName") or "", re.I)
    return f"{key.upper()}-{m.group(1)}" if m else None


def pr_row(key, n):
    roll = ((n["commits"]["nodes"] or [{}])[0].get("commit") or {}).get("statusCheckRollup") or {}
    checks = latest_attempts(roll)
    ci, failed, running = ci_of(checks, roll.get("state"), complete(roll))
    review = review_of(checks)
    state = n.get("mergeStateStatus")
    approved = ci == "passed" and review == "approved"

    if not approved:
        ready = False
    elif state is None or state == "UNKNOWN":
        ready = None
    else:
        ready = state in READY_STATES
    return {"repo": key, "number": n["number"], "title": n["title"], "issue": issue_id(n), "head": n.get("headRefOid"),
            "branch": n.get("headRefName"),
            "ci": ci, "failed_checks": failed, "checks_running": running, "ci_waiting": running and ci_waiting(checks),
            "check_runs": check_runs(roll), "check_runs_complete": complete(roll),
            "flaky": flaky_checks(roll), "runs": running_runs(roll),
            "queue_tier": queue_place(key, checks)[0], "queued_at": queue_place(key, checks)[1],
            "queued_jobs": queued_jobs(checks),
            "review": review,
            "updated": n["updatedAt"], "created": n["createdAt"], "mergeable": n.get("mergeable"), "merge_state": state,
            "blocker": BLOCKERS.get(state) if state else None, "merge_ready": ready,
            "ready_since": ready_since(checks) if ready else None,
            "failing_since": failing_since(checks) if failed else None}


def main_without_checks(path=CONFIG):



    try:
        with open(path) as f:
            names = json.load(f).get("main_without_checks", [])
        return {n for n in names if isinstance(n, str)}
    except (OSError, ValueError, AttributeError):
        return set()


def summarize(data, now, merge_state_known=True, unchecked_main=frozenset()):
    out = {"repos": [], "recent": [], "merged": [], "prs": [], "fetched": time.time(), "merge_state_known": merge_state_known}
    for key in REPOS:
        d = data[key]
        nodes = d["open"]["nodes"]
        to_main = [n for n in nodes if n.get("baseRefName") == "main"]
        prs = [pr_row(key, n) for n in to_main if not n["isDraft"]]
        out["prs"] += prs
        merged = sorted((m for m in d["merged"]["nodes"] if m["mergedAt"]), key=lambda m: m["mergedAt"], reverse=True)


        cutoff = (now - datetime.timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        more = (d["merged"].get("pageInfo") or {}).get("hasNextPage")
        merged_whole = more is False or (more is True and all((m.get("updatedAt") or "9999") < cutoff for m in d["merged"]["nodes"][-1:]))
        day = [m for m in merged if now - datetime.datetime.fromisoformat(m["mergedAt"].replace("Z", "+00:00")) < datetime.timedelta(hours=24)]
        main_roll = ((d["defaultBranchRef"] or {}).get("target") or {}).get("statusCheckRollup") or {}
        main_checks = latest_attempts(main_roll)
        main_ci, main_failed, main_running = ci_of(main_checks, main_roll.get("state"), complete(main_roll))
        main = {"running": "pending", "failing": "failure", "cancelled": "cancelled", "passed": "success",
                "none": "not_applicable" if key in unchecked_main else "unknown"}[main_ci]

        out["repos"].append({"name": key, "open": len(prs), "drafts": sum(1 for n in to_main if n["isDraft"]),
                             "open_total": d["open"]["totalCount"], "truncated": d["open"]["totalCount"] > len(nodes),
                             "merged_24h": len(day) if merged_whole else None, "main": main, "main_failed_checks": main_failed,
                             "main_running": main_running, "main_queue": queued_jobs(main_checks), "merged_today": [m["number"] for m in day],
                             "merged_complete": not (d["merged"].get("pageInfo") or {}).get("hasNextPage")})
        out["merged"] += [{"repo": key, "number": m["number"], "head": m.get("headRefOid"), "merged_at": m["mergedAt"], "created": m.get("createdAt")}
                          for m in merged]
        out["recent"] += [{"repo": key, "number": m["number"], "title": m["title"], "merged_at": m["mergedAt"], "issue": issue_id(m), "branch": m.get("headRefName"),
                           "created": m.get("createdAt")}
                          for m in merged[:10]]
    out["recent"] = sorted(out["recent"], key=lambda m: m["merged_at"], reverse=True)[:10]
    return out


def query(merge_state=True):
    frag = FRAGMENT.replace("CONTEXTS", CONTEXTS).replace("MERGE_STATE", "mergeStateStatus" if merge_state else "")
    q = "query {" + " ".join(f'{k}: repository(owner: "{settings.OWNER}", name: "{v}") {{ ...R }}' for k, v in REPOS.items()) + "}\n" + frag
    r = subprocess.run([GH, "api", "graphql", "-f", "query=" + q], capture_output=True, text=True, timeout=30)
    try:
        body = json.loads(r.stdout or "{}")
    except ValueError:
        body = {}
    if body.get("errors") or not body.get("data") or any(body["data"].get(k) is None for k in REPOS):
        msg = ((body.get("errors") or [{}])[0].get("message") or r.stderr.strip() or "GitHub query failed")[:160]
        raise RuntimeError(msg)
    return body["data"]


def fetch():
    known = True
    try:
        data = query(merge_state=True)
    except RuntimeError:
        data, known = query(merge_state=False), False
    return summarize(data, datetime.datetime.now(datetime.timezone.utc), merge_state_known=known,
                     unchecked_main=main_without_checks())


def gh_budget():
    d = json.loads(subprocess.run([GH, "api", "rate_limit"], capture_output=True, text=True, timeout=20, check=True).stdout)["resources"]
    return {"core": d["core"]["remaining"], "core_limit": d["core"]["limit"], "core_reset": d["core"]["reset"],
            "graphql": d["graphql"]["remaining"]}


def with_budget(snap, read=gh_budget):
    if not isinstance(snap, dict):
        return snap
    try:
        return dict(snap, github_budget={"value": read(), "fetched": time.time()})
    except Exception as e:
        return dict(snap, github_budget={"value": None, "fetched": None, "error": str(e)[:120] or type(e).__name__})


def main():
    try:
        if os.path.exists(CACHE):
            with open(CACHE) as f:
                cached = json.load(f)
            if capture.cache_current(CACHE, cached, TTL):
                print(json.dumps(with_budget(cached)))
                return
    except (OSError, ValueError):
        pass
    try:
        s = json.dumps(fetch())
    except Exception as e:

        try:
            with open(CACHE) as f:
                old = json.load(f)
            old["stale_reason"] = str(e)[:160]
            print(json.dumps(with_budget(old)))
        except (OSError, ValueError):
            print(json.dumps({"error": str(e)[:160]}))
        return
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        f.write(s)
    print(json.dumps(with_budget(json.loads(s))))


if __name__ == "__main__":
    main()
