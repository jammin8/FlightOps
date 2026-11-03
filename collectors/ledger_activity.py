import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger_github import OWNER, REPOS, Stop, Unreadable
from ledger_store import ZERO, epoch, iso

PAGES_PER_REPO = 6
PER_PAGE = 100
OVERLAP = 1800
SHA = re.compile(r"[0-9a-f]{40}")
LOST_CURSOR = (400, 404, 410, 422)
SOURCE = "activity"
START_GAPS = ("chain-break", "incarnation-start-unseen")


def blank():
    return {"covered_from": None, "covered_to": None, "feed_start": False, "walk": None}


def state_of(store, repo):
    return store.cursor(f"activity:{repo}", blank())


def page_path(repo, after=None):
    tail = f"&after={after}" if after else ""
    return f"repos/{OWNER}/{REPOS[repo]}/activity?per_page={PER_PAGE}&direction=desc{tail}"


def clean(row):
    if not isinstance(row, dict):
        return None
    event, ref, before, after, at, kind = (row.get(k) for k in ("id", "ref", "before", "after", "timestamp", "activity_type"))
    if type(event) is not int or not isinstance(ref, str) or not isinstance(kind, str) or epoch(at) is None:
        return None
    if not (isinstance(before, str) and SHA.fullmatch(before) and isinstance(after, str) and SHA.fullmatch(after)):
        return None
    actor = row.get("actor") if isinstance(row.get("actor"), dict) else {}
    return (event, kind, ref, before, after, at, actor.get("login"), actor.get("id") if type(actor.get("id")) is int else None)


def take_page(store, repo, rows):
    kept, bad, touched, added = [], 0, set(), 0
    for row in rows:
        fields = clean(row)
        if fields is None:
            bad += 1
            continue
        kept.append(fields)
        added_now = store.db.execute(
            "INSERT OR IGNORE INTO activity_events(repo, event_id, type, ref, before_sha, after_sha, at, actor, actor_id, recorded_pass) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (repo, *fields, store.pass_id)).rowcount
        if added_now:
            added += 1
            touched.add(fields[2])
    return kept, bad, touched, added


def read_repo(store, client, repo, now):
    state = state_of(store, repo)
    name = f"activity:{repo}"
    stats = {"pages": 0, "read": 0, "new": 0, "newest": None, "oldest": None, "not_modified": 0}
    touched, topped = set(), False
    try:
        while stats["pages"] < PAGES_PER_REPO:
            walk = state["walk"]
            if walk is None and topped:
                break
            top = walk is None
            path = page_path(repo, None if top else walk["after"])
            reply = client.rest(path, store.etag(path) if top and state["covered_to"] else None)
            stats["pages"] += 1
            if reply.not_modified:
                stats["not_modified"] = 1
                with store.tx():
                    state["covered_to"] = iso(now)
                    store.set_cursor(name, state)
                    store.close_gap(SOURCE, repo, "read", "read-failed", now)
                break
            if walk and walk["after"] and reply.status in LOST_CURSOR:
                with store.tx():
                    walk["after"] = None
                    store.set_cursor(name, state)
                    store.open_gap(SOURCE, repo, "walk", "cursor-lost", f"status {reply.status}; walk restarted from the top", now)
                continue
            if not reply.ok or not isinstance(reply.body, list):
                with store.tx():
                    store.open_gap(SOURCE, repo, "read", "read-failed", f"status {reply.status}", now)
                break
            with store.tx():
                kept, bad, fresh, added = take_page(store, repo, reply.body)
                touched |= fresh
                stats["read"] += len(kept)
                stats["new"] += added
                times = [f[5] for f in kept]
                stats["newest"] = max([stats["newest"] or ""] + times) or None
                oldest = min(times, key=epoch) if times else None
                stats["oldest"] = min((t for t in (stats["oldest"], oldest) if t), key=epoch, default=None)
                if top:
                    topped = True
                    stop = epoch(state["covered_to"]) - OVERLAP if state["covered_to"] else None
                    walk = state["walk"] = {"after": None, "stop_at": iso(stop) if stop else None, "top_at": iso(now)}
                    store.set_etag(path, reply.etag)
                if bad:
                    store.open_gap(SOURCE, repo, f"page:{walk['after'] or 'top'}", "row-malformed", f"{bad} rows refused", now)
                cursor = reply.next_cursor
                stop_at = epoch(walk["stop_at"])
                if cursor is None or (stop_at is not None and oldest is not None and epoch(oldest) <= stop_at):
                    if cursor is None:
                        state["feed_start"] = True
                        if walk["stop_at"] is None or state["covered_from"] is None:
                            state["covered_from"] = oldest
                    state["covered_to"] = max(walk["top_at"], state["covered_to"] or "")
                    state["walk"] = None
                    store.close_gap(SOURCE, repo, "walk", "walk-open", now)
                    store.close_gap(SOURCE, repo, "walk", "cursor-lost", now)
                else:
                    walk["after"] = cursor
                    store.open_gap(SOURCE, repo, "walk", "walk-open",
                                   f"unread between {walk['stop_at'] or 'feed start'} and {oldest}", now)
                store.close_gap(SOURCE, repo, "read", "read-failed", now)
                store.set_cursor(name, state)
    except Unreadable as problem:
        with store.tx():
            store.open_gap(SOURCE, repo, "read", "read-failed", str(problem), now)
    finally:
        with store.tx():
            store.db.execute("INSERT INTO activity_coverage(repo, pass, read_at, pages, rows_read, rows_new, not_modified, newest_at, oldest_at, walk_open) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (repo, store.pass_id, iso(now), stats["pages"], stats["read"], stats["new"], stats["not_modified"],
                              stats["newest"], stats["oldest"], 1 if state["walk"] else 0))
    return touched


def ordered(rows):
    rows = sorted(rows, key=lambda r: (epoch(r["at"]), r["event_id"]))
    out = []
    index = 0
    while index < len(rows):
        end = index
        while end < len(rows) and rows[end]["at"] == rows[index]["at"]:
            end += 1
        group = rows[index:end]
        while group:
            current = out[-1]["after_sha"] if out else None
            chosen = next((r for r in group if r["before_sha"] == current), group[0])
            group.remove(chosen)
            out.append(chosen)
        index = end
    return out


def incarnations(rows, feed_start=True):
    out, current = [], None
    for event in ordered(rows):
        created = event["before_sha"] == ZERO
        if created or current is None:
            if current is not None:
                current["end_at"], current["end_event"], current["end_basis"] = event["at"], event["event_id"], "recreated-without-deletion"
            basis = "branch_creation" if created else ("no-creation-seen" if out or feed_start else "feed-edge")
            current = {"seq": len(out) + 1, "start_at": event["at"], "start_event": event["event_id"], "start_basis": basis,
                       "end_at": None, "end_event": None, "end_basis": None, "first_sha": None, "last_sha": None, "events": [], "breaks": []}
            out.append(current)
        elif event["before_sha"] != current["last_sha"]:
            current["breaks"].append(event)
        current["events"].append(event)
        if current["first_sha"] is None and event["after_sha"] != ZERO:
            current["first_sha"] = event["after_sha"]
        if event["after_sha"] == ZERO:
            current["end_at"], current["end_event"], current["end_basis"] = event["at"], event["event_id"], "branch_deletion"
            current = None
        else:
            current["last_sha"] = event["after_sha"]
    return out


def events_of(store, repo, ref):
    return [dict(r) for r in store.db.execute("SELECT event_id, type, ref, before_sha, after_sha, at FROM activity_events WHERE repo = ? AND ref = ?", (repo, ref))]


def reconcile(store, repo, refs, now):
    feed_start = bool(state_of(store, repo)["feed_start"])
    for ref in sorted(refs):
        with store.tx():
            built = incarnations(events_of(store, repo, ref), feed_start)
            store.db.execute("DELETE FROM branch_incarnations WHERE repo = ? AND ref = ?", (repo, ref))
            current = set()
            for inc in built:
                store.db.execute("INSERT INTO branch_incarnations(repo, ref, seq, start_at, start_event, start_basis, end_at, end_event, end_basis, first_sha, last_sha, events, chain_breaks) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (repo, ref, inc["seq"], inc["start_at"], inc["start_event"], inc["start_basis"], inc["end_at"], inc["end_event"], inc["end_basis"],
                                  inc["first_sha"], inc["last_sha"], len(inc["events"]), len(inc["breaks"])))
                if inc["start_basis"] == "no-creation-seen":
                    subject = f"{ref}@{inc['start_event']}"
                    current.add((subject, "incarnation-start-unseen"))
                    store.open_gap(SOURCE, repo, subject, "incarnation-start-unseen", f"events without a branch creation from {inc['start_at']}", now)
                for event in inc["breaks"]:
                    subject = f"{ref}@{event['event_id']}"
                    current.add((subject, "chain-break"))
                    store.open_gap(SOURCE, repo, subject, "chain-break", f"before {event['before_sha']} does not follow the prior after at {event['at']}", now)
            for row in store.db.execute(f"SELECT subject, kind FROM gaps WHERE source = ? AND repo = ? AND closed_pass IS NULL AND kind IN ({','.join('?' * len(START_GAPS))}) "
                                        "AND substr(subject, 1, ?) = ?", (SOURCE, repo, *START_GAPS, len(ref) + 1, ref + "@")).fetchall():
                if (row["subject"], row["kind"]) not in current:
                    store.close_gap(SOURCE, repo, row["subject"], row["kind"], now)


def reconcile_new(store, now):
    done = store.cursor("reconciled", {"pass": 0, "feed_start": {}})
    touched = set()
    for repo in REPOS:
        feed_start = bool(state_of(store, repo)["feed_start"])
        rows = store.db.execute("SELECT DISTINCT ref FROM activity_events WHERE repo = ? AND (recorded_pass > ? OR ?)",
                                (repo, done["pass"], feed_start and not done["feed_start"].get(repo))).fetchall()
        refs = {r[0] for r in rows}
        reconcile(store, repo, refs, now)
        touched |= {(repo, ref) for ref in refs}
        done["feed_start"][repo] = feed_start
    done["pass"] = store.pass_id
    return touched, done


def mark_reconciled(store, done):
    with store.tx():
        store.set_cursor("reconciled", done)


def head_at(events, at):
    moment = epoch(at)
    before = [e for e in events if epoch(e["at"]) < moment]
    same = [e for e in events if epoch(e["at"]) == moment]
    later = [e for e in events if epoch(e["at"]) > moment]
    if same:
        states = [e["after_sha"] for e in same] + ([before[-1]["after_sha"]] if before else [])
        candidates = sorted({s for s in states if s != ZERO})
        if len(candidates) == 1:
            return {"status": "exact", "sha": candidates[0], "event": same[-1]["event_id"], "basis": "activity-chain", "candidates": None}
        return {"status": "tied", "sha": None, "event": None, "basis": "activity-same-second", "candidates": candidates}
    if not before:
        return {"status": "unknown", "sha": None, "event": None, "basis": "no-event-before", "candidates": None}
    last = before[-1]
    if later and later[0]["before_sha"] != last["after_sha"]:
        candidates = sorted({s for s in (last["after_sha"], later[0]["before_sha"]) if s != ZERO})
        return {"status": "gap", "sha": None, "event": None, "basis": "activity-chain-break", "candidates": candidates}
    if last["after_sha"] == ZERO:
        return {"status": "unknown", "sha": None, "event": None, "basis": "branch-deleted", "candidates": None}
    return {"status": "exact", "sha": last["after_sha"], "event": last["event_id"], "basis": "activity-chain", "candidates": None}


def coverage_of(state, at):
    if state["covered_from"] is None or state["covered_to"] is None:
        return "pending"
    moment = epoch(at)
    if moment < epoch(state["covered_from"]):
        return "before-feed"
    walk = state["walk"]
    if moment > epoch(state["covered_to"]) or (walk and moment >= (epoch(walk["stop_at"]) or 0)):
        return "pending"
    return "covered"
