#!/usr/bin/env python3
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_store

SCHEMA = "flightops-repeated-failures/1"
REPOS = ("backend", "web", "ios")
FAILED = ("failure", "timed_out", "startup_failure")
HEAD_CHARS = 40
READ_LIMIT = 2 * 3600
SKEW = 120
SOURCE = "ci"


LINKED_EVENTS = ("pull_request", "pull_request_target")


def linked(run, number):
    try:
        numbers = json.loads(run["pr_numbers"] or "[]")
    except ValueError:
        return None
    if not isinstance(numbers, list) or any(type(n) is not int for n in numbers):
        return None
    if numbers:
        return number in numbers
    return None if run["event"] in LINKED_EVENTS else False


def failed_attempts(store, repo, sha, number, now):
    head = store.db.execute("SELECT state, reads, last_read_at FROM ci_heads WHERE repo = ? AND sha = ?", (repo, sha)).fetchone()
    if head is None or head["reads"] < 1 or head["state"] not in ("live", "settled"):
        return None
    read_at = ledger_store.epoch(head["last_read_at"])
    if read_at is None or read_at > now + SKEW or now - read_at > READ_LIMIT:
        return None
    if store.db.execute("SELECT 1 FROM gaps WHERE source = ? AND repo = ? AND subject = ? AND closed_at IS NULL LIMIT 1", (SOURCE, repo, sha)).fetchone():
        return None
    if store.db.execute("""SELECT 1 FROM ci_fetches WHERE repo = ? AND state IN ('todo', 'not_retained')
                           AND run_id IN (SELECT run_id FROM ci_run_attempts WHERE repo = ? AND tested_sha = ?) LIMIT 1""", (repo, repo, sha)).fetchone():
        return None
    rows = store.db.execute("SELECT run_id, run_attempt, requested_at, started_at, pr_numbers, event FROM ci_attempt_latest "
                            "WHERE repo = ? AND tested_sha = ? AND conclusion IN (?, ?, ?)", (repo, sha) + FAILED).fetchall()
    links = [linked(r, number) for r in rows]
    if any(link is None for link in links):
        return None
    rows = [r for r, link in zip(rows, links) if link]
    if any(not (r["requested_at"] or r["started_at"]) for r in rows):
        return None
    return len({(r["run_id"], r["run_attempt"]) for r in rows}), read_at


def open_heads(directory):
    with open(os.path.join(directory, "raw", "repos.json"), encoding="utf-8") as handle:
        raw = json.load(handle)
    out = []
    for pr in raw.get("prs") or []:
        sha = pr.get("head")
        if pr.get("repo") in REPOS and type(pr.get("number")) is int and isinstance(sha, str) and len(sha) == HEAD_CHARS and all(c in "0123456789abcdef" for c in sha):
            out.append((pr["repo"], pr["number"], sha))
    return sorted(out)


def run(directory, now):
    try:
        heads = open_heads(directory)
    except (OSError, ValueError, AttributeError):
        return {"schema": SCHEMA, "error": "no pull request list"}
    if not os.path.exists(os.path.join(directory, ledger_store.FILE)):
        return {"schema": SCHEMA, "error": "no ledger yet"}
    try:
        store = ledger_store.Store(directory, readonly=True)
    except ledger_store.Refused as refused:
        return {"schema": SCHEMA, "error": str(refused)}
    try:
        prs = []
        for repo, number, sha in heads:
            found = failed_attempts(store, repo, sha, number, now)
            prs.append({"repo": repo, "number": number, "head": sha, "failed": found[0] if found else None,
                        "read": found is not None, "read_at": found[1] if found else None})
    finally:
        store.close()
    return {"schema": SCHEMA, "generated": now, "prs": prs}


def main(argv):
    if not argv:
        print(json.dumps({"error": "usage: failures.py <data directory>"}))
        return 2
    print(json.dumps(run(argv[0], time.time()), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
