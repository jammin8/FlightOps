#!/usr/bin/env python3
import fcntl
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_activity
import ledger_ci
import ledger_github
import ledger_import
import ledger_prs
import ledger_store
from ledger_github import REPOS, Stop, Unreadable

SCHEMA = "flightops-ledger-pass/1"
DEFAULT_SECONDS = 60
LOCKED = 5
TABLES = ("activity_events", "branch_incarnations", "prs", "pr_witness", "pr_heads", "head_policy", "ci_heads", "ci_run_attempts", "ci_jobs", "ci_check_runs", "ci_statuses")


def tally(store, sql):
    return {str(row[0]): row[1] for row in store.db.execute(sql).fetchall()}


def progress(store, now):
    cutoff = ledger_store.iso(now - ledger_ci.RETENTION)
    heads = """FROM ci_heads h JOIN prs p ON p.repo = h.repo AND p.number = h.number WHERE coalesce(h.pushed_at, p.created_at) >= ?"""
    last = store.db.execute("SELECT outcome, stopped, rest_requests, rest_not_modified, graphql_points FROM passes WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    walks = sum(1 for repo in REPOS if ledger_activity.state_of(store, repo)["walk"]) + sum(1 for repo in REPOS if ledger_prs.state_of(store, repo)["walk"])
    return {"backfill_heads_remaining": store.db.execute("SELECT count(*) FROM ci_heads WHERE state = 'queued'").fetchone()[0],
            "backfill_fetches_remaining": store.db.execute("SELECT count(*) FROM ci_fetches WHERE state = 'todo'").fetchone()[0],
            "heads_in_window": store.db.execute(f"SELECT count(*) {heads}", (cutoff,)).fetchone()[0],
            "heads_in_window_unread": store.db.execute(f"SELECT count(*) {heads} AND h.state = 'queued'", (cutoff,)).fetchone()[0],
            "oldest_unread_in_window": store.db.execute(f"SELECT min(coalesce(h.pushed_at, p.created_at)) {heads} AND h.state = 'queued'", (cutoff,)).fetchone()[0],
            "run_feed": {repo: {"through": state["through"], "swept": ledger_store.iso(state["swept"]) if state["swept"] else None, "sweep_page": state["page"]}
                         for repo, state in ((r, ledger_ci.feed_state(store, r)) for r in REPOS)},
            "walks_open": walks,
            "import_files_pending": store.db.execute("SELECT count(*) FROM import_files WHERE state = 'pending'").fetchone()[0],
            "gaps_open": store.db.execute("SELECT count(*) FROM gaps WHERE closed_pass IS NULL").fetchone()[0],
            "last_pass": dict(last) if last else None}


def report(store, now):
    rows = {table: store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}
    activity = {repo: dict(ledger_activity.state_of(store, repo), walk_open=ledger_activity.state_of(store, repo)["walk"] is not None) for repo in REPOS}
    for held in activity.values():
        held.pop("walk")
    last = store.db.execute("SELECT * FROM passes WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return {"schema": SCHEMA, "generated": int(time.time()), "progress": progress(store, now), "rows": rows,
            "gaps_open": {f"{row[0]}/{row[1]}": row[2] for row in store.db.execute("SELECT source, kind, count(*) FROM gaps WHERE closed_pass IS NULL GROUP BY 1, 2")},
            "activity": activity,
            "prs": {"binding": tally(store, "SELECT binding, count(*) FROM pr_witness GROUP BY 1"),
                    "head_at_open": tally(store, "SELECT head_at_open_status, count(*) FROM pr_witness GROUP BY 1"),
                    "first_ready_head": tally(store, "SELECT first_ready_head_status, count(*) FROM pr_witness GROUP BY 1"),
                    "final_head_check": tally(store, "SELECT final_head_check, count(*) FROM pr_witness GROUP BY 1")},
            "ci": {"heads": tally(store, "SELECT state, count(*) FROM ci_heads GROUP BY 1"),
                   "fetches": tally(store, "SELECT state, count(*) FROM ci_fetches GROUP BY 1"),
                   "run_sources": tally(store, "SELECT source, count(*) FROM ci_run_attempts GROUP BY 1"),
                   "conclusions": tally(store, "SELECT coalesce(conclusion, 'none'), count(*) FROM ci_attempt_latest GROUP BY 1"),
                   "retry_recovered": tally(store, "SELECT level, count(*) FROM ci_retry_outcomes GROUP BY 1"),
                   "attempts_unseen": store.db.execute(
                       """SELECT count(*) FROM (SELECT repo, run_id, max(run_attempt) AS top, count(DISTINCT run_attempt) AS seen FROM ci_run_attempts GROUP BY repo, run_id) WHERE seen < top""").fetchone()[0]},
            "import": {"files": tally(store, "SELECT state, count(*) FROM import_files GROUP BY 1"),
                       "rows_read": store.db.execute("SELECT coalesce(sum(rows_read), 0) FROM import_files").fetchone()[0],
                       "rows_new": store.db.execute("SELECT coalesce(sum(rows_new), 0) FROM import_files").fetchone()[0]},
            "last_pass": None if last is None else {key: last[key] for key in ("id", "started_at", "finished_at", "outcome", "rest_requests", "rest_not_modified", "graphql_requests", "graphql_points",
                                                                                "core_remaining_start", "core_remaining_end", "stopped")}}


def settle_history(store, now):
    touched, done = ledger_activity.reconcile_new(store, now)
    ledger_prs.resolve(store, set(), touched, now)
    ledger_activity.mark_reconciled(store, done)


class Pass:
    def __init__(self, store, client, now):
        self.store, self.client, self.now = store, client, now
        self.stopped, self.errors = None, []

    def guarded(self, name, step):
        try:
            step()
            return True
        except Stop as stop:
            self.stopped = stop.reason
        except Unreadable:
            self.errors.append(f"{name}: unreadable answer")
        except Exception as problem:
            self.errors.append(f"{name}: {type(problem).__name__}")
        return False

    def finish(self, outcome):
        store, client = self.store, self.client
        with store.tx():
            store.db.execute(
                """UPDATE passes SET finished_at = ?, outcome = ?, rest_requests = ?, rest_not_modified = ?, graphql_requests = ?, graphql_points = ?,
                   core_remaining_start = ?, core_remaining_end = ?, graphql_remaining_end = ?, stopped = ? WHERE id = ?""",
                (ledger_store.iso(time.time()), outcome, client.requests, client.not_modified, client.graphql_requests, client.graphql_points,
                 client.core_start, client.core, client.graphql, self.stopped, store.pass_id))


def run_pass(directory, seconds=DEFAULT_SECONDS, runner=ledger_github.run_command, now=None, root=ledger_import.ROOT, clock=time.monotonic):
    now = int(time.time() if now is None else now)
    store = ledger_store.Store(directory)
    try:
        store.begin_pass(now)
        pending = not store.cursor("import:manifest") or store.db.execute("SELECT 1 FROM import_files WHERE state = 'pending' LIMIT 1").fetchone()
        slice_seconds = min(ledger_import.STEP_SECONDS, seconds / 4) if pending else 0
        client = ledger_github.Client(runner=runner, clock=clock, seconds=seconds - slice_seconds)
        current = Pass(store, client, now)
        outcome = "interrupted"
        try:
            started = current.guarded("budget", client.start)
            reads = [("rules", lambda: ledger_prs.read_policy(store, client, now))]
            reads += [(f"activity {repo}", lambda repo=repo: ledger_activity.read_repo(store, client, repo, now)) for repo in REPOS]
            reads += [("inventory", lambda: ledger_prs.read_inventory(store, client, now))]
            for name, read in reads:
                if started and not current.stopped:
                    current.guarded(name, read)
            current.guarded("history", lambda: settle_history(store, now))
            current.guarded("policy", lambda: ledger_prs.attach_policy(store))
            if started and not current.stopped:
                current.guarded("ci", lambda: ledger_ci.run(store, client, now))
            current.guarded("feed", lambda: ledger_ci.check_feed(store, now))
            if slice_seconds:
                current.guarded("import", lambda: ledger_import.run(store, root, slice_seconds, clock, now))
            outcome = "error" if current.errors else "partial" if current.stopped else "complete"
        finally:
            current.finish(outcome)
        store.checkpoint()
        result = dict(report(store, now), stopped=current.stopped, errors=current.errors)
        return result
    finally:
        store.close()


def lock(directory):
    os.makedirs(directory, exist_ok=True)
    fd = os.open(os.path.join(directory, "collect.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main(argv):
    arguments = [a for a in argv if not a.startswith("--")]
    flags = [a for a in argv if a.startswith("--")]
    if not arguments:
        print(json.dumps({"error": "usage: ledger.py <data directory> [--seconds=N] [--under-collect] [--report]"}))
        return 2
    directory = arguments[0]
    if "--report" in flags:
        if not os.path.exists(os.path.join(directory, ledger_store.FILE)):
            print(json.dumps({"error": "no ledger yet"}))
            return 1
        try:
            store = ledger_store.Store(directory, readonly=True)
        except ledger_store.Refused as refused:
            print(json.dumps({"schema": SCHEMA, "error": str(refused)}))
            return 1
        print(json.dumps(report(store, int(time.time())), sort_keys=True))
        store.close()
        return 0
    held = None
    if "--under-collect" not in flags:
        held = lock(directory)
        if held is None:
            print(json.dumps({"error": "another collect holds the lock"}))
            return LOCKED
    seconds = next((float(f.split("=", 1)[1]) for f in flags if f.startswith("--seconds=")), DEFAULT_SECONDS)
    root = os.environ.get("FLIGHTOPS_LEDGER_IMPORT_ROOT") or ledger_import.ROOT
    try:
        result = run_pass(directory, seconds, root=root)
    except ledger_store.Refused as refused:
        print(json.dumps({"schema": SCHEMA, "error": str(refused)}))
        return 0
    except Exception as problem:
        print(json.dumps({"schema": SCHEMA, "error": f"ledger pass failed: {type(problem).__name__}"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
