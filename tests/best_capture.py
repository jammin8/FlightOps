#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collectors"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import best_evidence as evidence
import best_support as support
import models
import settings

STORE = os.path.expanduser("~/.orch/userdata/statev2.sqlite")
LEDGER = os.path.expanduser("~/.local/share/flightops/evidence.sqlite")
RAW = os.path.expanduser("~/.local/share/flightops/raw")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "best-slice")
OUT = os.path.join(FIXTURES, "synthetic-capture.json")
EARLIER = os.path.join(FIXTURES, "repos-generation-1.json")
PROJECTS = {v: k for k, v in settings.REPOS.items()}
ROW_FIELDS = ("id", "projectId", "title", "providerInstanceId", "pullRequests", "archivedAt", "updatedAt")
LINK_FIELDS = ("host", "repository", "number", "url", "source", "linkedAt")
SNAPSHOT_FIELDS = ("state", "mergedAt")
ITEM_FIELDS = ("id", "type", "runId", "messageId", "providerTurnId", "status")


def payloads(store, table, thread):
    return [json.loads(p) for (p,) in store.execute(f"SELECT payload_json FROM {table} WHERE thread_id = ?", (thread,))]


def trimmed_link(link):
    return {key: link.get(key) for key in LINK_FIELDS} | {"snapshot": {key: (link.get("snapshot") or {}).get(key) for key in SNAPSHOT_FIELDS}}


def shell_row(row):
    return {key: row.get(key) for key in ROW_FIELDS} | {"status": "completed", "pullRequests": [trimmed_link(l) for l in row.get("pullRequests") or []]}


def trimmed_message(message):
    text = message.get("text") or ""
    if message.get("role") == "user":
        text = text.partition(evidence.BRIEF_END)[0] + evidence.BRIEF_END + "\n[rest of the brief omitted from this capture]"
    else:
        text = "\n".join(text.split("\n")[:5]) + "\n[rest of the report omitted from this capture]" if evidence.REPORT.match(text) else "[assistant text omitted from this capture]"
    return {key: message.get(key) for key in ("id", "runId", "role", "streaming", "createdAt")} | {"text": text}


def snapshot(store, thread, row):
    runs = [{key: r.get(key) for key in ("id", "threadId", "status", "activeAttemptId", "userMessageId", "completedAt", "modelSelection")} for r in payloads(store, "orchestration_v2_projection_runs", thread)]
    turns = [{key: t.get(key) for key in ("id", "runAttemptId", "status", "startedAt", "completedAt", "nativeTurnRef")} for t in payloads(store, "orchestration_v2_projection_provider_turns", thread)]
    attempts = [{key: a.get(key) for key in ("id", "runId", "status")} for a in payloads(store, "orchestration_v2_projection_run_attempts", thread)]
    items = [{key: i.get(key) for key in ITEM_FIELDS} for i in payloads(store, "orchestration_v2_projection_turn_items", thread) if i.get("type") == "assistant_message"]
    return {"projection": {"thread": {"id": thread}, "runs": runs, "attempts": attempts, "providerTurns": turns, "turnItems": items,
                           "messages": [trimmed_message(m) for m in payloads(store, "orchestration_v2_projection_messages", thread)]}}


def ledger_capture(cohort):
    ledger = sqlite3.connect(f"file:{LEDGER}?mode=ro&immutable=1", uri=True)
    ledger.row_factory = sqlite3.Row
    rows = [dict(row) for row in ledger.execute("SELECT * FROM pr_witness") if evidence.pr_id(evidence.REPOSITORIES[row["repo"]], row["number"]) in cohort]
    gaps = [dict(row) for row in ledger.execute("SELECT source, repo, subject, kind, detail, opened_pass, opened_at FROM gaps WHERE closed_pass IS NULL AND source IN ('activity', 'prs')")]
    return {"rows": rows, "gaps": gaps, "passes": [dict(row) for row in ledger.execute("SELECT id, started_at, finished_at, outcome FROM passes WHERE finished_at IS NOT NULL")]}


def main():
    store = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    store.execute("BEGIN")
    with open(os.path.join(RAW, "repos.json")) as f:
        repos = json.load(f)
    with open(os.path.join(RAW, "models.json")) as f:
        report = json.load(f)
    with open(EARLIER) as f:
        earlier = json.load(f)
    held = {(m["repo"], m["number"]) for m in repos["recent"]}
    repos["recent"] += [m for m in earlier["recent"] if (m["repo"], m["number"]) not in held]
    rows = [json.loads(p) for (p,) in store.execute("SELECT payload_json FROM orchestration_v2_projection_threads")]
    projects = {pid: PROJECTS[name] for pid, name in store.execute("SELECT project_id, title FROM projection_projects") if name in PROJECTS}
    cohort = evidence.cohort_rows(repos)
    short = {PROJECTS[key.split("/", 1)[1].split("#")[0]] + "#" + key.split("#")[1] for key in cohort}
    kept, snapshots = [], {}
    links, _, _ = evidence.exact_links(rows)
    linked = {link["thread"] for key in cohort for link in links.get(key, [])}
    for row in rows:
        hint, _ = models.review_hint(row.get("title"), projects.get(row.get("projectId")), row.get("providerInstanceId"))
        if hint and hint["pr"] in short:
            kept.append(shell_row(row))
            snapshots[row["id"]] = snapshot(store, row["id"], row)
        elif row["id"] in linked:
            kept.append(shell_row(row))
    threads = {row["id"] for row in kept}
    runs = {rid: {key: run.get(key) for key in ("thread", "tokens", "model", "effort", "harness", "status")} for rid, run in report["runs"].items() if run.get("thread") in threads}
    store.rollback()
    out = {"capture": {"made_at": int(time.time()), "how": "read-only sqlite projection of Orchestrator, the evidence ledger's witness rows and the collector's own raw files; no Orchestrator HTTP and no GitHub call",
                       "not_real": ["project repositoryIdentity is filled from the project title", "thread status is set to completed because the projection row has no shell status",
                                    "recent holds the rows of both collection generations"],
                       "omitted": "message text past the brief header and the report header; run fields not read by the adapter; turn items other than assistant messages"},
           "shell": {evidence.STAND_IN_MARK: "tests/best_capture.py", "snapshotSequence": 1, "projects": [{"id": pid, "repositoryIdentity": {"canonicalKey": f"github.com/{settings.OWNER.lower()}/{settings.REPOS[name]}"}} for pid, name in projects.items()],
                     "threads": kept, "archivedThreads": []},
           "snapshots": snapshots, "repos": repos, "generations": [earlier], "witness": ledger_capture(cohort), "models": {"schema": report["schema"], "generated": report["generated"], "history_start": report.get("history_start"), "runs": runs}}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=0, sort_keys=True)
    print(OUT, os.path.getsize(OUT), len(kept), len(snapshots))
    recompute()


def recompute():
    with open(support.PUBLICATION, "w") as f:
        json.dump(support.published(support.capture())[0], f, sort_keys=True)


if __name__ == "__main__":
    recompute() if sys.argv[1:] == ["--recompute"] else main()
