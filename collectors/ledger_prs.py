import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghfacts
import ledger_activity
import receipts
from ledger_github import OWNER, REPOS, Unreadable
from ledger_store import ZERO, digest, epoch, iso

PAGE_SIZE = 100
PAGES_PER_PASS = 4
TIMELINE_LIMIT = 30
OVERLAP = 1800
POLICY_EVERY = 86400
SOURCE = "prs"
INCOMPLETE = "timeline-incomplete"
TIMELINE_TYPES = ("READY_FOR_REVIEW_EVENT", "CONVERT_TO_DRAFT_EVENT", "CLOSED_EVENT", "REOPENED_EVENT", "MERGED_EVENT",
                  "HEAD_REF_FORCE_PUSHED_EVENT", "HEAD_REF_DELETED_EVENT", "HEAD_REF_RESTORED_EVENT")
FIELDS = """number state isDraft createdAt updatedAt mergedAt closedAt headRefName headRefOid isCrossRepository baseRefName
  headRepository { nameWithOwner } mergeCommit { oid } author { login }
  timelineItems(first: %d, itemTypes: [%s]) { pageInfo { hasNextPage } nodes { __typename
    ... on ReadyForReviewEvent { createdAt } ... on ConvertToDraftEvent { createdAt } ... on ClosedEvent { createdAt }
    ... on ReopenedEvent { createdAt } ... on MergedEvent { createdAt } ... on HeadRefDeletedEvent { createdAt } ... on HeadRefRestoredEvent { createdAt }
    ... on HeadRefForcePushedEvent { createdAt beforeCommit { oid } afterCommit { oid } } } }""" % (TIMELINE_LIMIT, ", ".join(TIMELINE_TYPES))


def page_query(after):
    parts = []
    for repo, cursor in after.items():
        tail = f", after: {json.dumps(cursor)}" if cursor else ""
        parts.append(f'{repo}: repository(owner: "{OWNER}", name: "{REPOS[repo]}") {{ pullRequests(first: {PAGE_SIZE}, '
                     f'orderBy: {{field: UPDATED_AT, direction: DESC}}{tail}) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {FIELDS} }} }} }}')
    return " ".join(parts)


def state_of(store, repo):
    return store.cursor(f"prs:{repo}", {"covered_to": None, "walk": None})


def take_pr(store, repo, node, now):
    number = node.get("number")
    created, updated = node.get("createdAt"), node.get("updatedAt")
    if type(number) is not int or epoch(created) is None or epoch(updated) is None:
        return None
    timeline = node.get("timelineItems") or {}
    whole = 1 if (timeline.get("pageInfo") or {}).get("hasNextPage") is False else 0
    store.db.execute(
        """INSERT INTO prs(repo, number, state, is_draft, created_at, updated_at, merged_at, closed_at, head_ref, head_sha, head_repo, cross_repo, base_ref, merge_sha,
           author, timeline_complete, first_pass, last_pass) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(repo, number) DO UPDATE SET state = excluded.state, is_draft = excluded.is_draft, updated_at = excluded.updated_at, merged_at = excluded.merged_at,
           closed_at = excluded.closed_at, head_ref = excluded.head_ref, head_sha = excluded.head_sha, head_repo = excluded.head_repo, cross_repo = excluded.cross_repo,
           base_ref = excluded.base_ref, merge_sha = excluded.merge_sha, author = excluded.author, timeline_complete = excluded.timeline_complete, last_pass = excluded.last_pass""",
        (repo, number, node.get("state") or "UNKNOWN", None if node.get("isDraft") is None else int(node["isDraft"]), created, updated, node.get("mergedAt"), node.get("closedAt"),
         node.get("headRefName"), node.get("headRefOid"), (node.get("headRepository") or {}).get("nameWithOwner"),
         None if node.get("isCrossRepository") is None else int(node["isCrossRepository"]), node.get("baseRefName"), (node.get("mergeCommit") or {}).get("oid"),
         (node.get("author") or {}).get("login"), whole, store.pass_id, store.pass_id))
    for item in timeline.get("nodes") or []:
        if isinstance(item, dict) and item.get("createdAt"):
            store.db.execute("INSERT OR IGNORE INTO pr_timeline(repo, number, type, at, before_sha, after_sha) VALUES (?, ?, ?, ?, ?, ?)",
                             (repo, number, item.get("__typename"), item["createdAt"], (item.get("beforeCommit") or {}).get("oid") or "",
                              (item.get("afterCommit") or {}).get("oid") or ""))
    if whole:
        store.close_gap(SOURCE, repo, f"#{number}", "timeline-truncated", now)
    else:
        store.open_gap(SOURCE, repo, f"#{number}", "timeline-truncated", f"first {TIMELINE_LIMIT} items kept", now)
    return number


def read_inventory(store, client, now):
    changed = set()
    states = {repo: state_of(store, repo) for repo in REPOS}
    topped = set()
    for _ in range(PAGES_PER_PASS):
        after = {}
        for repo, state in states.items():
            if state["walk"] is not None:
                after[repo] = state["walk"]["after"]
            elif repo not in topped:
                after[repo] = None
        if not after:
            break
        try:
            data, errors = client.graphql_query(page_query(after))
        except Unreadable as problem:
            with store.tx():
                for repo in after:
                    store.open_gap(SOURCE, repo, "inventory", "read-failed", str(problem), now)
            break
        failed = {str((e.get("path") or ["all"])[0]) for e in errors if isinstance(e, dict)}
        for repo in after:
            connection = (data.get(repo) or {}).get("pullRequests") if isinstance(data.get(repo), dict) else None
            with store.tx():
                if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list) or repo in failed or "all" in failed:
                    store.open_gap(SOURCE, repo, "inventory", "read-failed", "repository missing from the answer", now)
                    topped.add(repo)
                    continue
                state = states[repo]
                if state["walk"] is None:
                    topped.add(repo)
                    stop = epoch(state["covered_to"]) - OVERLAP if state["covered_to"] else None
                    state["walk"] = {"after": None, "stop_at": iso(stop) if stop else None, "top_at": iso(now)}
                walk = state["walk"]
                oldest = None
                for node in connection["nodes"]:
                    number = take_pr(store, repo, node, now) if isinstance(node, dict) else None
                    if number is None:
                        store.open_gap(SOURCE, repo, "inventory", "row-malformed", "a PR without identity was refused", now)
                        continue
                    changed.add((repo, number))
                    updated = epoch(node["updatedAt"])
                    oldest = updated if oldest is None else min(oldest, updated)
                page = connection.get("pageInfo") or {}
                more, cursor = page.get("hasNextPage") is True, page.get("endCursor")
                stop_at = epoch(walk["stop_at"])
                if not more or (stop_at is not None and oldest is not None and oldest <= stop_at):
                    state["covered_to"] = walk["top_at"]
                    state["walk"] = None
                    store.close_gap(SOURCE, repo, "inventory", "walk-open", now)
                elif not cursor or cursor == walk["after"]:
                    state["walk"] = None
                    store.open_gap(SOURCE, repo, "inventory", "read-failed", "missing or repeated cursor", now)
                    store.set_cursor(f"prs:{repo}", state)
                    continue
                else:
                    walk["after"] = cursor
                    store.open_gap(SOURCE, repo, "inventory", "walk-open", f"unread older than {iso(oldest) if oldest else 'unknown'}", now)
                store.close_gap(SOURCE, repo, "inventory", "read-failed", now)
                store.set_cursor(f"prs:{repo}", state)
    return changed


def bind(pr, built):
    expected = f"{OWNER}/{REPOS[pr['repo']]}".lower()
    if pr["cross_repo"] is None or pr["head_repo"] is None or not pr["head_ref"]:
        return "head-repository-unknown", None
    if pr["cross_repo"] or pr["head_repo"].lower() != expected:
        return "head-repository-differs", None
    moment = epoch(pr["created_at"])
    candidates = [i for i in built if (epoch(i["start_at"]) <= moment or i["start_basis"] == "feed-edge") and (i["end_at"] is None or epoch(i["end_at"]) >= moment)]
    if len(candidates) > 1:
        holding = [i for i in candidates if any(e["after_sha"] == pr["head_sha"] for e in i["events"])]
        if len(holding) != 1:
            return "ambiguous-incarnation", None
        candidates = holding
    if not candidates:
        return "no-incarnation-at-open", None
    return "bound", candidates[0]


def final_check(pr, inc, built):
    if inc is None:
        return "not-checked"
    if any(e["after_sha"] == pr["head_sha"] for e in inc["events"]):
        return "in-incarnation"
    if any(e["after_sha"] == pr["head_sha"] for i in built for e in i["events"]):
        return "other-incarnation"
    return "absent"


def first_ready(pr, timeline):
    marks = sorted((t for t in timeline if t["type"] in ("ReadyForReviewEvent", "ConvertToDraftEvent")), key=lambda t: epoch(t["at"]))
    if marks:
        if marks[0]["type"] == "ReadyForReviewEvent":
            return marks[0]["at"], "ready-for-review-event"
        return pr["created_at"], "opened-ready-before-draft-event"
    if not pr["timeline_complete"]:
        return None, INCOMPLETE
    if pr["is_draft"] is None:
        return None, "draft-state-unknown"
    if pr["is_draft"]:
        return None, "still-draft" if pr["state"] == "OPEN" else "closed-as-draft"
    return pr["created_at"], "opened-ready"


def witness_at(at, coverage, binding, inc):
    if binding == "pending" or coverage == "pending":
        return {"status": "pending", "sha": None, "event": None, "basis": "activity-not-yet-covered", "candidates": None}
    if coverage == "before-feed":
        return {"status": "unknown", "sha": None, "event": None, "basis": "before-activity-feed", "candidates": None}
    if inc is None:
        return {"status": "unknown", "sha": None, "event": None, "basis": binding, "candidates": None}
    return ledger_activity.head_at(inc["events"], at)


def enqueue(store, repo, number, sha, pushed_at):
    store.db.execute("""INSERT INTO ci_heads(repo, sha, number, pushed_at, discovered_pass, state, due_at) VALUES (?, ?, ?, ?, ?, 'queued', 0)
                        ON CONFLICT(repo, sha) DO UPDATE SET pushed_at = coalesce(ci_heads.pushed_at, excluded.pushed_at)""",
                     (repo, sha, number, pushed_at, store.pass_id))


def note_head(store, repo, number, sha, role, event, pushed_at):
    if sha and sha != ZERO:
        store.db.execute("""INSERT INTO pr_heads(repo, number, sha, role, event_id, pushed_at, first_pass) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(repo, number, sha, role) DO UPDATE SET pushed_at = coalesce(pr_heads.pushed_at, excluded.pushed_at)""",
                         (repo, number, sha, role, event, pushed_at, store.pass_id))
        enqueue(store, repo, number, sha, pushed_at)


def span_events(pr, inc):
    start = epoch(pr["created_at"])
    ended = epoch(pr["merged_at"] or pr["closed_at"])
    return [e for e in inc["events"] if epoch(e["at"]) >= start and (ended is None or epoch(e["at"]) <= ended) and e["after_sha"] != ZERO]


def resolve_pr(store, pr, activity_state, now):
    repo, number = pr["repo"], pr["number"]
    built = ledger_activity.incarnations(ledger_activity.events_of(store, repo, f"refs/heads/{pr['head_ref']}"), bool(activity_state["feed_start"])) if pr["head_ref"] else []
    opening = ledger_activity.coverage_of(activity_state, pr["created_at"])
    binding, inc = bind(pr, built)
    if binding in ("bound", "no-incarnation-at-open", "ambiguous-incarnation") and opening != "covered":
        binding, inc = ("pending", None) if opening == "pending" else ("before-activity-feed", None)
    timeline = store.db.execute("SELECT type, at FROM pr_timeline WHERE repo = ? AND number = ?", (repo, number)).fetchall()
    ready_at, ready_basis = first_ready(pr, timeline)
    at_open = witness_at(pr["created_at"], opening, binding, inc)
    if ready_at:
        at_ready = witness_at(ready_at, ledger_activity.coverage_of(activity_state, ready_at), binding, inc)
    else:
        at_ready = {"status": "pending" if ready_basis == INCOMPLETE else "not-applicable", "sha": None, "event": None, "basis": ready_basis, "candidates": None}
    with store.tx():
        store.db.execute(
            """INSERT OR REPLACE INTO pr_witness(repo, number, incarnation_seq, binding, head_at_open, head_at_open_status, head_at_open_basis, head_at_open_event, head_at_open_candidates,
               first_ready_at, first_ready_at_basis, first_ready_head, first_ready_head_status, first_ready_head_basis, first_ready_head_event, first_ready_head_candidates,
               final_head_check, resolved_pass) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (repo, number, inc["seq"] if inc else None, binding, at_open["sha"], at_open["status"], at_open["basis"], at_open["event"], json.dumps(at_open["candidates"]) if at_open["candidates"] else None,
             ready_at, ready_basis, at_ready["sha"], at_ready["status"], at_ready["basis"], at_ready["event"], json.dumps(at_ready["candidates"]) if at_ready["candidates"] else None,
             final_check(pr, inc, built), store.pass_id))
        pushed = {}
        if inc:
            for event in span_events(pr, inc):
                pushed[event["after_sha"]] = event["at"]
                note_head(store, repo, number, event["after_sha"], "push", event["event_id"], event["at"])
            pushed.update({e["after_sha"]: e["at"] for e in inc["events"] if e["after_sha"] == pr["head_sha"]})
        for role, seen in (("head-at-open", at_open), ("first-ready", at_ready)):
            if seen["sha"]:
                at = next((e["at"] for e in inc["events"] if e["event_id"] == seen["event"]), None) if inc else None
                note_head(store, repo, number, seen["sha"], role, seen["event"], at)
        note_head(store, repo, number, pr["head_sha"], "observed-head", None, pushed.get(pr["head_sha"]))


def resolve(store, changed, touched, now):
    wanted = set(changed)
    for repo, ref in touched:
        if ref.startswith("refs/heads/"):
            wanted |= {(repo, r[0]) for r in store.db.execute("SELECT number FROM prs WHERE repo = ? AND head_ref = ?", (repo, ref[11:]))}
    wanted |= {(r[0], r[1]) for r in store.db.execute(
        """SELECT p.repo, p.number FROM prs p LEFT JOIN pr_witness w ON w.repo = p.repo AND w.number = p.number
           WHERE w.number IS NULL OR p.last_pass > w.resolved_pass OR w.head_at_open_status = 'pending' OR w.first_ready_head_status = 'pending'""")}
    states = {repo: ledger_activity.state_of(store, repo) for repo in REPOS}
    for repo, number in sorted(wanted):
        pr = store.db.execute("SELECT * FROM prs WHERE repo = ? AND number = ?", (repo, number)).fetchone()
        if pr is not None:
            resolve_pr(store, pr, states[repo], now)
    return len(wanted)


def read_policy(store, client, now):
    due = [repo for repo in REPOS if now - (store.cursor(f"policy:{repo}", {}).get("confirmed") or 0) >= POLICY_EVERY]
    if not due:
        return
    try:
        data, errors = client.graphql_query(ghfacts.rules_query())
    except Unreadable as problem:
        with store.tx():
            store.open_gap("policy", "all", "rules", "read-failed", str(problem), now)
        return
    with store.tx():
        for repo in due:
            rules = data.get(repo)
            if not isinstance(rules, dict) or receipts.required_checks(rules) is None or any((e.get("path") or [None])[0] == repo for e in errors if isinstance(e, dict)):
                store.open_gap("policy", repo, "rules", "read-failed", "rules missing or incomplete", now)
                continue
            mark = digest(rules)
            last = store.db.execute("SELECT digest FROM policy_reads WHERE repo = ? ORDER BY id DESC LIMIT 1", (repo,)).fetchone()
            if last is None or last[0] != mark:
                store.db.execute("INSERT INTO policy_reads(repo, rules, digest, read_at, pass) VALUES (?, ?, ?, ?, ?)", (repo, json.dumps(rules, sort_keys=True), mark, iso(now), store.pass_id))
            store.set_cursor(f"policy:{repo}", {"confirmed": now})
            store.close_gap("policy", repo, "rules", "read-failed", now)
        if all(data.get(repo) for repo in due):
            store.close_gap("policy", "all", "rules", "read-failed", now)


def attach_policy(store):
    latest = {}
    for repo in REPOS:
        row = store.db.execute("SELECT id, rules, read_at FROM policy_reads WHERE repo = ? ORDER BY id DESC LIMIT 1", (repo,)).fetchone()
        if row:
            latest[repo] = (row["id"], json.loads(row["rules"]), row["read_at"])
    attached = 0
    with store.tx():
        rows = store.db.execute(
            """SELECT DISTINCT h.repo, h.sha, h.number, h.pushed_at, p.base_ref FROM pr_heads h JOIN prs p ON p.repo = h.repo AND p.number = h.number
               LEFT JOIN head_policy q ON q.repo = h.repo AND q.sha = h.sha AND q.number = h.number
               LEFT JOIN pr_witness w ON w.repo = h.repo AND w.number = h.number
               WHERE q.sha IS NULL AND p.base_ref IS NOT NULL AND (h.pushed_at IS NOT NULL OR w.binding <> 'pending')""").fetchall()
        for row in rows:
            held = latest.get(row["repo"])
            if not held:
                continue
            required = receipts.required_checks(held[1], row["base_ref"])
            basis = "head-time-unknown" if not row["pushed_at"] else "read-before-head" if epoch(held[2]) <= epoch(row["pushed_at"]) else "read-after-head"
            attached += store.db.execute(
                "INSERT OR IGNORE INTO head_policy(repo, sha, number, base_ref, status, required, policy_read, policy_read_at, basis, pass) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["repo"], row["sha"], row["number"], row["base_ref"], "known" if required is not None else "unknown", json.dumps(required), held[0], held[2], basis, store.pass_id)).rowcount
    return attached
