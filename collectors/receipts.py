#!/usr/bin/env python3



















































import datetime
import fnmatch
import hashlib
import json
import math
import re
from collections import defaultdict

SCHEMA = "pr-facts/2"
DERIVER = 3
GATES = {"review-gate", "evaluate"}
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
RUNNING = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED"}
MODES = ("first_attempt", "first_completed", "latest")
PR_EVENTS = {"pull_request", "pull_request_target"}
TRAILER = re.compile(r"(?im)^co-authored-by:\s*(.+?)\s*<")
SYNC = re.compile(r"^Merge\b.*\b(main|master)\b", re.I)
SIZE = [(50, "XS"), (200, "S"), (800, "M"), (2000, "L"), (math.inf, "XL")]
EARLY = 60
FAMILIES = (("opus", "claude-opus"), ("sonnet", "claude-sonnet"), ("fable", "claude-fable"), ("haiku", "claude-haiku"),
            ("claude", "claude"), ("codex", "codex"), ("copilot", "copilot"), ("cursor", "cursor"), ("devin", "devin"),
            ("jules", "jules"), ("grok", "grok"), ("gemini", "gemini"))

SAID = {"MISSING": "missing", "STATUS": "commit status history not kept", "UNORDERED": "attempt order unknown",
        "UNRESOLVED": "required app not identified"}


def ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except (ValueError, AttributeError):
        return None


def complete_connection(conn):

    return isinstance(conn, dict) and isinstance(conn.get("pageInfo"), dict) and conn["pageInfo"].get("hasNextPage") is False \
        and isinstance(conn.get("nodes"), list)




def required_checks(repo_rules, branch="main"):



    if not isinstance(repo_rules, dict):
        return None
    protection, rulesets = repo_rules.get("branchProtectionRules"), repo_rules.get("rulesets")
    if not complete_connection(protection) or not complete_connection(rulesets):
        return None
    out = set()
    for r in protection.get("nodes") or []:
        if fnmatch.fnmatchcase(branch, r.get("pattern") or "") and r.get("requiresStatusChecks"):
            checks = r.get("requiredStatusChecks")
            if checks is not None:
                out |= {(((c.get("app") or {}).get("slug")), c.get("context"), None) for c in checks}
            else:
                out |= {(None, c, None) for c in r.get("requiredStatusCheckContexts") or []}
    for rs in rulesets.get("nodes") or []:
        if rs.get("enforcement") != "ACTIVE" or rs.get("target") != "BRANCH":
            continue
        names = ((rs.get("conditions") or {}).get("refName") or {})
        def hit(pats):
            return any(p in ("~ALL", "~DEFAULT_BRANCH") or fnmatch.fnmatchcase("refs/heads/" + branch, p) for p in pats or [])
        if not hit(names.get("include")) or hit(names.get("exclude")):
            continue
        if not complete_connection(rs.get("rules")):
            return None
        for rule in rs["rules"].get("nodes") or []:
            for c in ((rule.get("parameters") or {}).get("requiredStatusChecks") or []):
                out.add((None, c.get("context"), c.get("integrationId")))
    return sorted(out, key=lambda x: (x[1], x[0] or "", x[2] or 0))




def attempts_hydrated(commit):



    out, why = defaultdict(list), []
    if commit.get("_error"):
        why.append("GitHub error: " + str(commit["_error"])[:80])
    suites = commit.get("checkSuites")
    if not complete_connection(suites):
        why.append("check suites truncated" if isinstance(suites, dict) and (suites.get("pageInfo") or {}).get("hasNextPage")
                   else "check suites not returned")
    for s in (suites or {}).get("nodes") or []:
        runs = s.get("checkRuns")
        if not complete_connection(runs):
            why.append("check runs truncated" if isinstance(runs, dict) and (runs.get("pageInfo") or {}).get("hasNextPage")
                       else "check runs not returned")
        wf = ((s.get("workflowRun") or {}).get("workflow") or {}).get("name") or ""
        app = (s.get("app") or {}).get("slug") or ""
        app_id = (s.get("app") or {}).get("databaseId")
        for c in (runs or {}).get("nodes") or []:
            state = c.get("conclusion") if c.get("status") == "COMPLETED" else (c.get("status") or "PENDING")
            out[(app, c.get("name"))].append({"order": c.get("databaseId"), "start": c.get("startedAt"), "end": c.get("completedAt"),
                                              "state": state or "PENDING", "required": c.get("isRequired"), "workflow": wf,
                                              "app_id": app_id})
    status = commit.get("status")
    for c in ((status or {}).get("contexts") or []):
        creator = c.get("creator") or {}
        out[("status", c.get("context"))].append({"order": None, "start": c.get("createdAt"), "end": c.get("createdAt"),
                                                  "state": c.get("state") or "PENDING", "required": c.get("isRequired"),
                                                  "status": True, "creator": (creator.get("__typename"), creator.get("login"))
                                                  if creator else None})
    unordered = set()
    for k, v in out.items():
        if len(v) > 1 and any(a["order"] is None for a in v):
            unordered.add(k)
        v.sort(key=lambda a: (a["order"] is None, a["order"] or 0))
    return {"att": dict(out), "collected": not why, "why": sorted(set(why)), "unordered": unordered}


def attempts_rollup(rollup):


    out = defaultdict(list)
    for c in ((rollup or {}).get("contexts") or {}).get("nodes", []) or []:
        if c.get("__typename") == "CheckRun":
            state = c.get("conclusion") if c.get("status") == "COMPLETED" else (c.get("status") or "PENDING")
            key = (((c.get("checkSuite") or {}).get("app") or {}).get("slug") or "", c.get("name"))
            out[key].append({"order": c.get("databaseId"), "start": c.get("startedAt"), "end": c.get("completedAt"),
                             "state": state or "PENDING", "required": c.get("isRequired"), "workflow": ""})
        else:
            out[("status", c.get("context"))].append({"order": None, "start": c.get("createdAt"), "end": c.get("createdAt"),
                                                      "state": c.get("state") or "PENDING", "required": c.get("isRequired"),
                                                      "status": True})
    for v in out.values():
        v.sort(key=lambda a: (a["order"] is None, a["order"] or 0))
    return {"att": dict(out), "collected": False, "why": ["latest runs only (no full history)" if out else "history not read yet"],
            "unordered": set()}


def policy_of(rules, all_attempts):

    if rules is not None:
        req = {tuple(r) for r in rules if r[1] not in GATES}
        return ("required", req, "rules") if req else ("tracked", None, "rules")
    seen = {(k[0], k[1], None) for att in all_attempts for k, a in att.items()
            if k[0] != "status" and any(x["required"] for x in a) and k[1] not in GATES}
    seen |= {(None, k[1], None) for att in all_attempts for k, a in att.items()
             if k[0] == "status" and any(x["required"] for x in a) and k[1] not in GATES}
    return ("required", seen, "observed") if seen else ("tracked", None, "observed")


def obligations(att, key):





    slug, name, app_id = key
    runs, unresolved = [], False
    ids = {k_app: a["app_id"] for (k_app, _), v in att.items() if k_app != "status" for a in v if a.get("app_id") is not None}
    for (k_app, k_name), v in att.items():
        if k_app == "status" or k_name != name:
            continue
        for a in v:
            if app_id is not None:
                if a.get("app_id") is None:
                    unresolved = True
                elif a["app_id"] == app_id:
                    runs.append(a)
            elif slug is None or k_app == slug:
                runs.append(a)
    runs.sort(key=lambda a: (a["order"] is None, a["order"] or 0))
    out = []
    if runs or unresolved:
        out.append({"kind": "run", "attempts": runs, "unresolved": unresolved})
    statuses = att.get(("status", name)) or []
    if slug is None and app_id is None:
        if statuses:
            out.append({"kind": "status", "attempts": statuses, "unresolved": False})
        return out


    seen = [(a, status_source(a, slug, app_id, ids)) for a in statuses]
    eligible = [a for a, ok in seen if ok]
    if eligible or any(ok is None for _, ok in seen):
        out.append({"kind": "status", "attempts": eligible, "unresolved": any(ok is None for _, ok in seen)})
    return out


def status_source(a, slug, app_id, ids):



    if a.get("required") is not None:
        return bool(a["required"])
    kind, login = a.get("creator") or (None, None)
    if kind is None:
        return None
    if kind != "Bot" or not (login or "").endswith("[bot]"):
        return False
    bot = login[:-len("[bot]")]
    if slug is not None:
        return bot == slug
    return (ids[bot] == app_id) if bot in ids else None


def head_ci(h, policy):


    kind, req, source = policy
    att = h["att"]
    if not h["collected"]:
        why = "; ".join(h["why"])
        return {m: {"outcome": "unknown", "reason": why, "failed": []} for m in MODES}, False
    if kind == "required":
        keys = sorted(req, key=lambda k: (k[1], k[0] or "", k[2] or 0))
        jobs = {k: obligations(att, k) for k in keys}
    else:
        keys = sorted(k for k in att if k[1] not in GATES)
        jobs = {k: [{"kind": "status" if k[0] == "status" else "run", "attempts": att[k], "unresolved": False}] for k in keys}
    if not keys:
        return {m: {"outcome": "unknown", "reason": "no checks", "failed": []} for m in MODES}, True

    def ordered(a):
        return len(a) <= 1 or all(x["order"] is not None for x in a)

    def pick(ob, mode):
        a = ob["attempts"]
        if ob["unresolved"]:
            return "UNRESOLVED"
        if not a:
            return "MISSING"
        if ob["kind"] == "status":
            return a[-1]["state"] if mode == "latest" else "STATUS"
        if not ordered(a):
            return "UNORDERED"
        if mode == "first_attempt":
            return a[0]["state"]
        if mode == "latest":
            return a[-1]["state"]
        done = [x["state"] for x in a if x["state"] not in RUNNING and x["state"] != "CANCELLED"]
        return done[0] if done else ("PENDING" if any(x["state"] in RUNNING for x in a) else "CANCELLED")

    out = {}
    for mode in MODES:
        states = [(k[1], pick(ob, mode) if ob is not None else "MISSING")
                  for k, obs in jobs.items() for ob in (obs or [None])]
        bad = sorted({name for name, st in states if st in FAILED})
        if bad:
            out[mode] = {"outcome": "fail", "reason": None, "failed": bad}
        elif all(st == "SUCCESS" for _, st in states):
            out[mode] = ({"outcome": "pass", "reason": None, "failed": []} if source == "rules"
                         else {"outcome": "unknown", "reason": "required checks not evidenced", "failed": []})
        else:
            other = sorted({SAID.get(st, st.lower()) for _, st in states if st != "SUCCESS"})
            out[mode] = {"outcome": "unknown", "reason": ", ".join(other), "failed": []}
    complete = all(obs and all(ob["kind"] == "run" and not ob["unresolved"] and ordered(ob["attempts"]) for ob in obs)
                   for obs in jobs.values())
    return out, complete




def timeline_complete(p):
    tl = p.get("timelineItems")
    return complete_connection(tl) and not any("timelineItems" in g for g in p.get("gaps") or [])


def anchor_of(p):



    if not timeline_complete(p):
        return None, "incomplete"
    marks = [(ts(e.get("createdAt")) or 0, e["__typename"]) for e in (p.get("timelineItems") or {}).get("nodes") or []
             if e.get("__typename") in ("ReadyForReviewEvent", "ConvertToDraftEvent")]
    marks.sort()
    if marks and marks[0][1] == "ReadyForReviewEvent":
        return marks[0][0], "ready"
    if not marks and p.get("isDraft"):
        return None, None
    return ts(p.get("createdAt")), "opened"


def commits_of(p):

    raw = [(n["commit"], False) for n in (p.get("commits") or {}).get("nodes") or []]
    for e in (p.get("timelineItems") or {}).get("nodes") or []:
        b = e.get("beforeCommit") if e.get("__typename") == "HeadRefForcePushedEvent" else None
        if b and all(c["oid"] != b["oid"] for c, _ in raw):
            raw.append((b, True))
    return raw


def receipt_sha(s):
    return isinstance(s, str) and re.fullmatch(r"[0-9a-f]{40}", s) is not None and s != "0" * 40


def receipt_time(t):
    return isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)


def receipt_end(p):

    end = (ts(p.get("mergedAt")) or ts(p.get("closedAt"))) if p.get("state") != "OPEN" else None
    return end if end is not None else (p.get("fetched_at") if receipt_time(p.get("fetched_at")) else ts(p.get("updatedAt")))


def opening_receipts(p, receipts):


    created, end = ts(p.get("createdAt")), receipt_end(p)
    return [o for o in ((receipts or {}).get("opened") or {}).get(p.get("number"), [])
            if created is not None and end is not None and receipt_time(o.get("t"))
            and created <= o["t"] <= end and receipt_sha(o.get("sha"))]


def open_intervals(p):





    tl = p.get("timelineItems") or {}
    created = ts(p.get("createdAt"))
    snapshot = p.get("fetched_at") if receipt_time(p.get("fetched_at")) else ts(p.get("updatedAt"))
    if not timeline_complete(p) or not isinstance(tl.get("nodes"), list) or created is None or snapshot is None or snapshot < created:
        return []
    state, start, previous, last_close, last_merge = "OPEN", created, created, None, None
    intervals = []
    for e in tl["nodes"]:
        if not isinstance(e, dict) or not e.get("__typename"):
            return []
        kind = e["__typename"]
        if kind not in ("ClosedEvent", "MergedEvent", "ReopenedEvent"):
            continue
        at = ts(e.get("createdAt"))
        if at is None or not previous < at <= snapshot:
            return []
        previous = at
        if kind == "ReopenedEvent":
            if state != "CLOSED":
                return []
            state, start = "OPEN", at
        else:
            if state != "OPEN":
                return []
            intervals.append((start, at, start == created, False))
            state, last_close = ("MERGED" if kind == "MergedEvent" else "CLOSED"), at
            if kind == "MergedEvent":
                last_merge = at
    if state != p.get("state"):
        return []
    closed, merged = ts(p.get("closedAt")), ts(p.get("mergedAt"))
    if (p.get("closedAt") is not None and (closed is None or closed != last_close)) \
            or (p.get("mergedAt") is not None and (merged is None or merged != last_merge)):
        return []
    if (state == "CLOSED" and closed != last_close) or (state == "MERGED" and merged != last_merge):
        return []
    if state == "OPEN":
        intervals.append((start, snapshot, start == created, True))
    return intervals


def head_ref_pushes(p, receipts):



    branch = p.get("headRefName")
    if not branch or p.get("isCrossRepository") is True:
        return []
    created = ts(p.get("createdAt"))
    end = receipt_end(p)
    known = {c["oid"] for c, _ in commits_of(p)} | {o["sha"] for o in opening_receipts(p, receipts)}
    intervals = open_intervals(p) if p.get("isCrossRepository") is False else []
    return sorted((x for x in (receipts or {}).get("pushes", [])
                   if x.get("ref") == "refs/heads/" + branch and receipt_sha(x.get("head"))
                   and receipt_time(x.get("t")) and created is not None and end is not None and x["t"] <= end
                   and (x["head"] in known or any((a < x["t"] or (left and a == x["t"]))
                                                 and (x["t"] < b or (right and x["t"] == b))
                                                 for a, b, left, right in intervals))),
                  key=lambda x: x["t"])


def heads_of(p, hydrated, receipts=None):

    raw = commits_of(p)
    last = (((p.get("commits") or {}).get("nodes") or [{}])[-1].get("commit") or {}).get("oid")
    pushes = head_ref_pushes(p, receipts)
    named = {x["head"] for x in pushes} | {o["sha"] for o in opening_receipts(p, receipts)}


    present = {c["oid"] for c, _ in raw}
    for sha in sorted(named - present):
        raw.append(({"oid": sha, **{k: v for k, v in (hydrated.get(sha) or {}).items()
                                    if k in ("committedDate", "messageHeadline", "messageBody", "parents")}}, False))
    pushed_at = {}
    for x in pushes:
        pushed_at.setdefault(x["head"], x["t"])
    heads = []
    for c, replaced in raw:
        h = hydrated.get(c["oid"])
        rolled = ((c.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes")
        ran = c.get("statusCheckRollup") or (h and ((h.get("checkSuites") or {}).get("nodes")))
        if not (replaced or c["oid"] == last or ran or c["oid"] in named):
            continue
        if h:
            got = attempts_hydrated(h)
            suites = (h.get("checkSuites") or {}).get("nodes") or []
            seen = [ts(s.get("createdAt")) for s in suites if s.get("createdAt")]
            runs = [{"t": ts(s["workflowRun"].get("createdAt")), "id": s["workflowRun"].get("databaseId"),
                     "event": s["workflowRun"].get("event"), "workflow": (s["workflowRun"].get("workflow") or {}).get("name") or "",
                     "branch": (s.get("branch") or {}).get("name")}
                    for s in suites if (s.get("workflowRun") or {}).get("event") in PR_EVENTS]
        else:
            got = attempts_rollup(c.get("statusCheckRollup"))
            seen = [ts(((x.get("checkSuite") or {}).get("createdAt")) or x.get("createdAt") or x.get("startedAt"))
                    for x in rolled or []]
            runs = []
        seen = [t for t in seen if t]
        trailers = TRAILER.findall(c.get("messageBody") or "")
        observed, via = (pushed_at[c["oid"]], "push event") if c["oid"] in pushed_at else \
            ((min(seen), "check suite") if seen else (None, "none"))
        heads.append({"sha": c["oid"], "observed": observed, "observed_via": via, "replaced": replaced,
                      "committed": ts(c.get("committedDate")),
                      "sync": ((c["parents"]["totalCount"] > 1 and bool(SYNC.match(c["messageHeadline"])))
                               if (c.get("parents") or {}).get("totalCount") is not None and c.get("messageHeadline") is not None else None),
                      "trailers": trailers, "family": family_of(trailers), **got, "hydrated": bool(h),
                      "read": (h or {}).get("_read"), "refresh_failed": (h or {}).get("_refresh_error"),
                      "runs": sorted((r for r in runs if r["t"]), key=lambda r: (r["t"], r["id"] or 0))})
    heads.sort(key=lambda h: (h["observed"] or h["committed"] or 0, h["sha"]))
    return heads


def family_of(trailers):

    fams = set()
    for t in trailers:
        n = t.lower()
        fams.add(next((v for k, v in FAMILIES if k in n), "other"))
    return None if not fams else fams.pop() if len(fams) == 1 else "mixed"


def covered(coverage, start, end):

    return any(a <= start and end <= b for a, b in coverage or [])


def r0_of(p, heads, receipts):

    t_anchor, kind = anchor_of(p)
    if t_anchor is None:
        return None
    by_sha = {h["sha"]: h for h in heads}
    rec = receipts or {}
    if kind == "opened":
        for o in opening_receipts(p, rec):
            if o["sha"] in by_sha:
                return {"sha": o["sha"], "status": "verified", "evidence": "opened event", "event": None, "workflow": None,
                        "delay_s": None, "receipt": o.get("id")}
    before = [x for x in head_ref_pushes(p, rec) if x["t"] <= t_anchor]
    if before:
        last = before[-1]
        if last["head"] in by_sha and covered(rec.get("coverage"), last["t"], t_anchor):
            return {"sha": last["head"], "status": "supported", "evidence": "push events", "event": None, "workflow": None,
                    "delay_s": int(t_anchor - last["t"]), "receipt": last.get("id")}
    branch = p.get("headRefName")
    cands = [(r["t"], r["id"] or 0, h, r) for h in heads for r in h["runs"] if r["t"] >= t_anchor - EARLY and branch and r["branch"] == branch]
    if not cands:
        return None
    t, _, h, r = min(cands, key=lambda x: (x[0], x[1]))
    return {"sha": h["sha"], "status": "candidate", "evidence": f"{r['event']} \"{r['workflow']}\" run on its branch", "event": r["event"],
            "workflow": r["workflow"], "delay_s": int(t - t_anchor), "receipt": r["id"]}




def digest(*parts):
    h = hashlib.sha256()
    for x in parts:
        h.update(json.dumps(x, sort_keys=True, separators=(",", ":")).encode())
    return h.hexdigest()[:16]


def pr_receipts(p, receipts):



    rec = receipts or {}
    opened = opening_receipts(p, rec)
    anchor, _ = anchor_of(p)
    cov = [[a, min(b, anchor)] for a, b in rec.get("coverage") or [] if anchor is not None and a <= anchor]
    return {"opened": {p.get("number"): opened} if opened else {}, "pushes": head_ref_pushes(p, rec), "coverage": cov}


def pr_record(key, p, hydrated, receipts=None, rules=None):


    rec = pr_receipts(p, receipts)
    heads = heads_of(p, hydrated, rec)
    policy = policy_of(rules, [h["att"] for h in heads])
    for h in heads:
        h["ci"], h["complete"] = head_ci(h, policy)
        h["complete"] = h["complete"] and h["collected"] and not h["refresh_failed"]
    r0 = r0_of(p, heads, rec)
    work = [h for h in heads if h["sync"] is False]
    first_decided = next((h for h in work if h["ci"]["first_completed"]["outcome"] in ("pass", "fail")), None)
    anchor, kind = anchor_of(p)
    size = (p.get("additions") or 0) + (p.get("deletions") or 0)
    by_sha = {h["sha"]: h for h in heads}
    r0_head = by_sha.get(r0["sha"]) if r0 else None
    incomplete = sorted(set(p.get("gaps") or []) | ({"readiness history not fully read"} if kind == "incomplete" else set())
                        | ({"required checks not evidenced"} if policy[2] != "rules" else set())
                        | ({f"{sum(1 for h in heads if not h['complete'])} heads without complete history"}
                           if any(not h["complete"] for h in heads) else set())
                        | ({"history to read again"} if p.get("reread_heads") else set())
                        | ({f"{sum(1 for h in heads if h['refresh_failed'])} heads whose last read failed (older history shown)"}
                           if any(h["refresh_failed"] for h in heads) else set()))
    reads = [h["read"] for h in heads if h["read"]]
    return {
        "pr": key, "number": p.get("number"), "state": p.get("state"), "draft": bool(p.get("isDraft")),
        "head_ref": p.get("headRefName"), "base_ref": p.get("baseRefName"),
        "opened": ts(p.get("createdAt")), "anchor": anchor, "anchor_kind": kind,
        "merged": ts(p.get("mergedAt")), "closed": ts(p.get("closedAt")),
        "size": {"lines": size, "files": p.get("changedFiles"), "class": next(c for lim, c in SIZE if size <= lim)},
        "policy": policy[0], "policy_source": policy[2],
        "required": sorted(f"{a or '*'}/{n}" + (f" (app {i})" if i is not None else "") for a, n, i in (policy[1] or [])),
        "heads": [{"sha": h["sha"], "observed": h["observed"], "observed_via": h["observed_via"], "sync": h["sync"],
                   "replaced": h["replaced"], "complete": h["complete"], "why": h["why"], "read": h["read"],
                   "refresh_failed": h["refresh_failed"], "family": h["family"],
                   "trailers": sorted(set(h["trailers"])), **{m: h["ci"][m] for m in MODES}} for h in heads],
        "r0": None if not r0 else {**r0, "first_attempt": r0_head["ci"]["first_attempt"],
                                   "first_completed": r0_head["ci"]["first_completed"], "family": r0_head["family"],
                                   "observed_via": r0_head["observed_via"], "sync": r0_head["sync"]},
        "first_decided": None if not first_decided else {"sha": first_decided["sha"],
                                                         "outcome": first_decided["ci"]["first_completed"]["outcome"],
                                                         "failed": first_decided["ci"]["first_completed"]["failed"],
                                                         "family": first_decided["family"]},
        "history_complete": sum(1 for h in heads if h["complete"]),
        "history_read": [min(reads), max(reads)] if reads else None,
        "incomplete": incomplete,
        "gaps": sorted(set(p.get("gaps") or [])),
        "settled": bool(p.get("settled")), "fetched": p.get("fetched_at"),
        "inputs": digest(p, {sha: hydrated[sha] for sha in sorted(hydrated) if sha in by_sha}, rec, rules),
    }


def derive(facts, receipts, now, rules=None, sources=None):



    prs = {}
    for key in sorted(facts):
        p, hydrated = facts[key]
        repo = key.split("#")[0]
        prs[key] = pr_record(key, p, hydrated, receipts.get(repo), (rules or {}).get(repo))
    return {"schema": SCHEMA, "deriver": DERIVER, "generated": int(now), "sources": sources or {}, "prs": prs}




STATUSES = ("verified", "supported", "candidate")


def summary(prs, since, now):









    cohort = [r for r in prs.values() if r.get("anchor") is not None and since <= r["anchor"] <= now]
    out = {"prs": len(cohort), "r0": {s: 0 for s in STATUSES + ("unknown",)},
           "readiness_unknown": sum(1 for r in prs.values() if r.get("anchor_kind") == "incomplete"
                                    and r.get("opened") is not None and since <= r["opened"] <= now),
           "first_try": {s: {"pass": 0, "fail": 0, "unknown": 0} for s in STATUSES}, "unknown_reasons": {},
           "heads": 0, "heads_complete": 0, "trailer_labels": {}}
    for r in cohort:
        out["heads"] += len(r["heads"])
        out["heads_complete"] += sum(1 for h in r["heads"] if h["complete"])
        r0 = r.get("r0")
        if not r0:
            out["r0"]["unknown"] += 1
            continue
        st, outcome = r0["status"], r0["first_attempt"]["outcome"]
        out["r0"][st] += 1
        out["first_try"][st][outcome] += 1
        if st == "verified" and outcome == "unknown":
            why = r0["first_attempt"]["reason"] or "unknown"
            out["unknown_reasons"][why] = out["unknown_reasons"].get(why, 0) + 1
        label = r0.get("family") or "none"
        out["trailer_labels"][label] = out["trailer_labels"].get(label, 0) + 1
    return out
