#!/usr/bin/env python3
import datetime
import hashlib
import json
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

REPOSITORIES = {k: f"{settings.OWNER}/{v}".lower() for k, v in settings.REPOS.items()}
REPO_KEYS = {v: k for k, v in REPOSITORIES.items()}
SHA = re.compile(r"[0-9a-f]{40}")
BRIEF_REPO = re.compile(r"^Repo: ([^\s/]+/[^\s/]+)", re.M)
BRIEF_PR = re.compile(r"^PR: #([1-9][0-9]*)$", re.M)
BRIEF_HEAD = re.compile(r"^Expected head SHA \(full\): ([0-9a-f]{40})$", re.M)
REPORT = re.compile(r"\ARepo: ([^\s/]+/[^\s/]+)\nPR: #([1-9][0-9]*)\nHead SHA: ([0-9a-f]{40})\nReviewer family: (Claude|Codex)\nVerdict: (APPROVE|REQUEST_CHANGES|BLOCKED)\n")
BRIEF_END = "Setup, in this order."
FAMILY_HARNESS = {"Claude": "claude", "Codex": "codex"}
ORCH_SCHEMA = "orch-orchestration/2"
STAND_IN_SCHEMA = "flightops-stand-in/1"
STAND_IN_MARK = "flightopsStandIn"
LEDGER_SOURCE = "evidence-ledger"
LEDGER_SCHEMA = "flightops-evidence-ledger/2"
LEDGER_PRS = 300
LEDGER_HEADS = 6
LEDGER_ROWS = 60
LEDGER_BYTES = 24 * 1024 * 1024
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}

CLAIM_REASONS = {
    "anchored_size": "fixed_base_diff_not_collected",
    "actual_setup": "requested_settings_only",
    "exact_producer": "git_creation_witness_absent",
    "candidate_inventory_complete": "rework_inventory_witness_absent",
    "assignment_complete": "assignment_witness_absent",
    "resolution_complete": "resolution_witness_absent",
}


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def instant(text):
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def pr_id(repo, number):
    return f"{repo}#{number}"


def once(pattern, text):
    found = pattern.findall(text)
    return found[0] if len(found) == 1 else None


def only(rows):
    return rows[0] if len(rows) == 1 else None


def requested_settings(run):
    selection = run.get("modelSelection") or {}
    options = selection.get("options") or []
    effort = {o.get("id"): o.get("value") for o in options if isinstance(o, dict)}.get("effort")
    return {"harness": selection.get("instanceId"), "model": selection.get("model"), "effort": effort, "tier": None}


def turn_of(projection, run, attempt, message):
    item = only([i for i in projection.get("turnItems") or [] if i.get("type") == "assistant_message" and i.get("messageId") == message.get("id")])
    if item is None or item.get("runId") != run["id"] or item.get("status") != "completed" or not item.get("providerTurnId"):
        return None
    return only([t for t in projection.get("providerTurns") or [] if t.get("id") == item["providerTurnId"] and t.get("runAttemptId") == attempt["id"] and t.get("status") == "completed"])


def verdict_of(snapshot, thread_id):
    if not isinstance(snapshot, dict) or snapshot.get("hasMoreHistory") is True:
        return None, "history_partial"
    projection = snapshot.get("projection") or {}
    thread = projection.get("thread") or {}
    if thread.get("id") != thread_id:
        return None, "wrong_thread"
    run = only(projection.get("runs") or [])
    if run is None or run.get("threadId") != thread_id or run.get("status") != "completed":
        return None, "not_one_completed_run"
    attempt = only([a for a in projection.get("attempts") or [] if a.get("runId") == run.get("id")])
    if attempt is None or attempt.get("id") != run.get("activeAttemptId") or attempt.get("status") != "completed":
        return None, "not_one_completed_attempt"
    messages = [m for m in projection.get("messages") or [] if m.get("runId") == run["id"] and m.get("streaming") is False]
    brief = only([m for m in messages if m.get("role") == "user"])
    if brief is None or brief.get("id") != run.get("userMessageId") or not isinstance(brief.get("text"), str):
        return None, "brief_not_bound"
    said = [m for m in messages if m.get("role") == "assistant" and isinstance(m.get("text"), str)]
    if not said:
        return None, "no_report"
    last = max(said, key=lambda m: (m.get("createdAt") or "", m.get("id") or ""))
    report = REPORT.match(last["text"])
    if report is None:
        return None, "report_format"
    repo, number, head, family, verdict = report.groups()
    repo = repo.lower()
    header, marker, _ = brief["text"].partition(BRIEF_END)
    if not marker:
        return None, "brief_format"
    assigned = once(BRIEF_REPO, header), once(BRIEF_PR, header), once(BRIEF_HEAD, header)
    if assigned != (report.group(1), number, head):
        return None, "report_disagrees_with_brief"
    if repo not in REPOSITORIES.values():
        return None, "repository_out_of_scope"
    settings = requested_settings(run)
    if not (settings["harness"] or "").lower().startswith(FAMILY_HARNESS[family]):
        return None, "family_disagrees_with_harness"
    turn = turn_of(projection, run, attempt, last)
    if turn is None:
        return None, "turn_not_bound"
    return {"pr": pr_id(repo, int(number)), "head": head, "verdict": verdict, "family": family, "thread": thread_id, "run": run["id"],
            "attempt": attempt["id"], "turn": turn["id"],
            "message": last.get("id"), "completed_at": run.get("completedAt"), "requested": settings}, None


def exact_links(rows):
    links, refused = {}, 0
    for row in rows:
        for link in row.get("pullRequests") or []:
            repo = (link.get("repository") or "").lower()
            number = link.get("number")
            url = f"https://github.com/{repo}/pull/{number}"
            if link.get("host") != "github.com" or repo not in REPOSITORIES.values() or type(number) is not int or (link.get("url") or "").lower() != url:
                refused += 1
                continue
            snapshot = link.get("snapshot") or {}
            links.setdefault(pr_id(repo, number), []).append({"thread": row.get("id"), "source": link.get("source"), "linked_at": link.get("linkedAt"),
                                                               "state": snapshot.get("state"), "merged_at": snapshot.get("mergedAt")})
    owned = {}
    for key, rows_for in links.items():
        for link in rows_for:
            owned.setdefault(link["thread"], set()).add(key)
    return links, {thread: len(keys) for thread, keys in owned.items()}, refused


def sighted(ledger, repos):
    fetched = repos["fetched"]
    for row in repos.get("prs") or []:
        head = row.get("head")
        repo = REPOSITORIES.get(row.get("repo"))
        if repo is None or not isinstance(head, str) or SHA.fullmatch(head) is None or type(row.get("number")) is not int:
            continue
        entry = ledger.setdefault(pr_id(repo, row["number"]), {})
        seen = entry.setdefault(head, {"first_seen": fetched, "last_seen": fetched, "ci": None, "check_runs": {}, "complete": False})
        seen["last_seen"] = max(seen["last_seen"], fetched)
        seen["ci"] = row.get("ci")
        seen["complete"] = row.get("check_runs_complete") is True and "compacted" not in seen
        for check in row.get("check_runs") or []:
            seen["check_runs"][f"{check.get('kind')}:{check.get('name')}:{check.get('id')}:{check.get('started')}"] = check
        for extra in sorted(seen["check_runs"])[:-LEDGER_ROWS]:
            del seen["check_runs"][extra]
            seen.update(complete=False, compacted=True)
        for old in sorted(entry, key=lambda h: entry[h]["last_seen"])[:-LEDGER_HEADS]:
            del entry[old]
    latest = lambda key: max(h["last_seen"] for h in ledger[key].values())
    for old in sorted(ledger, key=latest)[:-LEDGER_PRS]:
        del ledger[old]
    return ledger


def ledger_shape(ledger):
    return isinstance(ledger, dict) and all(isinstance(heads, dict) and heads and all(
        isinstance(seen, dict) and type(seen.get("first_seen")) in (int, float) and type(seen.get("last_seen")) in (int, float) and isinstance(seen.get("check_runs"), dict)
        for seen in heads.values()) for heads in ledger.values())


def compacted(ledger):
    sizes = {key: len(canonical_text(heads)) for key, heads in ledger.items()}
    total = sum(sizes.values())
    for key in sorted(ledger, key=lambda k: (max(h["last_seen"] for h in ledger[k].values()), k)):
        if total <= LEDGER_BYTES:
            break
        for seen in ledger[key].values():
            seen.update(check_runs={}, complete=False, compacted=True)
        total -= sizes[key] - len(canonical_text(ledger[key]))
    return ledger


def compaction_gaps(ledger):
    heads = sum("compacted" in seen for entry in ledger.values() for seen in entry.values())
    return [f"{heads} sighted heads lost check rows to compaction"] if heads else []


def cohort_rows(repos):
    rows = {}
    for row in repos.get("merged") or repos.get("recent") or []:
        repo = REPOSITORIES.get(row.get("repo"))
        if repo and type(row.get("number")) is int:
            head = row.get("head")
            rows[pr_id(repo, row["number"])] = {"repo": repo, "number": row["number"], "state": "merged", "head": head if isinstance(head, str) and SHA.fullmatch(head) else None,
                                                "merged_at": row.get("merged_at"), "opened_at": row.get("created")}
    for row in repos.get("prs") or []:
        repo = REPOSITORIES.get(row.get("repo"))
        if repo and type(row.get("number")) is int:
            head = row.get("head")
            rows.setdefault(pr_id(repo, row["number"]), {"repo": repo, "number": row["number"], "state": "open", "head": head if isinstance(head, str) and SHA.fullmatch(head) else None,
                                                         "merged_at": None, "opened_at": row.get("created")})
    return rows


def last_sighted(ledger, key):
    seen = ledger.get(key) or {}
    return max(seen, key=lambda head: seen[head]["last_seen"]) if seen else None


def head_state(github, sighted_head, verdicts):
    heads = {v["head"] for v in verdicts}
    if github:
        if github in heads:
            return "exact", github
        return ("verdicts_on_other_heads" if heads else "github_only"), github
    if sighted_head:
        if sighted_head in heads:
            return "exact_at_last_sighting", sighted_head
        return ("verdicts_on_other_heads" if heads else "github_only"), sighted_head
    if len(heads) == 1:
        return "verdict_only", next(iter(heads))
    return ("ambiguous" if heads else "unknown"), None


def ci_at(ledger, key, head):
    seen = (ledger.get(key) or {}).get(head) if head else None
    if seen is None:
        return {"state": "unknown", "reason": "head_not_sighted_open"}
    checks = list(seen["check_runs"].values())
    failed = sum(c.get("conclusion") in FAILED or c.get("status") in FAILED for c in checks)
    done = sum(c.get("status") == "COMPLETED" for c in checks)
    return {"state": "observed", "reason": "first_attempt_unknown", "ci": seen.get("ci"), "checks": len(checks), "completed": done, "failed": failed,
            "check_list_complete": seen["complete"], "checks_compacted": "compacted" in seen, "first_seen": seen["first_seen"], "last_seen": seen["last_seen"]}


def fresh_tokens(tokens):
    return tokens["input"] + tokens["cache_write"] + tokens["output"]


def usage_of(threads, runs):
    rows = [r for r in runs.values() if r.get("thread") in threads]
    known = [r["tokens"] for r in rows if isinstance(r.get("tokens"), dict)]
    return {"runs": len(rows), "runs_with_usage": len(known), "fresh_tokens_on_linked_threads": sum(map(fresh_tokens, known)) if rows and len(known) == len(rows) else None}


def review_usage_of(verdicts, runs):
    named = {v["run"]: v["thread"] for v in verdicts}
    known = [runs[run]["tokens"] for run, thread in named.items() if run in runs and runs[run].get("thread") == thread and isinstance(runs[run].get("tokens"), dict)]
    return {"runs": len(named), "runs_with_usage": len(known), "fresh_tokens_on_review_runs": sum(map(fresh_tokens, known)) if named and len(known) == len(named) else None}


def ledger_key(key):
    full, number = key.rsplit("#", 1)
    return REPO_KEYS[full.lower()], int(number)


def ledger_pr(repo, subject):
    return pr_id(REPOSITORIES.get(repo, repo), int(subject[1:]))


def witness_head(row, field):
    candidates = row[f"{field}_candidates"]
    return {"sha": row[field], "status": row[f"{field}_status"], "basis": row[f"{field}_basis"], "event": row[f"{field}_event"], "candidates": json.loads(candidates) if candidates else []}


def witness_of(row, gaps):
    return {"source": LEDGER_SOURCE, "binding": row["binding"], "head_at_open": witness_head(row, "head_at_open"), "first_ready_at": {"value": row["first_ready_at"], "basis": row["first_ready_at_basis"]},
            "first_ready_head": witness_head(row, "first_ready_head"), "final_head_check": row["final_head_check"], "gaps": sorted(gaps)}


def ledger_coverage(witnesses, total, open_gaps, at, begin):
    tally = lambda field: sum(w[field]["status"] != "exact" for w in witnesses.values())
    gaps = ["no source-specific rule qualifies this evidence", f"{total - len(witnesses)} of {total} PRs have no row", f"head at open is not exact on {tally('head_at_open')} rows", f"first-ready head is not exact on {tally('first_ready_head')} rows"]
    gaps += [f"{count} open {source} {kind} gaps" for source, kind, count in open_gaps]
    return {"digest": digest({"witnesses": witnesses, "gaps": gaps}), "at": at, "begin": begin, "gaps": gaps}


def ready_value(record):
    value = ((record["witness"] or {}).get("first_ready_at") or {}).get("value")
    return value if isinstance(value, str) and value.endswith("Z") and instant(value) is not None else None


def join(cohort, links, link_counts, verdicts, ledger, runs, ledger_rows=None):
    records = []
    for key, row in sorted(cohort.items()):
        bound = verdicts.get(key, [])
        linked = links.get(key, [])
        threads = {link["thread"] for link in linked}
        state, head = head_state(row["head"], last_sighted(ledger, key), bound)
        widest = max((link_counts.get(thread, 0) for thread in threads), default=0)
        records.append(row | {"pr": key, "links": linked, "verdicts": bound, "head_state": state, "exact_head": head, "github_head": row["head"],
                              "ci": ci_at(ledger, key, head), "usage": usage_of(threads, runs), "review_usage": review_usage_of(bound, runs), "thread_prs": widest, "witness": (ledger_rows or {}).get(key),
                              "merged_agrees": any(link["state"] == "merged" and instant(link["merged_at"]) is not None and instant(link["merged_at"]) == instant(row["merged_at"]) for link in linked)})
    return records


def ledger_reason(claim, record):
    witness = record["witness"]
    if claim == "first_ready":
        if witness is None:
            return "draft_history_not_collected"
        return "no_source_rule_for_first_ready" if ready_value(record) else "first_ready_" + witness["first_ready_at"]["basis"].replace("-", "_")
    if witness is None:
        return "first_ready_head_unknown"
    status = witness["first_ready_head"]["status"]
    return "no_source_rule_for_anchor" if status == "exact" else "first_ready_head_" + status.replace("-", "_")


def reason_for(claim, record):
    if claim in ("first_ready", "verified_anchor"):
        return ledger_reason(claim, record)
    if claim == "original_attempt_complete":
        return record["ci"]["reason"]
    if claim == "usage_complete":
        return "children_unknown" if record["usage"]["fresh_tokens_on_linked_threads"] is not None else "usage_missing"
    if claim == "usage_exclusive":
        return "thread_links_several_prs" if record["thread_prs"] > 1 else "allocation_unknown"
    return CLAIM_REASONS[claim]


def proof(record, claim, as_of):
    head = record["exact_head"]
    source = LEDGER_SOURCE if record["witness"] and claim in ("first_ready", "verified_anchor") else "orch"
    return {"id": f"{record['pr']}:{claim}", "schema": "flightops-pr-proof/1", "source_id": source, "claim": claim, "pr_id": record["pr"], "anchor_sha": head,
            "digest": digest([record["pr"], head, claim, {}]), "qualified": False, "effective_at": as_of, "recorded_at": as_of, "unqualified_reason": reason_for(claim, record)}


def setting(value, basis):
    return {"value": value, "basis": basis, "evidence_ids": []}


def setup(ident, harness, model, effort):
    return {"id": ident, "provider": setting(harness, "requested"), "model": setting(model, "requested"), "effort": setting(effort, "requested"), "tier": setting(None, "unknown")}


SETUPS = [setup("claude-sonnet-5-5/medium", "claudeAgent", "claude-sonnet-5-5", "medium"), setup("claude-opus-5-5/medium", "claudeAgent", "claude-opus-5-5", "medium")]
TASK = "unclassified"
SIZES = ("xs", "s", "m", "l", "xl")
MIX = [{"repo": repo, "task": TASK, "size": size, "weight": 1 / (len(REPOSITORIES) * len(SIZES))} for repo in REPOSITORIES.values() for size in SIZES]
POLICY = {"version": "joint-strata-max-bootstrap/2", "seed": 139, "bootstrap_replicates": 199, "calibration_simulations": 120,
          "family": [s["id"] for s in SETUPS], "reference_setup": SETUPS[0]["id"], "reference_mix": MIX}
MEASURES = [
    {"id": "first_try_ci", "kind": "rate", "direction": "higher", "semantics": "first_try_ci", "unit": "share of PRs",
     "required_claims": ["verified_anchor", "original_attempt_complete", "exact_producer"]},
    {"id": "rework", "kind": "rate", "direction": "lower", "semantics": "rework", "unit": "share of PRs",
     "required_claims": ["candidate_inventory_complete", "assignment_complete", "resolution_complete", "exact_producer"]},
    {"id": "fresh_tokens", "kind": "amount", "direction": "lower", "semantics": "usage", "unit": "fresh tokens per PR",
     "required_claims": ["usage_complete", "usage_exclusive", "exact_producer"]},
]
MEMBER_CLAIMS = ("first_ready", "actual_setup", "verified_anchor", "anchored_size")
MEASURE_REASONS = {"first_try_ci": lambda r: r["ci"]["reason"], "rework": lambda r: "rework_witness_absent",
                   "fresh_tokens": lambda r: reason_for("usage_exclusive", r)}


def observation(record, as_of, github_at):
    claims = sorted(set(MEMBER_CLAIMS) | {c for m in MEASURES for c in m["required_claims"]})
    proofs = [proof(record, claim, as_of) for claim in claims] if record["exact_head"] else []
    ids = {p["claim"]: p["id"] for p in proofs}
    pick = lambda *names: [ids[n] for n in names if n in ids]
    threads = sorted({link["thread"] for link in record["links"]} | {v["thread"] for v in record["verdicts"]})
    keys = [f"cluster:thread:{thread}" for thread in threads] or [f"stack:pr:{record['pr']}"]
    measures = {m["id"]: {"value": None, "effective_at": as_of, "evidence_ids": pick(*m["required_claims"]), "reason": MEASURE_REASONS[m["id"]](record)} for m in MEASURES}
    row = {"pr_id": record["pr"], "repo": record["repo"], "number": record["number"], "revision": digest(record)[:32], "observed_at": github_at,
           "first_ready_at": ready_value(record), "membership_evidence_ids": pick("first_ready"), "setup_id": None, "possible_setup_ids": [],
           "setup_evidence_ids": pick("actual_setup"), "task": TASK, "mature_at": record["merged_at"],
           "anchor": {"kind": "unknown", "r0_sha": None, "base_sha": None, "evidence_ids": pick("verified_anchor")},
           "anchored_size": None, "shared": record["thread_prs"] > 1, "independence_keys": keys, "measures": measures}
    return row, proofs


def normalized(records, sources, inventory, as_of, frozen_at, github_at):
    observations, evidence = [], []
    for record in records:
        row, proofs = observation(record, as_of, github_at)
        observations.append(row)
        evidence += proofs
    return {"schema": "flightops-normalized-pr/1", "as_of": as_of, "frozen_at": frozen_at, "snapshot_id": "best-" + as_of, "previous_manifest_hash": None,
            "policy": POLICY, "inventory": inventory, "sources": sources, "setups": SETUPS, "measures": MEASURES, "evidence": evidence, "observations": observations}


UNKNOWN = ["provider-reported model, effort and tier", "subagents and children", "token allocation between PRs", "failed-before-thread dispatches",
           "retention and eligible population", "assignment", "rework resolution", "Git creation and publication"]
STAGE_LIMIT = 5


def coverage_gaps(repos, window_start, drafts_known=False):
    gaps = [] if drafts_known else ["draft history not collected"]
    rows = repos.get("merged")
    if rows is None:
        gaps.append("merged heads not in this collection")
    if not all(r.get("merged_complete") is True for r in repos.get("repos") or [{}]):
        gaps.append("merged list is cut per repository")
    opened = [instant(r.get("created")) for r in (rows or repos.get("recent") or []) + (repos.get("prs") or [])]
    earliest = min((t for t in opened if t is not None), default=None)
    if earliest is None or earliest > window_start:
        gaps.append("history starts inside the 90-day window")
    return gaps, earliest


def source_rows(repos, repos_bytes, models, models_bytes, orch_schema, orch_digest, orch_gaps, orch_at, now, github_extra=(), coverage=None):
    start = now - 90 * 86400
    github_gaps, earliest = coverage_gaps(repos, start, coverage is not None)
    github_gaps += github_extra
    fetched = repos["fetched"]
    orch_start = models.get("history_start") or now
    row = lambda ident, schema, content, at, begin, gaps: {"id": ident, "schema": schema, "digest": content, "snapshot_at": stamp(at), "history_start": stamp(min(begin, at)),
                                                            "complete_through": stamp(at), "complete": not gaps, "gaps": gaps}
    witnessed = [row(LEDGER_SOURCE, LEDGER_SCHEMA, coverage["digest"], coverage["at"], coverage["begin"], coverage["gaps"])] if coverage else []
    return [row("github", "flightops-repos/merged+open", hashlib.sha256(repos_bytes).hexdigest(), fetched, earliest or fetched, github_gaps),
            row("orch", orch_schema, orch_digest, orch_at, orch_start, ["retention and eligible population unknown"] + orch_gaps),
            row("usage", models.get("schema") or "flightops-models/1", hashlib.sha256(models_bytes).hexdigest(), models["generated"], orch_start,
                ["children and allocation unknown"])] + witnessed


def inventory_of(sources):
    wanted = [s for s in sources if s["id"] in ("github", "orch")]
    return {"status": "complete" if all(s["complete"] for s in wanted) else "partial", "source_ids": [s["id"] for s in wanted]}


def ci_words(record):
    ci = record["ci"]
    if ci["state"] != "observed":
        return "no check seen on this head while the PR was open"
    kept = "older check rows were compacted away; " if ci["checks_compacted"] else ""
    return f"{ci['checks']} checks seen on this head, {ci['failed']} failed, last CI state {ci['ci']}; {kept}first attempt unknown"


def stage(name, state, detail):
    return {"name": name, "state": state, "detail": detail}


def witness_words(head):
    if head["sha"]:
        return f"{head['sha']} ({head['status']}, Activity event {head['event']})"
    choices = f", {len(head['candidates'])} candidates" if head["candidates"] else ""
    return f"{head['status']}: {head['basis']}{choices}"


def witness_state(status):
    return "exact" if status == "exact" else "unknown"


def ledger_stages(record):
    witness = record["witness"]
    if witness is None:
        return [stage("Head at open", "unknown", "no evidence-ledger row for this PR"), stage("First ready", "unknown", "no evidence-ledger row for this PR")]
    gaps = f"; open gaps: {', '.join(witness['gaps'])}" if witness["gaps"] else ""
    head, ready = witness["head_at_open"], witness["first_ready_at"]
    value = ready_value(record)
    words = f"{value} ({ready['basis']})" if value else f"not known: {ready['basis']}"
    return [stage("Head at open", witness_state(head["status"]), f"evidence ledger, binding {witness['binding']}: {witness_words(head)}{gaps}; unqualified"),
            stage("First ready", "observed" if value else "unknown", f"evidence ledger: {words}; head {witness_words(witness['first_ready_head'])}{gaps}; unqualified")]


def trace_of(record, row):
    best = next((v for v in record["verdicts"] if v["head"] == record["exact_head"]), record["verdicts"][0] if record["verdicts"] else None)
    reviewed = f", reviewed head {best['head']}" if best and best["head"] != record["exact_head"] else ""
    created = [link for link in record["links"] if link["source"] == "created"]
    origin = (f"Orch link ({created[0]['source']}) in {created[0]['thread']}" if created else f"Orch link in {record['links'][0]['thread']}") if record["links"] else "no Orch link"
    merged = "; Orch and GitHub agree on the merge time" if record["merged_agrees"] else ""
    head_words = {"exact": "GitHub head equals a review verdict head", "exact_at_last_sighting": "GitHub's last open-PR head equals a review verdict head; the merged head itself is not collected yet", "verdict_only": "head from a review verdict; GitHub head not collected", "github_only": "GitHub head; no review verdict bound",
                  "verdicts_on_other_heads": "GitHub head differs from every review verdict head", "ambiguous": "several verdict heads and no GitHub head", "unknown": "no head record"}
    usage = record["usage"]
    fresh = usage["fresh_tokens_on_linked_threads"]
    review_fresh = record["review_usage"]["fresh_tokens_on_review_runs"]
    usage_words = f"{usage['runs_with_usage']} of {usage['runs']} runs on linked threads have usage" + (f", ~{fresh} fresh tokens on those threads" if fresh is not None else "")
    usage_words += f"; the named review runs used {review_fresh} fresh tokens" if review_fresh is not None else "; usage of the named review runs is unknown" if record["verdicts"] else ""
    setup_words = (f"review run requested {best['requested']['model']} {best['requested']['effort']}; reported settings and builder unknown" if best else "requested settings only; reported settings and builder unknown")
    shared = f"thread links {record['thread_prs']} PRs" if record["thread_prs"] > 1 else "exclusive use unproven"
    reasons = {mid: fact["reasons"] for mid, fact in row["measures"].items()}
    return {"pr": record["pr"], "state": record["state"], "merged_at": record["merged_at"], "opened_at": record["opened_at"], "head": record["exact_head"], "reasons": reasons,
            "stages": [stage("PR", "exact" if record["links"] or record["verdicts"] else "unknown", origin + "; GitHub row" + merged),
                       stage("Run", "exact" if best else "unknown", f"{best['verdict']} by {best['family']}: thread {best['thread']}, run {best['run']}{reviewed}, attempt {best['attempt']}, turn {best['turn']}, joined by Orch's turn item for report {best['message']}" if best else "no review record names this PR, head, run and turn"),
                       stage("Usage", "observed" if usage["runs"] or review_fresh is not None else "unknown", usage_words + f"; {shared}"),
                       stage("Head", "exact" if record["head_state"] in ("exact", "exact_at_last_sighting") else "observed" if record["exact_head"] else "unknown", head_words[record["head_state"]] + (f": {record['exact_head']}" if record["exact_head"] else "")),
                       *ledger_stages(record),
                       stage("CI", record["ci"]["state"], ci_words(record)),
                       stage("Setup", "unknown", setup_words)]}


def chosen(records):
    rank = {"exact": 0, "exact_at_last_sighting": 1, "verdict_only": 2, "verdicts_on_other_heads": 3, "github_only": 4, "ambiguous": 5, "unknown": 6}
    order = sorted(records, key=lambda r: (r["state"] != "merged", rank[r["head_state"]], r["ci"]["state"] != "observed", r["pr"]))
    return order[:STAGE_LIMIT]


def presentation(decision, manifest, records, counts):
    rows = {row["pr_id"]: row for row in manifest["rows"]}
    setups = {ident: {"label": f"{raw['model']['value']} · {raw['effort']['value']}", "exact": raw["exact"],
                      "settings": {dim: {"value": raw[dim]["value"], "basis": raw[dim]["basis"]} for dim in ("provider", "model", "effort", "tier")}}
              for ident, raw in manifest["setups"].items()}
    body = {"schema": PRESENTATION_SCHEMA, "decision_hash": decision["decision_hash"], "manifest_hash": decision["manifest_hash"], "input_hash": decision["input_hash"],
            "as_of": decision["as_of"], "snapshot_id": manifest["snapshot_id"], "family": {"status": "diagnostic", "setups": manifest["policy"]["family"], "note": DIAGNOSTIC}, "setups": setups, "measures": {m["id"]: LABELS[m["id"]] for m in MEASURES},
            "counts": counts, "sources": [{key: s[key] for key in ("id", "schema", "snapshot_at", "history_start", "complete", "gaps")} for s in manifest["sources"]],
            "unknown": UNKNOWN, "traces": [trace_of(r, rows[r["pr"]]) for r in chosen(records) if r["pr"] in rows]}
    return body | {"presentation_hash": digest(body)}


PRESENTATION_SCHEMA = "flightops-best-presentation/1"
DIAGNOSTIC = "Diagnostic family: two Claude setups that prove the path. This is not the production comparison."
PUBLICATION_SCHEMA = "flightops-best/1"
LABELS = {"first_try_ci": "First-try CI", "rework": "Rework", "fresh_tokens": "Tokens"}


def canonical_text(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def publication(decision, manifest, shown, generated):
    return {"schema": PUBLICATION_SCHEMA, "generated": generated, "decision": canonical_text(decision), "manifest": canonical_text(manifest), "presentation": canonical_text(shown)}
