#!/usr/bin/env python3





















import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghevents
import ghfacts
import receipts

OUT = os.path.expanduser("~/Library/Application Support/FlightOps/pr-facts.json")


def write(path, v):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(v, f, separators=(",", ":"))
    os.replace(tmp, path)


def facts_sources(budget, store):

    if budget:
        return {k: budget.get(k) for k in ("discovered", "discovery_complete", "discovered_at", "waiting", "waiting_keys",
                                            "unfetched", "stale", "retry", "points_last_hour", "cap", "held", "errors",
                                            "last_clean_read", "due_heads", "due_prs", "rules_read")}
    st = store.state()
    return {"discovered": len(st.get("prs") or []), "discovery_complete": False, "discovered_at": st.get("discovered"),
            "waiting": None, "waiting_keys": [], "unfetched": [], "stale": [], "retry": st.get("retry") or {},
            "points_last_hour": None, "cap": ghfacts.CAP, "held": None, "errors": ["this run's fetch failed"],
            "last_clean_read": st.get("last_clean_read"), "due_heads": None, "due_prs": [], "rules_read": {}}


def run(now=None, events_dir=ghevents.DIR, store=None, out=OUT, poll=ghevents.poll, refresh=ghfacts.refresh):
    now = now or time.time()
    errors = []
    try:
        events = poll(now, events_dir)
    except Exception as e:
        events, errors = {}, [f"events: {e}"[:160]]
        try:
            events = ghevents.summary(ghevents.load_state(events_dir), now)
        except Exception:
            pass
    store = store or ghfacts.Store()
    try:
        budget = refresh(now, store)
    except Exception as e:
        budget, errors = {}, errors + [f"facts: {e}"[:160]]
    try:
        with ghfacts.transaction(store):
            ghfacts.retire_settled(store, now)
            ghevents.archive_receipts(events_dir, now, ghfacts.receipt_holds(store))
    except Exception as e:
        errors.append(f"retention: {e}"[:160])
    sources = {"facts": facts_sources(budget, store),
               "events": {"repos": events.get("repos", {}), "counted_last_hour": events.get("counted_last_hour"), "cap": events.get("cap")},
               "run": {"at": int(now), "errors": errors + list(budget.get("errors") or [])}}
    facts, recs = ghfacts.load(store), ghevents.load_receipts(events_dir)
    data = receipts.derive(facts, recs, now, ghfacts.load_rules(store, now), sources)
    expired_rules = ghfacts.load_expired_rules(store, now)
    for key, record in data["prs"].items():
        repo = key.split("#")[0]

        expired = expired_rules.get(repo)
        if expired is not None:
            p, hydrated = facts[key]
            ghfacts.retain_expired_failures(record, p, hydrated, recs.get(repo), expired)
    write(out, data)
    return {"schema": "prfacts-run/2", "at": int(now), "prs": len(data["prs"]), "waiting": sources["facts"]["waiting"],
            "events": events, "facts": {k: v for k, v in budget.items() if k not in ("waiting_keys", "unfetched", "stale", "retry")},
            "errors": sources["run"]["errors"]}


if __name__ == "__main__":
    print(json.dumps(run()))
