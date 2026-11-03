#!/usr/bin/env python3
import json
import math
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import capture
import orch_api

SCHEMA = "flightops-models/1"
RECORD_SCHEMA = "flightops-models-pr/1"
TOKENS_SCHEMA = "agent-tokens/3"
TOKENS_SCRIPT = os.environ.get("FLIGHTOPS_AGENT_TOKENS") or os.path.expanduser("~/bin/agent-tokens.py")
TOKENS_SECONDS = 50
REPORT_BYTES = 64 * 1024 * 1024
BUCKETS = ("input", "cache_read", "cache_write", "output", "reasoning")
FAMILIES = {"Claude": "claude", "Codex": "codex"}
REVIEW_TITLE = re.compile(r"Review (backend|web|ios)#([1-9][0-9]*) \(([A-Z][a-z]+)(?:, [^()]*)?\) @([0-9a-f]{7,40})")
REPOSITORY = re.compile(r"(?:github\.com/)?" + re.escape(settings.OWNER) + "/(" + "|".join(re.escape(v) for v in settings.REPOS.values()) + ")", re.I)
REPO_KEYS = {v.lower(): k for k, v in settings.REPOS.items()}
SETTINGS_BASIS = "orch-requested"
LINK_ESTIMATE = "orch-link-estimate"
GITHUB_SECONDS = 1800
GITHUB_REPOS = ("backend", "web", "ios")
CLOCK_SKEW = 5
ERROR_CHARS = 120
FIELDS = {"project": "orch-shell", "run": "orch-run", "model": SETTINGS_BASIS, "effort": SETTINGS_BASIS, "harness": SETTINGS_BASIS,
          "pr": "orch-link or title-hint", "role": "title-hint", "head": "title-hint", "tokens": "agent-tokens", "allocation": "orch-link"}
UNKNOWN = ["builder", "rework", "exclusive PR usage", "provider-reported execution", "subagent allocation", "failed-before-thread dispatches", "retention"]


FOREIGN = "out-of-scope"


class Failure(Exception):
    pass


def shell_projects(base, token, opener):
    if not orch_api.LOOPBACK.fullmatch(base):
        raise Failure("Orch address is not local")
    if token is None:
        raise Failure("Orch read token unavailable")
    try:
        shell = orch_api.get(base, token, orch_api.SHELL, opener)
    except orch_api.Down:
        raise Failure("Orch unreachable") from None
    except orch_api.Refused as refused:
        raise Failure(f"Orch answered {refused.code}") from None
    except orch_api.Malformed:
        raise Failure("Orch answered malformed data") from None
    try:
        names = orch_api.project_names(shell)
        foreign = {project["id"] for project in shell["projects"]
                   if isinstance((project.get("repositoryIdentity") or {}).get("canonicalKey"), str) and project["id"] not in names}
        threads = {row["id"]: names.get(row["projectId"]) or (FOREIGN if row["projectId"] in foreign else None)
                   for row in shell["threads"] + shell["archivedThreads"]}
    except (KeyError, TypeError, AttributeError):
        raise Failure("Orch answered malformed data") from None
    return threads, "olderSettled" not in shell


def agent_tokens(cache, script=None, run=capture.run):
    command = [sys.executable, "-I", script or TOKENS_SCRIPT, "--cache", cache]
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        answer = run(command, limit=REPORT_BYTES, timeout=TOKENS_SECONDS)
    except (OSError, subprocess.SubprocessError):
        raise Failure("agent-tokens did not finish") from None
    except capture.TooLarge:
        raise Failure("agent-tokens gave too much output") from None
    if answer.returncode != 0:
        raise Failure("agent-tokens failed")
    try:
        data = json.loads(answer.stdout)
    except ValueError:
        raise Failure("agent-tokens gave unreadable output") from None
    if not isinstance(data, dict) or data.get("schema") != TOKENS_SCHEMA:
        raise Failure("agent-tokens is an unexpected version")
    if data.get("orch") != "ok":
        raise Failure("agent-tokens could not read Orch")
    if type(data.get("generated")) is not int or data["generated"] <= 0 or not isinstance(data.get("runs"), list):
        raise Failure("agent-tokens gave unreadable output")
    return data


def link_keys(links):
    keys = set()
    for link in links or []:
        match = REPOSITORY.fullmatch(link.get("repository") or "")
        if match and link.get("via") == "orch-link" and type(link.get("number")) is int and link["number"] > 0:
            keys.add(f"{REPO_KEYS[match.group(1).lower()]}#{link['number']}")
    return keys


def branch_only(links):
    return not link_keys(links) and any((link or {}).get("via") == "orch-branch" for link in links or [])


def review_hint(title, project, harness):
    match = REVIEW_TITLE.fullmatch(title or "")
    if not match:
        return None, False
    repo, number, family, head = match.groups()
    if repo != project or family not in FAMILIES or not (harness or "").lower().startswith(FAMILIES[family]):
        return None, True
    return {"pr": f"{repo}#{number}", "family": family, "head": head}, False


def bucket_usage(usage):
    if not isinstance(usage, dict):
        return None
    amounts = {name: usage.get(name) for name in BUCKETS}
    return amounts if all(type(n) is int and n >= 0 for n in amounts.values()) else None


def allocation_of(keys, links):
    if len(keys) > 1:
        return "shared"
    return "orch-link" if links else "unallocated"


def published_run(run, project, allocation):
    return {"thread": run.get("thread"), "project": project, "model": run.get("model"), "effort": run.get("effort"),
            "harness": run.get("harness"), "settings_basis": SETTINGS_BASIS, "status": run.get("status"), "started": run.get("started"),
            "ended": run.get("ended"), "tokens": bucket_usage(run.get("usage")), "usage_basis": run.get("usage_basis"),
            "allocation": allocation}


def sum_usage(rows):
    if not rows or any(row is None for row in rows):
        return None
    return {name: sum(row[name] for row in rows) for name in BUCKETS}


def possible_keys(title, links, project_known, project, harness=None):
    keys = link_keys(links)
    match = REVIEW_TITLE.fullmatch(title or "")
    if match:
        repo, number, family, _ = match.groups()
        family_fits = family in FAMILIES and (harness is None or harness.lower().startswith(FAMILIES[family]))
        if family_fits and (not project_known or project == repo):
            keys.add(f"{repo}#{number}")
    return keys


def cut_prs(unexported, threads):
    keys = set()
    for entry in unexported or []:
        thread = entry.get("thread")
        keys |= possible_keys(entry.get("title"), entry.get("links"), threads.get(thread) is not None, threads.get(thread))
    return keys


def note(path):
    try:
        with open(path) as f:
            return f.read(ERROR_CHARS).strip() or None
    except OSError:
        return None


def inventory_complete(snap):
    rows = {row["name"]: row for row in snap["repos"]}
    listed = [(row["repo"], row["number"]) for row in snap["prs"]]
    return (len(rows) == len(snap["repos"]) and set(rows) == set(GITHUB_REPOS) and len(set(listed)) == len(listed)
            and all(row.get("truncated") is False for row in rows.values()) and all(repo in rows for repo, _ in listed)
            and all(rows[name]["open"] == sum(repo == name for repo, _ in listed) for name in rows))


def github_prs(raw):
    failed = note(os.path.join(raw, "repos.hold")) or note(os.path.join(raw, "repos.error"))
    try:
        with open(os.path.join(raw, "repos.json")) as f:
            snap = json.load(f)
        known = {f"{row['repo']}#{row['number']}" for row in snap["prs"] + snap["recent"]}
        reason = snap.get("stale_reason") or snap.get("error")
        return known, snap.get("fetched"), failed or (str(reason)[:ERROR_CHARS] if reason else None), inventory_complete(snap)
    except FileNotFoundError:
        return None, None, failed, False
    except (OSError, ValueError, KeyError, TypeError):
        return None, None, failed or "repos.json unreadable", False


def github_state(known, at, error, generated, complete=True):
    if error:
        return "failed"
    if known is None:
        return "missing"
    if type(at) not in (int, float) or not math.isfinite(at) or at <= 0:
        return "unreadable"
    if at > generated + CLOCK_SKEW:
        return "future"
    if generated - at > GITHUB_SECONDS:
        return "stale"
    return "fresh" if complete else "partial"


def github_coverage(known, at, error, generated, prs, cut, complete=True):
    state = github_state(known, at, error, generated, complete)
    usable = state == "fresh"
    return {"at": at if type(at) in (int, float) and math.isfinite(at) else None, "state": state, "error": error,
            "prs": len(known) if usable else None, "attributed": len((known & prs) - cut) if usable else None,
            "unknown": len(known - prs - cut) if usable else None, "unresolved": len(known & cut) if usable else None}


def publication(tokens, threads, projects_complete, known_prs=None, github_at=None, github_error=None, github_complete=True):
    counts = {"total": 0, "attributed": 0, "no_pr": 0, "branch_only": 0, "project_unknown": 0, "out_of_scope": 0,
              "usage_unknown": 0, "hint_refused": 0}
    runs, entries = {}, {}
    cut = cut_prs(tokens.get("unexported"), threads)
    for run in tokens["runs"]:
        rid = run.get("run")
        if not isinstance(rid, str) or not rid:
            continue
        counts["total"] += 1
        project = threads.get(run.get("thread"))
        if project is None:
            counts["project_unknown"] += 1
            cut |= possible_keys(run.get("title"), run.get("links"), False, None, run.get("harness") or "")
            continue
        if project == FOREIGN:
            counts["out_of_scope"] += 1
            cut |= link_keys(run.get("links"))
            continue
        hint, refused = review_hint(run.get("title"), project, run.get("harness"))
        counts["hint_refused"] += refused
        links = link_keys(run.get("links"))
        keys = links | ({hint["pr"]} if hint else set())
        if not keys:
            counts["branch_only" if branch_only(run.get("links")) else "no_pr"] += 1
            continue
        counts["attributed"] += 1
        runs[rid] = published_run(run, project, allocation_of(keys, links))
        counts["usage_unknown"] += runs[rid]["tokens"] is None
        for key in keys:
            reviewed = hint is not None and hint["pr"] == key
            entries.setdefault(key, []).append({"run": rid, "role": "review" if reviewed else "linked",
                                                "pr_via": "orch-link" if key in links else "title-hint", "role_via": "title-hint" if reviewed else None,
                                                "head": hint["head"] if reviewed else None, "family_hint": hint["family"] if reviewed else None})
    prs = {}
    for key, rows in entries.items():
        rows.sort(key=lambda row: (runs[row["run"]]["started"] or "", row["run"]))
        usable = [runs[row["run"]]["tokens"] if runs[row["run"]]["allocation"] == "orch-link" else None for row in rows]
        total = sum_usage(usable) if key not in cut else None
        prs[key] = {"schema": RECORD_SCHEMA, "built": None, "built_by": None, "built_confidence": "unknown", "built_family": None,
                    "reworks": None, "tokens": total, "tokens_basis": LINK_ESTIMATE if total else None, "tokens_complete": False, "history_complete": key not in cut,
                    "history_start": tokens.get("history_start"), "shared_runs": sum(runs[row["run"]]["allocation"] == "shared" for row in rows),
                    "run_refs": rows}
    reviewed = sum(any(row["role"] == "review" for row in record["run_refs"]) for record in prs.values())
    github = github_coverage(known_prs, github_at, github_error, tokens["generated"], prs.keys(), cut, github_complete)
    coverage = {"prs_attributed": len(prs), "prs_review_hint": reviewed, "prs_link_only": len(prs) - reviewed, "prs_builder_known": 0,
                "prs_unresolved": len(cut), "runs": counts, "github": github, "projects_complete": projects_complete}
    return {"schema": SCHEMA, "generated": tokens["generated"], "source": TOKENS_SCHEMA, "history_start": tokens.get("history_start"),
            "window_start": tokens.get("window_start"), "fields": FIELDS, "unknown": UNKNOWN, "coverage": coverage, "runs": runs, "prs": prs}


def collect(data_dir, opener=orch_api.open_local, base=orch_api.ORCH_URL, token_file=orch_api.TOKEN_FILE, tokens=agent_tokens):
    threads, complete = shell_projects(base, orch_api.read_token(token_file), opener)
    report = tokens(os.path.join(data_dir, "cache", "agent-tokens-3.json"))
    return publication(report, threads, complete, *github_prs(os.path.join(data_dir, "raw")))


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        out = collect(data_dir)
    except Failure as failure:
        out = {"schema": SCHEMA, "error": str(failure)}
    json.dump(out, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
