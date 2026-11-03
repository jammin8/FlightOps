#!/usr/bin/env python3






















import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghevents
import ghfacts
import prfacts
import receipts

REFRESH_MAX = 10


def diff(a, b, path=""):

    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [path or "."]


def short(sha):
    return (sha or "")[:7]


def line(r):

    r0 = r.get("r0")
    if r0:
        fa = r0["first_attempt"]
        why = f" ({fa['reason']})" if fa["outcome"] == "unknown" else f" ({', '.join(fa['failed'])})" if fa["failed"] else ""
        delay = f", {r0['delay_s']} s" if r0.get("delay_s") is not None else ""
        r0s = f"first-ready commit {short(r0['sha'])} {r0['status']} ({r0['evidence']}{delay}) · first try {fa['outcome']}{why}"
    else:
        r0s = "first-ready commit unknown" + (" (readiness history incomplete)" if r.get("anchor_kind") == "incomplete" else "")
    fd = r.get("first_decided")
    fds = f" · first decided work head {short(fd['sha'])} {fd['outcome']}" if fd and (not r0 or fd["sha"] != r0["sha"]) else ""
    return f"{r['pr']}: {r0s}{fds} · heads {len(r['heads'])}, complete history {r['history_complete']}"


def check(keys, published, facts, recs, rules, expired_rules=None):

    out = []
    for k in keys:
        pub = (published.get("prs") or {}).get(k)
        if k not in facts:
            out.append((k, "incomplete", "no stored inputs" + ("" if pub is None else " (published, but its inputs are gone)"), None))
            continue
        p, hydrated = facts[k]
        repo = k.split("#")[0]
        r = receipts.pr_record(k, p, hydrated, recs.get(repo), rules.get(repo))
        if (expired_rules or {}).get(repo) is not None:
            ghfacts.retain_expired_failures(r, p, hydrated, recs.get(repo), expired_rules[repo])
        if pub is None:
            out.append((k, "incomplete", "not published yet", r))
        elif pub.get("inputs") != r["inputs"]:
            out.append((k, "incomplete", "published from earlier inputs; the next refresh republishes it", r))
        elif diff(pub, r):
            out.append((k, "contradiction", "differs at " + ", ".join(diff(pub, r)[:6]), r))
        elif r["incomplete"]:
            out.append((k, "incomplete", "reproduced; inputs not complete: " + "; ".join(r["incomplete"]), r))
        else:
            out.append((k, "verified", "reproduced from complete inputs", r))
    return out


def backlog(published):

    f = (published.get("sources") or {}).get("facts") or {}
    why = []
    if not f.get("discovery_complete"):
        why.append("discovery didn't read every page")
    if f.get("unfetched"):
        why.append(f"{len(f['unfetched'])} PRs discovered but not fetched yet")
    if f.get("stale"):
        why.append(f"{len(f['stale'])} PRs changed since they were fetched")
    if f.get("retry"):
        why.append(f"{len(f['retry'])} PRs to retry after an error")
    if f.get("due_heads"):
        why.append(f"{f['due_heads']} head histories due to be read again")
    if f.get("due_heads") is None and "due_heads" in f:
        why.append("whether head histories are due isn't known")
    errors = ((published.get("sources") or {}).get("run") or {}).get("errors") or []
    if errors:
        why.append("the last run had errors: " + "; ".join(errors[:3]))
    return why


def main(argv=None, published_path=prfacts.OUT, events_dir=ghevents.DIR, store=None, now=None, stdout=sys.stdout):
    args = list(sys.argv[1:] if argv is None else argv)
    now = now or time.time()
    days = 7
    if "--days" in args:
        i = args.index("--days")
        days = float(args[i + 1])
        del args[i:i + 2]
    as_json, refresh = "--json" in args, "--refresh" in args
    keys = [a for a in args if not a.startswith("--")]
    store = store or ghfacts.Store()
    try:
        with open(published_path) as f:
            published = json.load(f)
        problem = None if published.get("schema") == receipts.SCHEMA else f"schema {published.get('schema')!r}, expected {receipts.SCHEMA}"
    except (OSError, ValueError) as e:
        published, problem = {"prs": {}}, f"no readable publication ({type(e).__name__})"
    fetched = []
    if refresh:
        if not keys or len(keys) > REFRESH_MAX:
            stdout.write(f"--refresh needs 1 to {REFRESH_MAX} PRs\n")
            return 2
        try:
            fetched = ghfacts.refetch(keys, now, store)
        except ghfacts.Busy as busy:
            stdout.write(f"{busy}\n")
            return 2
    facts, recs, rules = ghfacts.load(store, archived=keys), ghevents.load_receipts(events_dir, archived=bool(keys)), ghfacts.load_rules(store, now)
    whole = not keys
    keys = keys or sorted(set(published.get("prs") or {}) | set(facts))
    results = [] if problem else check(keys, published, facts, recs, rules, ghfacts.load_expired_rules(store, now))
    behind = backlog(published) if whole and not problem else []
    derived = {k: r for k, _, _, r in results if r}
    window = receipts.summary(derived, now - days * 86400, now)
    scope = {"publication": published_path, "generated": published.get("generated"), "facts": store.d, "events": events_dir,
             "published_records": len(published.get("prs") or {}), "checked": len(results), "whole_publication": whole}
    if problem:
        verdict = "incomplete"
    elif not results:
        empty = whole and (published.get("sources") or {}).get("facts", {}).get("discovery_complete") \
            and (published.get("sources") or {}).get("facts", {}).get("discovered") == 0
        verdict, problem = ("verified", "discovery found no PRs") if empty else ("incomplete", "nothing to verify")
    else:
        verdicts = {v for _, v, _, _ in results}
        verdict = "contradiction" if "contradiction" in verdicts else "incomplete" if "incomplete" in verdicts or behind else "verified"
    if as_json:
        json.dump({"scope": scope, "verdict": verdict, "problem": problem, "backlog": behind,
                   "results": [{"pr": k, "verdict": v, "detail": d} for k, v, d, _ in results],
                   "summary": window, "refetched": fetched}, stdout)
        stdout.write("\n")
    else:
        stdout.write(f"Checked {scope['checked']} of {scope['published_records']} published records in {published_path} "
                     f"(generated {scope['generated']}), facts in {store.d}, events in {events_dir}\n")
        if problem:
            stdout.write(f"{verdict}: {problem}\n")
        for k, v, d, r in results:
            stdout.write(f"{v:13} {line(r) if r else k} — {d}\n")
        for b in behind:
            stdout.write(f"incomplete    the publication isn't the whole window: {b}\n")
        stdout.write(f"\nPRs first ready in the last {days:g} days: {json.dumps(window, sort_keys=True)}\n")
        stdout.write(f"Result: {verdict}\n")
    return {"verified": 0, "contradiction": 1}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
