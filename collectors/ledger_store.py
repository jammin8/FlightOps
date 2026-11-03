import contextlib
import datetime
import hashlib
import json
import os
import sqlite3

FILE = "evidence.sqlite"
SCHEMA_VERSION = 2
ZERO = "0" * 40
EVIDENCE = ("activity_events", "ci_run_attempts", "ci_jobs", "ci_check_runs", "ci_statuses", "policy_reads", "gap_events")

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE passes(
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT,
  rest_requests INTEGER NOT NULL DEFAULT 0, rest_not_modified INTEGER NOT NULL DEFAULT 0,
  graphql_requests INTEGER NOT NULL DEFAULT 0, graphql_points INTEGER NOT NULL DEFAULT 0,
  core_remaining_start INTEGER, core_remaining_end INTEGER, graphql_remaining_end INTEGER,
  stopped TEXT, report TEXT) STRICT;
CREATE TABLE cursors(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_pass INTEGER NOT NULL) STRICT;
CREATE TABLE etags(path TEXT PRIMARY KEY, etag TEXT NOT NULL, updated_pass INTEGER NOT NULL) STRICT;
CREATE TABLE gap_events(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, repo TEXT NOT NULL, subject TEXT NOT NULL, kind TEXT NOT NULL,
  event TEXT NOT NULL CHECK(event IN ('opened', 'updated', 'closed')), detail TEXT, pass INTEGER NOT NULL, at TEXT NOT NULL) STRICT;
CREATE INDEX gap_events_key ON gap_events(source, repo, subject, kind, id);
CREATE VIEW gaps AS
  SELECT o.id, o.source, o.repo, o.subject, o.kind, o.detail, o.pass AS opened_pass, o.at AS opened_at, c.pass AS closed_pass, c.at AS closed_at
  FROM gap_events o LEFT JOIN gap_events c ON c.id = (
    SELECT min(x.id) FROM gap_events x
    WHERE x.source = o.source AND x.repo = o.repo AND x.subject = o.subject AND x.kind = o.kind AND x.event = 'closed' AND x.id > o.id)
  WHERE o.event = 'opened';

CREATE TABLE activity_events(
  repo TEXT NOT NULL, event_id INTEGER NOT NULL, type TEXT NOT NULL, ref TEXT NOT NULL,
  before_sha TEXT NOT NULL, after_sha TEXT NOT NULL, at TEXT NOT NULL, actor TEXT, actor_id INTEGER,
  recorded_pass INTEGER NOT NULL, PRIMARY KEY(repo, event_id)) STRICT;
CREATE INDEX activity_ref ON activity_events(repo, ref, at);
CREATE TABLE activity_coverage(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pass INTEGER NOT NULL, read_at TEXT NOT NULL, pages INTEGER NOT NULL,
  rows_read INTEGER NOT NULL, rows_new INTEGER NOT NULL, not_modified INTEGER NOT NULL,
  newest_at TEXT, oldest_at TEXT, walk_open INTEGER NOT NULL) STRICT;
CREATE TABLE branch_incarnations(
  repo TEXT NOT NULL, ref TEXT NOT NULL, seq INTEGER NOT NULL, start_at TEXT NOT NULL, start_event INTEGER NOT NULL,
  start_basis TEXT NOT NULL, end_at TEXT, end_event INTEGER, end_basis TEXT, first_sha TEXT, last_sha TEXT,
  events INTEGER NOT NULL, chain_breaks INTEGER NOT NULL, PRIMARY KEY(repo, ref, seq)) STRICT;

CREATE TABLE prs(
  repo TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL, is_draft INTEGER, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, merged_at TEXT, closed_at TEXT, head_ref TEXT, head_sha TEXT, head_repo TEXT,
  cross_repo INTEGER, base_ref TEXT, merge_sha TEXT, author TEXT, timeline_complete INTEGER NOT NULL,
  first_pass INTEGER NOT NULL, last_pass INTEGER NOT NULL, PRIMARY KEY(repo, number)) STRICT;
CREATE TABLE pr_timeline(
  repo TEXT NOT NULL, number INTEGER NOT NULL, type TEXT NOT NULL, at TEXT NOT NULL, before_sha TEXT NOT NULL DEFAULT '',
  after_sha TEXT NOT NULL DEFAULT '', PRIMARY KEY(repo, number, type, at, before_sha, after_sha)) STRICT;
CREATE TABLE pr_witness(
  repo TEXT NOT NULL, number INTEGER NOT NULL, incarnation_seq INTEGER, binding TEXT NOT NULL,
  head_at_open TEXT, head_at_open_status TEXT NOT NULL, head_at_open_basis TEXT NOT NULL, head_at_open_event INTEGER, head_at_open_candidates TEXT,
  first_ready_at TEXT, first_ready_at_basis TEXT NOT NULL,
  first_ready_head TEXT, first_ready_head_status TEXT NOT NULL, first_ready_head_basis TEXT NOT NULL, first_ready_head_event INTEGER, first_ready_head_candidates TEXT,
  final_head_check TEXT NOT NULL, resolved_pass INTEGER NOT NULL, PRIMARY KEY(repo, number)) STRICT;
CREATE TABLE pr_heads(
  repo TEXT NOT NULL, number INTEGER NOT NULL, sha TEXT NOT NULL, role TEXT NOT NULL, event_id INTEGER, pushed_at TEXT,
  first_pass INTEGER NOT NULL, PRIMARY KEY(repo, number, sha, role)) STRICT;

CREATE TABLE policy_reads(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, rules TEXT NOT NULL, digest TEXT NOT NULL, read_at TEXT NOT NULL, pass INTEGER NOT NULL) STRICT;
CREATE TABLE head_policy(
  repo TEXT NOT NULL, sha TEXT NOT NULL, number INTEGER NOT NULL, base_ref TEXT NOT NULL, status TEXT NOT NULL, required TEXT,
  policy_read INTEGER, policy_read_at TEXT, basis TEXT NOT NULL, pass INTEGER NOT NULL, PRIMARY KEY(repo, sha, number)) STRICT;

CREATE TABLE ci_heads(
  repo TEXT NOT NULL, sha TEXT NOT NULL, number INTEGER NOT NULL, pushed_at TEXT, discovered_pass INTEGER NOT NULL,
  state TEXT NOT NULL, due_at INTEGER NOT NULL, reads INTEGER NOT NULL DEFAULT 0, last_read_at TEXT,
  PRIMARY KEY(repo, sha)) STRICT;
CREATE INDEX ci_heads_due ON ci_heads(state, due_at);
CREATE TABLE ci_fetches(
  repo TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, requested_at TEXT,
  tries INTEGER NOT NULL DEFAULT 0, done_pass INTEGER, PRIMARY KEY(repo, run_id, attempt, kind)) STRICT;
CREATE INDEX ci_fetches_todo ON ci_fetches(state, requested_at);

CREATE TABLE ci_run_attempts(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, run_id INTEGER NOT NULL, run_attempt INTEGER NOT NULL, digest TEXT NOT NULL,
  workflow_name TEXT, workflow_id INTEGER, workflow_path TEXT, run_number INTEGER, event TEXT, status TEXT, conclusion TEXT,
  tested_sha TEXT, head_branch TEXT, head_repo TEXT, pr_numbers TEXT, check_suite_id INTEGER, previous_attempt INTEGER,
  requested_at TEXT, started_at TEXT, updated_at TEXT, actor TEXT, triggering_actor TEXT,
  source TEXT NOT NULL, source_ref TEXT, observed_at TEXT, recorded_pass INTEGER NOT NULL,
  UNIQUE(repo, run_id, run_attempt, digest, source)) STRICT;
CREATE INDEX ci_runs_sha ON ci_run_attempts(repo, tested_sha);
CREATE TABLE ci_jobs(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, run_id INTEGER NOT NULL, run_attempt INTEGER NOT NULL, job_id INTEGER NOT NULL, digest TEXT NOT NULL,
  name TEXT, workflow_name TEXT, tested_sha TEXT, head_branch TEXT, status TEXT, conclusion TEXT,
  queued_at TEXT, started_at TEXT, completed_at TEXT, runner_name TEXT, runner_group TEXT, labels TEXT, steps_total INTEGER, steps_unsuccessful TEXT,
  check_run_id INTEGER, observed_at TEXT NOT NULL, recorded_pass INTEGER NOT NULL,
  UNIQUE(repo, job_id, run_attempt, digest)) STRICT;
CREATE INDEX ci_jobs_run ON ci_jobs(repo, run_id, run_attempt);
CREATE TABLE ci_check_runs(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, check_run_id INTEGER NOT NULL, digest TEXT NOT NULL, name TEXT, tested_sha TEXT,
  status TEXT, conclusion TEXT, started_at TEXT, completed_at TEXT, app_slug TEXT, app_id INTEGER, check_suite_id INTEGER,
  actions_run_id INTEGER, observed_at TEXT NOT NULL, recorded_pass INTEGER NOT NULL, UNIQUE(repo, check_run_id, digest)) STRICT;
CREATE INDEX ci_checks_sha ON ci_check_runs(repo, tested_sha);
CREATE TABLE ci_statuses(
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, status_id INTEGER NOT NULL, tested_sha TEXT NOT NULL, context TEXT, state TEXT,
  created_at TEXT, updated_at TEXT, creator TEXT, observed_at TEXT NOT NULL, recorded_pass INTEGER NOT NULL,
  UNIQUE(repo, status_id)) STRICT;

CREATE TABLE import_files(
  id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL, repo TEXT NOT NULL, bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  window_start TEXT, window_end TEXT, state TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0, rows_read INTEGER NOT NULL DEFAULT 0,
  rows_new INTEGER NOT NULL DEFAULT 0, rows_bad INTEGER NOT NULL DEFAULT 0, limits TEXT NOT NULL, finished_pass INTEGER) STRICT;

CREATE VIEW ci_attempt_latest AS
  SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY repo, run_id, run_attempt
                                              ORDER BY updated_at DESC NULLS LAST, id DESC) AS observation
                 FROM ci_run_attempts) WHERE observation = 1;
CREATE VIEW ci_job_latest AS
  SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY repo, job_id, run_attempt
                                              ORDER BY completed_at DESC NULLS LAST, id DESC) AS observation
                 FROM ci_jobs) WHERE observation = 1;
CREATE VIEW ci_attempt_clocks AS
  SELECT a.repo, a.run_id, a.run_attempt, a.requested_at, min(j.queued_at) AS queued_at, a.started_at,
         CASE WHEN count(j.id) > 0 AND count(j.id) = count(j.completed_at) THEN max(j.completed_at) END AS completed_at
  FROM ci_attempt_latest a LEFT JOIN ci_job_latest j ON j.repo = a.repo AND j.run_id = a.run_id AND j.run_attempt = a.run_attempt
  GROUP BY a.repo, a.run_id, a.run_attempt;
CREATE VIEW ci_retry_outcomes AS
  SELECT 'run' AS level, a.repo, a.tested_sha, a.run_id, a.workflow_name AS name, min(a.run_attempt) AS failed_attempt, a.conclusion AS failed_conclusion,
         min(b.run_attempt) AS passed_attempt, 'retry recovered' AS label
  FROM ci_attempt_latest a JOIN ci_attempt_latest b
    ON b.repo = a.repo AND b.run_id = a.run_id AND b.run_attempt > a.run_attempt AND b.conclusion = 'success'
  WHERE a.conclusion IN ('failure', 'timed_out', 'startup_failure')
    AND NOT EXISTS (SELECT 1 FROM ci_attempt_latest c WHERE c.repo = a.repo AND c.run_id = a.run_id AND c.run_attempt > b.run_attempt
                    AND c.conclusion IN ('failure', 'timed_out', 'startup_failure'))
  GROUP BY a.repo, a.run_id
  UNION ALL
  SELECT 'job', a.repo, a.tested_sha, a.run_id, a.name, min(a.run_attempt), a.conclusion, min(b.run_attempt), 'retry recovered'
  FROM ci_job_latest a JOIN ci_job_latest b
    ON b.repo = a.repo AND b.run_id = a.run_id AND b.name = a.name AND b.run_attempt > a.run_attempt AND b.conclusion = 'success'
  WHERE a.conclusion IN ('failure', 'timed_out')
    AND NOT EXISTS (SELECT 1 FROM ci_job_latest c WHERE c.repo = a.repo AND c.run_id = a.run_id AND c.name = a.name AND c.run_attempt > b.run_attempt
                    AND c.conclusion IN ('failure', 'timed_out'))
  GROUP BY a.repo, a.run_id, a.name;
"""


def guards():
    for table in EVIDENCE:
        for verb in ("UPDATE", "DELETE"):
            yield (f"CREATE TRIGGER {table}_keep_{verb.lower()} BEFORE {verb} ON {table} "
                   f"BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;")


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text):
    try:
        return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()) if text else None
    except (ValueError, AttributeError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


class Refused(RuntimeError):
    pass


def refuse_other_schema(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        found = probe.execute("PRAGMA user_version").fetchone()[0]
    finally:
        probe.close()
    if found not in (0, SCHEMA_VERSION):
        raise Refused(f"ledger file is schema {found}, this build needs {SCHEMA_VERSION}; the file is unchanged, move {FILE} aside to rebuild")


class Store:
    def __init__(self, directory, readonly=False):
        self.path = os.path.join(directory, FILE)
        self.pass_id = 0
        refuse_other_schema(self.path)
        if readonly:
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5, isolation_level=None)
            self.db.execute("PRAGMA query_only=ON")
        else:
            os.makedirs(directory, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.migrate()
        self.db.execute("PRAGMA cache_size=-16000")
        self.db.row_factory = sqlite3.Row

    def migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        with self.tx():
            for statement in SCHEMA.split(";\n"):
                if statement.strip():
                    self.db.execute(statement)
            for statement in guards():
                self.db.execute(statement)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextlib.contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def close(self):
        self.db.close()

    def checkpoint(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def begin_pass(self, now):
        with self.tx():
            self.pass_id = self.db.execute("INSERT INTO passes(started_at) VALUES (?)", (iso(now),)).lastrowid
        return self.pass_id

    def cursor(self, name, default=None):
        row = self.db.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set_cursor(self, name, value):
        self.db.execute("INSERT INTO cursors(name, value, updated_pass) VALUES (?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_pass = excluded.updated_pass",
                        (name, json.dumps(value, sort_keys=True), self.pass_id))

    def etag(self, path):
        row = self.db.execute("SELECT etag FROM etags WHERE path = ?", (path,)).fetchone()
        return row[0] if row else None

    def set_etag(self, path, etag):
        if etag:
            self.db.execute("INSERT INTO etags(path, etag, updated_pass) VALUES (?, ?, ?) "
                            "ON CONFLICT(path) DO UPDATE SET etag = excluded.etag, updated_pass = excluded.updated_pass",
                            (path, etag, self.pass_id))

    def gap_state(self, source, repo, subject, kind):
        return self.db.execute("SELECT event, detail FROM gap_events WHERE source = ? AND repo = ? AND subject = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                               (source, repo, subject, kind)).fetchone()

    def gap_event(self, source, repo, subject, kind, event, detail, now):
        self.db.execute("INSERT INTO gap_events(source, repo, subject, kind, event, detail, pass, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (source, repo, subject, kind, event, detail, self.pass_id, iso(now)))

    def open_gap(self, source, repo, subject, kind, detail, now):
        last = self.gap_state(source, repo, subject, kind)
        if last is None or last[0] == "closed":
            self.gap_event(source, repo, subject, kind, "opened", detail, now)
        elif last[1] != detail:
            self.gap_event(source, repo, subject, kind, "updated", detail, now)

    def close_gap(self, source, repo, subject, kind, now):
        last = self.gap_state(source, repo, subject, kind)
        if last is not None and last[0] != "closed":
            self.gap_event(source, repo, subject, kind, "closed", None, now)

    def add_run(self, row, source, source_ref, observed_at):
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO ci_run_attempts(repo, run_id, run_attempt, digest, workflow_name, workflow_id, workflow_path, run_number, event, status,
               conclusion, tested_sha, head_branch, head_repo, pr_numbers, check_suite_id, previous_attempt, requested_at, started_at, updated_at, actor,
               triggering_actor, source, source_ref, observed_at, recorded_pass)
               VALUES (:repo, :run_id, :run_attempt, :digest, :workflow_name, :workflow_id, :workflow_path, :run_number, :event, :status,
               :conclusion, :tested_sha, :head_branch, :head_repo, :pr_numbers, :check_suite_id, :previous_attempt, :requested_at, :started_at, :updated_at, :actor,
               :triggering_actor, :source, :source_ref, :observed_at, :recorded_pass)""",
            dict(row, source=source, source_ref=source_ref, observed_at=observed_at, recorded_pass=self.pass_id))
        return cursor.rowcount


def normal_run(repo, run):
    run_id, attempt = run.get("id"), run.get("run_attempt")
    if type(run_id) is not int or type(attempt) is not int or attempt < 1:
        return None
    prs = sorted({p.get("number") for p in run.get("pull_requests") or [] if isinstance(p, dict) and type(p.get("number")) is int})
    previous = run.get("previous_attempt_url")
    fields = {"repo": repo, "run_id": run_id, "run_attempt": attempt,
              "workflow_name": run.get("name"), "workflow_id": run.get("workflow_id"), "workflow_path": run.get("path"),
              "run_number": run.get("run_number"), "event": run.get("event"), "status": run.get("status"), "conclusion": run.get("conclusion"),
              "tested_sha": run.get("head_sha"), "head_branch": run.get("head_branch"),
              "head_repo": (run.get("head_repository") or {}).get("full_name"), "pr_numbers": json.dumps(prs),
              "check_suite_id": run.get("check_suite_id"), "previous_attempt": 1 if previous else 0,
              "requested_at": run.get("created_at"), "started_at": run.get("run_started_at"), "updated_at": run.get("updated_at"),
              "actor": (run.get("actor") or {}).get("login"), "triggering_actor": (run.get("triggering_actor") or {}).get("login")}
    return dict(fields, digest=digest(fields))
