#!/usr/bin/env python3
import json
import os
import resource
import sqlite3
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import best_evidence as evidence
import ledger_store
import models
import orch_api
from model_analysis import ContractError, build_manifest, decide
from model_analysis.contract import digest as frozen_digest

READS = 60
READ_SECONDS = 40
RETAINED_FREEZES = 16
MODELS_LIMIT = 3600
FINISHED = ("idle", "completed", "failed", "cancelled", "interrupted")
MIB = 1024 * 1024
MEMORY_BYTES = 290 * MIB
SOURCE_BYTES = 32 * MIB
THREAD_BYTES = 16 * MIB
HELD_FILES = 3


def within_budget(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size > SOURCE_BYTES:
        raise models.Failure(f"{os.path.basename(path)} exceeds {SOURCE_BYTES // MIB} MiB")


def held_files(path):
    prefix = os.path.basename(path) + ".held-"
    return sorted((entry.name, entry.stat().st_size) for entry in os.scandir(os.path.dirname(path)) if entry.name.startswith(prefix))


def read_cache(path, valid, now):
    try:
        if os.path.getsize(path) <= SOURCE_BYTES:
            with open(path, "rb") as f:
                value = json.loads(f.read())
            if valid(value):
                return value, True
    except FileNotFoundError:
        return {}, True
    except OSError:
        raise models.Failure(os.path.basename(path) + " unreadable") from None
    except (ValueError, RecursionError, MemoryError):
        pass
    held = held_files(path)
    if len(held) >= HELD_FILES:
        return {}, False
    os.replace(path, f"{path}.held-{now}-{len(held)}")
    return {}, True


def cache_gaps(path, writable):
    gaps = [f"{name} held aside, {size} bytes, earlier rows are not in this read" for name, size in held_files(path)]
    return gaps + ([] if writable else [f"{os.path.basename(path)} is unusable and {HELD_FILES} files are already held, so this pass saves none"])


def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w") as f:
        json.dump(value, f, separators=(",", ":"))
    os.replace(temporary, path)


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        raise models.Failure(os.path.basename(path) + " not collected yet") from None


def github(raw, now):
    within_budget(os.path.join(raw, "repos.json"))
    known, at, error, complete = models.github_prs(raw)
    state = models.github_state(known, at, error, now, complete)
    if state != "fresh":
        raise models.Failure("GitHub collection is " + state)
    data = read_bytes(os.path.join(raw, "repos.json"))
    return json.loads(data), data


def usage(raw, now):
    failed = models.note(os.path.join(raw, "models.error"))
    if failed:
        raise models.Failure("Models collection failed: " + failed)
    within_budget(os.path.join(raw, "models.json"))
    data = read_bytes(os.path.join(raw, "models.json"))
    try:
        report = json.loads(data)
        generated, runs = report["generated"], report["runs"]
    except (ValueError, KeyError, TypeError):
        raise models.Failure("Models report unreadable") from None
    if report.get("schema") != models.SCHEMA:
        raise models.Failure("Models report unreadable")
    if type(generated) not in (int, float) or not 0 <= now - generated <= MODELS_LIMIT or not isinstance(runs, dict):
        raise models.Failure("Models report is stale")
    return report, data


class LiveOrch:
    def __init__(self, base=orch_api.ORCH_URL, token_file=orch_api.TOKEN_FILE):
        self.base, self.token_file = base, token_file

    def shell(self):
        if not orch_api.LOOPBACK.fullmatch(self.base):
            raise models.Failure("Orch address is not local")
        self.token = orch_api.read_token(self.token_file)
        if self.token is None:
            raise models.Failure("Orch read token unavailable")
        shell = orch_api.get(self.base, self.token, orch_api.SHELL, orch_api.open_local, SOURCE_BYTES)
        if evidence.STAND_IN_MARK in shell:
            raise models.Failure("Orch answered with a stand-in recording")
        return shell

    def thread(self, thread_id):
        return orch_api.get(self.base, self.token, f"{orch_api.THREADS}/{urllib.parse.quote(thread_id, safe=':')}", orch_api.open_local, THREAD_BYTES)


def ledger_tables(db, cohort):
    rows = {}
    for key in cohort:
        repo, number = evidence.ledger_key(key)
        row = db.execute("SELECT * FROM pr_witness WHERE repo = ? AND number = ?", (repo, number)).fetchone()
        if row is not None:
            rows[key] = dict(row)
    pr_gaps = {}
    for repo, subject, kind in db.execute("SELECT repo, subject, kind FROM gaps WHERE closed_pass IS NULL AND source = 'prs' AND subject LIKE '#%'"):
        pr_gaps.setdefault(evidence.ledger_pr(repo, subject), []).append(kind)
    open_gaps = [tuple(row) for row in db.execute("SELECT source, kind, count(*) FROM gaps WHERE closed_pass IS NULL AND source IN ('activity', 'prs') GROUP BY 1, 2 ORDER BY 1, 2")]
    return rows, pr_gaps, open_gaps, tuple(db.execute("SELECT min(started_at), max(finished_at) FROM passes WHERE finished_at IS NOT NULL").fetchone())


def read_ledger(data_dir, cohort, now):
    if not os.path.exists(os.path.join(data_dir, ledger_store.FILE)):
        return {}, None, []
    try:
        store = ledger_store.Store(data_dir, readonly=True)
    except ledger_store.Refused:
        return {}, None, ["evidence ledger is another schema"]
    except sqlite3.Error:
        return {}, None, ["evidence ledger unreadable"]
    try:
        rows, pr_gaps, open_gaps, (started, finished) = ledger_tables(store.db, cohort)
    except sqlite3.Error:
        return {}, None, ["evidence ledger unreadable"]
    finally:
        store.close()
    at, begin = evidence.instant(finished), evidence.instant(started)
    if at is None or begin is None:
        return {}, None, ["evidence ledger has no finished pass"]
    if at > now:
        return {}, None, ["evidence ledger clock is ahead of this pass"]
    witnesses = {key: evidence.witness_of(row, pr_gaps.get(key, [])) for key, row in rows.items()}
    return witnesses, evidence.ledger_coverage(witnesses, len(cohort), open_gaps, int(at), int(begin)), []


def source_schema(orch):
    return evidence.ORCH_SCHEMA if type(orch) is LiveOrch else evidence.STAND_IN_SCHEMA


def shell_rows(orch):
    try:
        shell = orch.shell()
        names = orch_api.project_names(shell)
        rows = shell["threads"] + shell["archivedThreads"]
    except orch_api.Down:
        raise models.Failure("Orch unreachable") from None
    except orch_api.TooLarge:
        raise models.Failure(f"Orch shell exceeds {SOURCE_BYTES // MIB} MiB") from None
    except orch_api.Refused as refused:
        raise models.Failure(f"Orch answered {refused.code}") from None
    except (orch_api.Malformed, KeyError, TypeError, AttributeError):
        raise models.Failure("Orch answered malformed data") from None
    return rows, names, "olderSettled" not in shell


def review_targets(rows, names, cohort):
    wanted = {evidence.REPO_KEYS[key.rsplit("#", 1)[0].lower()] + "#" + key.rsplit("#", 1)[1]: key for key in cohort}
    targets = []
    for row in rows:
        hint, _ = models.review_hint(row.get("title"), names.get(row.get("projectId")), row.get("providerInstanceId"))
        if hint and hint["pr"] in wanted:
            targets.append(row)
    return sorted(targets, key=lambda row: (row.get("updatedAt") or "", row["id"]), reverse=True)


def read_reviews(targets, cached, orch, clock):
    verdicts, refused, unread, fresh = {}, {}, 0, {}
    stop = clock() + READ_SECONDS
    reads = 0
    for row in targets:
        if row.get("status") not in FINISHED:
            unread += 1
            continue
        entry = cached.get(row["id"])
        if not isinstance(entry, dict) or entry.get("updated") != row.get("updatedAt") or type(entry.get("observed")) is not int:
            if reads >= READS or clock() > stop:
                unread += 1
                continue
            reads += 1
            try:
                record, reason = evidence.verdict_of(orch.thread(row["id"]), row["id"])
            except orch_api.TooLarge:
                record, reason = None, "thread_too_large"
            except (orch_api.Down, orch_api.Refused, orch_api.Malformed):
                unread += 1
                continue
            entry = {"updated": row.get("updatedAt"), "observed": int(clock()), "record": record, "refused": reason}
        fresh[row["id"]] = entry
        if entry["record"]:
            verdicts.setdefault(entry["record"]["pr"], []).append(entry["record"])
        else:
            refused[entry["refused"]] = refused.get(entry["refused"], 0) + 1
    return verdicts, refused, unread, fresh


def retain(directory, freeze):
    os.makedirs(directory, exist_ok=True)
    save_json(os.path.join(directory, freeze["manifest"]["manifest_hash"] + ".json"), freeze)
    held = sorted((entry.stat().st_mtime, entry.path) for entry in os.scandir(directory) if entry.name.endswith(".json"))
    for _, path in held[:-RETAINED_FREEZES]:
        os.unlink(path)


def compute(repos, repos_bytes, report, report_bytes, orch_schema, rows, verdicts, refused, unread, projects_complete, ledger, entries, now, github_gaps, orch_gaps, ledger_rows, coverage):
    cohort = evidence.cohort_rows(repos)
    links, counts, refused_links = evidence.exact_links(rows)
    records = evidence.join(cohort, links, counts, verdicts, ledger, report["runs"], ledger_rows)
    gaps = orch_gaps + ([] if projects_complete else ["Orch shell omitted settled threads"])
    if unread:
        gaps.append(f"{unread} review threads unread")
    orch_digest = evidence.digest({"verdicts": verdicts, "links": links})
    oldest = min([now] + [entry["observed"] for entry in entries.values()])
    sources = evidence.source_rows(repos, repos_bytes, report, report_bytes, orch_schema, orch_digest, gaps, oldest, now, github_gaps + evidence.compaction_gaps(ledger), coverage)
    as_of = evidence.stamp(now)
    packet = evidence.normalized(records, sources, evidence.inventory_of(sources), as_of, evidence.stamp(max(repos["fetched"], now)), evidence.stamp(repos["fetched"]))
    manifest = build_manifest(packet)
    decision = decide(manifest)
    freeze = {"schema": "flightops-analysis-freeze/1", "normalized_input": packet, "manifest": manifest}
    freeze["freeze_hash"] = frozen_digest(freeze)
    merged = sum(r["state"] == "merged" for r in records)
    tally = {"prs": len(records), "merged": merged, "open": len(records) - merged, "with_orch_link": sum(bool(r["links"]) for r in records),
             "with_review_record": sum(bool(r["verdicts"]) for r in records), "head_exact": sum(r["head_state"] in ("exact", "exact_at_last_sighting") for r in records),
             "ci_observed": sum(r["ci"]["state"] == "observed" for r in records), "reviews_unread": unread, "reviews_refused": refused, "links_refused": refused_links,
             "first_ready_seen": sum(evidence.ready_value(r) is not None for r in records), "head_at_open_exact": sum((r["witness"] or {}).get("head_at_open", {}).get("status") == "exact" for r in records)}
    shown = evidence.presentation(decision, manifest, records, tally)
    return evidence.publication(decision, manifest, shown, now), freeze


def collect(data_dir, orch, clock=time.time):
    orch_schema = source_schema(orch)
    now = int(clock())
    raw = os.path.join(data_dir, "raw")
    cache = os.path.join(data_dir, "cache")
    repos, repos_bytes = github(raw, now)
    report, report_bytes = usage(raw, now)
    rows, names, complete = shell_rows(orch)
    os.makedirs(cache, exist_ok=True)
    ledger_path, reviews_path = os.path.join(cache, "best-ledger.json"), os.path.join(cache, "best-reviews.json")
    prior, ledger_saves = read_cache(ledger_path, evidence.ledger_shape, now)
    ledger = evidence.compacted(evidence.sighted(prior, repos))
    targets = review_targets(rows, names, evidence.cohort_rows(repos))
    cached, reviews_save = read_cache(reviews_path, lambda value: isinstance(value, dict), now)
    verdicts, refused, unread, entries = read_reviews(targets, cached, orch, clock)
    ledger_rows, coverage, refusals = read_ledger(data_dir, evidence.cohort_rows(repos), now)
    out, freeze = compute(repos, repos_bytes, report, report_bytes, orch_schema, rows, verdicts, refused, unread, complete, ledger, entries, now,
                          cache_gaps(ledger_path, ledger_saves) + refusals, cache_gaps(reviews_path, reviews_save), ledger_rows, coverage)
    if ledger_saves:
        save_json(ledger_path, ledger)
    if reviews_save:
        save_json(reviews_path, entries)
    retain(os.path.join(cache, "best-freeze"), freeze)
    return out


def run(data_dir, orch, clock=time.time):
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        return collect(data_dir, orch, clock)
    except models.Failure as failure:
        return {"schema": evidence.PUBLICATION_SCHEMA, "error": str(failure)}
    except MemoryError:
        return {"schema": evidence.PUBLICATION_SCHEMA, "error": f"input exceeds the {MEMORY_BYTES // MIB} MiB memory budget"}
    except ContractError as failure:
        reason = "invalid independence keys" if str(failure) == "independence keys required" else "comparison contract refused"
        return {"schema": evidence.PUBLICATION_SCHEMA, "error": "evidence could not be frozen: " + reason, "failure_code": reason.replace(" ", "_")}
    except OSError:
        return {"schema": evidence.PUBLICATION_SCHEMA, "error": "evidence could not be frozen: storage unavailable", "failure_code": "storage_unavailable"}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"schema": evidence.PUBLICATION_SCHEMA, "error": "evidence could not be frozen: malformed input", "failure_code": "malformed_input"}


def main():
    json.dump(run(sys.argv[1] if len(sys.argv) > 1 else ".", LiveOrch()), sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
