#!/usr/bin/env python3












import datetime
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import capture

CACHE_DIR = os.path.expanduser("~/Library/Caches/flightops")
CACHE = os.path.join(CACHE_DIR, "linear.json")
HISTORY = os.path.join(CACHE_DIR, "launch-history.json")
OVERRIDE = os.path.expanduser("~/.config/flightops/launch.json")
TTL = 1800
API = "https://api.linear.app/graphql"
STAGES = ["backlog", "todo", "in_progress", "in_review", "done"]
LAUNCH_INITIATIVE = settings.CONFIG["linear_initiative"].lower()
DEFAULT_TARGET = "2026-12-01"
HORIZON_DAYS = 730
MAX_HORIZON_DAYS = 3650

ISSUES = """query($after: String) { issues(first: 250, after: $after) {
  pageInfo { hasNextPage endCursor }
  nodes { identifier title url createdAt updatedAt completedAt dueDate priority state { type name } project { id }
          labels { nodes { name } } } } }"""
OWNER_LABEL = "owner action"
PAGE = "pageInfo { hasNextPage endCursor }"
PROJECTS = ["""query($after: String) { projects(first: 100, after: $after) { PAGE nodes { id name url targetDate status { name type } } } }""",
            """query($after: String) { projects(first: 100, after: $after) { PAGE nodes { id name url targetDate state } } }"""]
INITIATIVES = ["""query($after: String) { initiatives(first: 50, after: $after) { PAGE nodes { id name url targetDate status
                  projects(first: 100) { pageInfo { hasNextPage } nodes { id } } } } }""",
               """query($after: String) { initiatives(first: 50, after: $after) { PAGE nodes { id name targetDate status
                  projects(first: 100) { pageInfo { hasNextPage } nodes { id } } } } }"""]
PROJECTS = [q.replace("PAGE", PAGE) for q in PROJECTS]
INITIATIVES = [q.replace("PAGE", PAGE) for q in INITIATIVES]


class LinearError(Exception):
    pass


def key():
    r = subprocess.run(["security", "find-generic-password", "-s", settings.CONFIG["linear_keychain_service"], "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def gql(api_key, query, variables=None):
    req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 {"Content-Type": "application/json", "Authorization": api_key})
    with urllib.request.urlopen(req, timeout=25) as r:
        body = json.load(r)
    if body.get("errors"):
        raise LinearError(body["errors"][0].get("message", "query failed"))
    return body["data"]


def fetch_all(api_key, queries, field, what):



    err = None
    for q in queries:
        try:
            nodes, after, seen = [], None, set()
            while True:
                page = gql(api_key, q, {"after": after})[field]
                nodes += page["nodes"]
                if not page["pageInfo"]["hasNextPage"]:
                    return nodes
                cursor = page["pageInfo"].get("endCursor")
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise LinearError(f"{what} pagination has a missing or repeated cursor")
                seen.add(cursor)
                after = cursor
        except LinearError as e:
            err = e
    raise LinearError(f"{what} query failed: {err}")


def stage(state):
    t, name = state["type"], state["name"].lower()
    if t == "canceled" or "duplicate" in name:
        return None
    if t == "completed":
        return "done"
    if t == "started":
        return "in_review" if "review" in name else "in_progress"
    if t == "unstarted":
        return "todo"
    return "backlog"


def zero():
    return {s: 0 for s in STAGES}


def finish(c):
    scope = sum(c[s] for s in STAGES)
    return {**c, "scope": scope, "pct_done": round(100 * c["done"] / scope) if scope else 0}


def split_name(name):

    head, sep, rest = name.partition("·")
    head = head.strip()
    if sep and head.isdigit() and rest.strip():
        return int(head), rest.strip()
    return None, name.strip()


def canceled(p):
    return (p.get("status") or {}).get("type") == "canceled" or (p.get("state") or "") == "canceled"


def valid_date(s):
    try:
        datetime.date.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


def read_override(path=OVERRIDE):

    if not os.path.exists(path):
        return None, None
    try:
        with open(path) as f:
            o = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"launch.json unreadable: {e}"[:160]
    if not isinstance(o, dict):
        return None, "launch.json is not an object"
    ids = o.get("project_ids")
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        return None, "launch.json project_ids must be a list of project ids"
    if o.get("target") is not None and not valid_date(o.get("target")):
        return None, "launch.json target must be YYYY-MM-DD"
    return {"project_ids": ids, "target": o.get("target")}, None


def launch_cohort(projects, initiatives, override=None):




    byid = {p["id"]: p for p in projects}
    override = override or {}
    if override.get("project_ids"):
        ids = list(dict.fromkeys(override["project_ids"]))
        unknown = [i for i in ids if i not in byid]
        if unknown:
            return None, "launch.json names unknown projects: " + ", ".join(unknown)
        dead = [byid[i]["name"] for i in ids if canceled(byid[i])]
        if dead:
            return None, "launch.json names canceled projects: " + ", ".join(dead)
        source, name = "override", "Launch"
    else:
        ini = next((i for i in initiatives if split_name(i["name"])[1].lower() == LAUNCH_INITIATIVE), None) \
            or next((i for i in initiatives if LAUNCH_INITIATIVE in i["name"].lower()), None)
        if not ini:
            return None, f"No “{settings.CONFIG['linear_initiative']}” initiative found and no ~/.config/flightops/launch.json"
        nested = ini.get("projects") or {}
        if (nested.get("pageInfo") or {}).get("hasNextPage"):
            return None, "the launch initiative lists more projects than were fetched"
        listed = [n["id"] for n in nested.get("nodes", [])]
        if any(i not in byid for i in listed):
            return None, "the launch initiative names projects that weren't fetched"
        ids = [i for i in listed if not canceled(byid[i])]
        source, name = "initiative", split_name(ini["name"])[1]
    if not ids:
        return None, "the launch cohort has no projects"
    live = {i: byid[i] for i in ids}
    dates = sorted(live[i]["targetDate"] for i in ids if live[i].get("targetDate"))
    if override.get("target"):
        target, target_source = override["target"], "override"
    elif dates:
        target, target_source = dates[0], "projects"
    else:
        target, target_source = DEFAULT_TARGET, "default"
    version = hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:10]
    return {"name": name, "source": source, "project_ids": ids, "target": target, "target_source": target_source,
            "version": version}, None


def forecast(rows, target, now, days=28, runs=2000, horizon=HORIZON_DAYS, max_horizon=MAX_HORIZON_DAYS):









    tdays = (datetime.date.fromisoformat(target) - now.date()).days if target else None
    if tdays is not None and tdays > horizon:
        horizon = min(tdays, max_horizon)
    start = now - datetime.timedelta(days=days)
    comp = [0] * days
    undated = 0
    for _, done, completed in rows:
        if not done:
            continue
        if completed is None or completed > now:
            undated += 1
        elif completed >= start:
            comp[min(days - 1, (completed - start).days)] += 1
    done_n = sum(1 for _, d, _ in rows if d)
    remaining = len(rows) - done_n


    when = [(cr, (c if c is not None and c <= now else now) if d else None) for cr, d, c in rows]
    burnup = []
    for w in range(8, -1, -1):
        t = now - datetime.timedelta(weeks=w)
        burnup.append({"t": t.date().isoformat(), "done": sum(1 for _, c in when if c and c <= t),
                       "scope": sum(1 for cr, _ in when if not cr or cr <= t)})
    active = sum(1 for x in comp if x)
    out = {"scope": len(rows), "done": done_n, "remaining": remaining, "per_week": round(sum(comp) * 7 / days, 1),
           "sample_days": days, "active_days": active, "undated_done": undated, "target": target,
           "burnup": burnup, "burnup_reconstructed": True, "horizon_days": horizon,
           "insufficient": sum(comp) < 8 or active < 5}
    if out["insufficient"] or remaining == 0:
        return out
    rng = random.Random(now.date().toordinal())
    finished = []
    for _ in range(runs):
        r, d = remaining, 0
        while r > 0 and d < horizon:
            d += 1
            r -= rng.choice(comp)
        if r <= 0:
            finished.append(d)
    finished.sort()

    def percentile(q):
        k = math.ceil(q * runs)
        return (now + datetime.timedelta(days=finished[k - 1])).date().isoformat() if len(finished) >= k else None

    out["p50"], out["p85"] = percentile(0.5), percentile(0.85)
    out["runs"] = runs
    if tdays is not None:
        if tdays > horizon:
            out["prob_note"] = "target beyond the simulation horizon"
        else:
            out["prob_by_target"] = round(100 * sum(1 for f in finished if f <= tdays) / runs, 1)
    return out


def with_history(launch, history, today):


    fc = launch.get("forecast") or {}
    entry = {"date": today, "cohort": launch["version"], "done": fc.get("done"), "scope": fc.get("scope"), "p50": fc.get("p50")}
    hist = sorted([h for h in history if isinstance(h, dict) and h.get("date") and h.get("date") != today] + [entry],
                  key=lambda h: h["date"])[-180:]
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()
    past = [h for h in hist if h.get("cohort") == launch["version"] and h["date"] <= cutoff]
    if past:
        p = past[-1]
        drift = {"since": p["date"]}
        if isinstance(p.get("scope"), int) and isinstance(fc.get("scope"), int):
            drift["scope_change"] = fc["scope"] - p["scope"]
        if isinstance(p.get("done"), int) and isinstance(fc.get("done"), int):
            drift["done_change"] = fc["done"] - p["done"]
        if fc.get("p50") and p.get("p50"):
            drift["p50_days"] = (datetime.date.fromisoformat(fc["p50"]) - datetime.date.fromisoformat(p["p50"])).days
        launch["drift"] = drift
    return launch, hist


def unique_issues(issues):
    seen = {}
    for i in issues:
        if seen.setdefault(i["identifier"], i) != i:
            raise LinearError(f"conflicting copies of {i['identifier']}")
    return list(seen.values())


def summarize(issues, projects, initiatives, now=None, override=None, override_error=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    issues = unique_issues(issues)
    week = now - datetime.timedelta(days=7)
    parse = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    total, unassigned = zero(), zero()
    per_project = {p["id"]: zero() for p in projects}
    flight, recent = [], []
    index = {}
    done_7d = 0
    pname = {p["id"]: p["name"] for p in projects}
    for i in issues:
        s = stage(i["state"])
        pid = (i.get("project") or {}).get("id")
        if s is None:
            index[i["identifier"]] = [i["title"], "canceled", pname.get(pid), pid]
            continue
        total[s] += 1
        (per_project.setdefault(pid, zero()) if pid else unassigned)[s] += 1
        item = {"id": i["identifier"], "title": i["title"], "url": i["url"], "stage": s,
                "project": pname.get(pid), "updated": i["updatedAt"]}
        if s in ("in_progress", "in_review"):
            flight.append(item)
        index[i["identifier"]] = [i["title"], s, pname.get(pid), pid]
        if s == "done" and i.get("completedAt"):
            item["completed"] = i["completedAt"]
            recent.append(item)
            if week <= parse(i["completedAt"]) <= now:
                done_7d += 1
    flight.sort(key=lambda x: x["updated"], reverse=True)


    owner = [{"id": i["identifier"], "title": i["title"], "url": i["url"], "updated": i["updatedAt"],
              "created": i.get("createdAt"), "due": i.get("dueDate"), "stage": stage(i["state"]),
              "priority": int(i.get("priority") or 0), "project_id": (i.get("project") or {}).get("id")}
             for i in issues if stage(i["state"]) not in (None, "done")
             and any(l["name"].lower() == OWNER_LABEL for l in (i.get("labels") or {}).get("nodes", []))]
    owner.sort(key=lambda o: (o["due"] or "9999", o["priority"] or 9, o["updated"]))


    cohort, why = launch_cohort(projects, initiatives, override)
    if override_error:
        launch = {"error": override_error}
    elif cohort is None:
        launch = {"error": why}
    else:
        ids = set(cohort["project_ids"])
        counts, rows = zero(), []
        for i in issues:
            s = stage(i["state"])
            if s is None or (i.get("project") or {}).get("id") not in ids:
                continue
            counts[s] += 1
            created = parse(i["createdAt"]) if i.get("createdAt") else None
            completed = parse(i["completedAt"]) if s == "done" and i.get("completedAt") else None
            rows.append((created, s == "done", completed))
        if not rows:
            launch = {**cohort, "counts": finish(counts), "error": "the launch projects have no issues"}
        else:
            launch = {**cohort, "counts": finish(counts), "forecast": forecast(rows, cohort["target"], now)}
    recent.sort(key=lambda x: x["completed"], reverse=True)

    def pstatus(p):
        st = p.get("status") or {}
        return st.get("name") or (p.get("state") or "").title() or "Unknown"

    prows = []
    for p in projects:
        if canceled(p):
            continue
        c = finish(per_project.get(p["id"], zero()))
        prows.append({"id": p["id"], "name": p["name"], "url": p.get("url"), "target_date": p.get("targetDate"),
                      "status": pstatus(p), **c,
                      "in_flight": [f for f in flight if f["project"] == p["name"]][:4]})
    byid = {p["id"]: p for p in prows}

    warnings = [f"initiative {split_name(ini['name'])[1]} lists more projects than were fetched"
                for ini in initiatives if ((ini.get("projects") or {}).get("pageInfo") or {}).get("hasNextPage")]
    if len(owner) > 100:
        warnings.append(f"{len(owner) - 100} more owner actions than shown")
    irows, grouped = [], set()
    for ini in initiatives:
        ids = [n["id"] for n in (ini.get("projects") or {}).get("nodes", []) if n["id"] in byid]
        grouped.update(ids)
        c = zero()
        for pid in ids:
            for s in STAGES:
                c[s] += byid[pid][s]
        irows.append({"id": ini["id"], "name": ini["name"], "url": ini.get("url"), "target_date": ini.get("targetDate"),
                      "status": ini.get("status") or "", "project_ids": ids, **finish(c)})

    number = lambda r: split_name(r["name"])[0]
    irows.sort(key=lambda r: (number(r) if number(r) is not None else math.inf, r["name"].lower()))
    other = [p["id"] for p in prows if p["id"] not in grouped]

    return {**finish(total), "done_7d": done_7d, "unassigned": finish(unassigned),
            "initiatives": irows, "projects": prows, "other_project_ids": other,
            "in_flight": flight[:12], "in_flight_total": len(flight), "recent_done": recent[:6], "index": index,
            "owner_actions": owner[:100], "owner_actions_total": len(owner), "launch": launch, "warnings": warnings,
            "fetched": time.time()}


def fetch(api_key, override=None, override_error=None):
    issues = fetch_all(api_key, [ISSUES], "issues", "issues")
    projects = fetch_all(api_key, PROJECTS, "projects", "projects")
    initiatives = fetch_all(api_key, INITIATIVES, "initiatives", "initiatives")
    return summarize(issues, projects, initiatives, override=override, override_error=override_error)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f)
    os.replace(tmp, path)


def serve_stale(cached, reason):

    if isinstance(cached, dict) and not cached.get("error"):
        print(json.dumps({**cached, "stale_reason": reason[:160]}))
    else:
        print(json.dumps({"error": reason[:160]}))


def main():
    cached = load(CACHE)
    if cached is not None and capture.cache_current(CACHE, cached, TTL):
        print(json.dumps(cached))
        return
    api_key = key()
    if not api_key:
        if isinstance(cached, dict) and not cached.get("error"):
            serve_stale(cached, "Linear key unavailable (keychain locked?)")
        else:
            print(json.dumps({"error": "no_key"}))
        return
    try:
        override, override_error = read_override()
        out = fetch(api_key, override, override_error)
        launch = out.get("launch") or {}
        if launch.get("version"):
            today = datetime.datetime.now().date().isoformat()
            out["launch"], hist = with_history(launch, load(HISTORY) or [], today)
            save(HISTORY, hist)
    except Exception as e:
        serve_stale(cached, str(e) or type(e).__name__)
        return
    save(CACHE, out)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
