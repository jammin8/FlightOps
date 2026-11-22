#!/usr/bin/env python3
import fcntl
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402
import bottlenecks  # noqa: E402
import config  # noqa: E402
import github  # noqa: E402
import metrics  # noqa: E402
import store as storage  # noqa: E402

USAGE = "usage: flightops.py collect [--seconds N] | status [--json] | repos | use OWNER/NAME [OWNER/NAME ...] | replay FROM TO [OWNER/NAME ...]"


def status_path():
    return os.environ.get("FLIGHTOPS_STATUS_FILE") or os.path.expanduser("~/.local/state/flightops/status.json")


def do_collect(seconds=90, runner=github.run_command, now=None):
    settings, problem = config.load()
    db = storage.Store()
    lock = open(os.path.join(storage.data_dir(), "collect.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("flightops: another collection is running", file=sys.stderr)
        return 0
    try:
        now = int(time.time() if now is None else now)
        repos = [r["name"] for r in settings["repos"]]
        result = {"stopped": None, "errors": []}
        if repos:
            client = github.Client(runner=runner, seconds=seconds)
            with db.tx():
                db.db.execute("INSERT INTO passes(started_at) VALUES (?)", (storage.iso(now),))
                pass_id = db.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            result = collect.run_pass(db, client, repos, now)
            with db.tx():
                db.db.execute("""UPDATE passes SET finished_at = ?, outcome = ?, rest_requests = ?, rest_not_modified = ?, graphql_requests = ?,
                                 core_remaining = ?, graphql_remaining = ?, report = ? WHERE id = ?""",
                              (storage.iso(time.time()), "error" if result["errors"] else result["stopped"] or "complete", client.requests, client.not_modified,
                               client.graphql_requests, client.core, client.graphql, json.dumps(result), pass_id))
        metrics.write(metrics.build(db, settings, int(time.time()), problem or "ok"), status_path())
        for error in result["errors"]:
            print(f"flightops: {error}", file=sys.stderr)
        return 1 if result["errors"] and result["stopped"] == "github" else 0
    finally:
        lock.close()
        db.close()


def do_status(as_json):
    try:
        with open(status_path()) as f:
            status = json.load(f)
    except (OSError, ValueError):
        print("flightops: no status yet; run collect first", file=sys.stderr)
        return 2
    if as_json:
        json.dump(status, sys.stdout, indent=1)
        print()
        return 0
    everything = status["views"]["all"]
    print(everything["status_line"])
    for stage in everything["stages"]:
        print(f"  {stage['label']:<15} {'–' if stage['count'] is None else stage['count']}")
    for pr in (p for p in status["prs"] if p["needs_you"]):
        print(f"  needs you: {pr['id']} {pr['needs_you']}")
    return 0


def do_repos(runner=github.run_command):
    client = github.Client(runner=runner, seconds=60)
    found = []
    try:
        for page in range(1, 6):
            reply = client.rest(f"user/repos?per_page=100&sort=pushed&page={page}", budget=False)
            if not reply.ok or not isinstance(reply.body, list):
                raise github.Unreadable("gh is not signed in" if reply.status == 401 else f"repository list unreadable (status {reply.status})")
            found += [{"name": r.get("full_name"), "private": bool(r.get("private")), "pushed_at": r.get("pushed_at"), "archived": bool(r.get("archived"))}
                      for r in reply.body if isinstance(r, dict) and r.get("full_name")]
            if len(reply.body) < 100:
                break
    except (github.Unreadable, github.Stop) as problem:
        print(json.dumps({"error": str(problem)}))
        return 1
    print(json.dumps({"repos": found}))
    return 0


def do_replay(start, end, only):
    settings, _ = config.load()
    repos = only or [r["name"] for r in settings["repos"]]
    db = storage.Store()
    try:
        days = bottlenecks.replay(db, repos, storage.epoch(start + "T00:00:00Z"), storage.epoch(end + "T23:59:59Z"))
    finally:
        db.close()
    print(f"{'day':<11}{'stage':<12}{'verdict':<10}{'slowing':<9}{'stuck':<7}{'load':>6}{'oldest':>9}{'slow end':>10}{'normal':>9}")
    for (day, stage), row in sorted(days.items()):
        print(f"{storage.iso(day * bottlenecks.DAY)[:10]:<11}{stage:<12}{row['state']:<10}{row['slowing'] or '':<9}{row['stuck'] or '':<7}{row['load']:>6}"
              f"{row['oldest_min']:>9}{row['slow_end_min']:>10}{'' if row['normal_min'] is None else row['normal_min']:>9}")
    tally = {}
    for (_, stage), row in days.items():
        tally.setdefault(stage, {}).setdefault(row["state"], 0)
        tally[stage][row["state"]] += 1
    for stage, counts in sorted(tally.items()):
        print(f"{stage}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


def main(arguments):
    if not arguments:
        print(USAGE, file=sys.stderr)
        return 2
    command, rest = arguments[0], arguments[1:]
    if command == "collect":
        seconds = int(rest[1]) if len(rest) == 2 and rest[0] == "--seconds" and rest[1].isdigit() else 90
        return do_collect(seconds)
    if command == "status":
        return do_status("--json" in rest)
    if command == "repos":
        return do_repos()
    if command == "use" and rest and all(config.NAME.match(r) for r in rest):
        saved = config.save(rest)
        print(json.dumps(saved))
        return 0
    if command == "replay" and len(rest) >= 2 and all(storage.epoch(d + "T00:00:00Z") for d in rest[:2]) and all(config.NAME.match(r) for r in rest[2:]):
        return do_replay(rest[0], rest[1], rest[2:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
