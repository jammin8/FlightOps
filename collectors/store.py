import contextlib
import datetime
import json
import os
import sqlite3

FILE = "flightops.sqlite"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE cursors(name TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE etags(path TEXT PRIMARY KEY, etag TEXT NOT NULL) STRICT;
CREATE TABLE passes(
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT,
  rest_requests INTEGER NOT NULL DEFAULT 0, rest_not_modified INTEGER NOT NULL DEFAULT 0, graphql_requests INTEGER NOT NULL DEFAULT 0,
  core_remaining INTEGER, graphql_remaining INTEGER, report TEXT) STRICT;
CREATE TABLE reads(
  repo TEXT NOT NULL, part TEXT NOT NULL, ok_at TEXT, failed_at TEXT, error TEXT, PRIMARY KEY(repo, part)) STRICT;
CREATE TABLE snapshots(repo TEXT PRIMARY KEY, read_at TEXT NOT NULL, body TEXT NOT NULL) STRICT;

CREATE TABLE prs(
  repo TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL, is_draft INTEGER, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, merged_at TEXT, closed_at TEXT, head_ref TEXT, head_sha TEXT, base_ref TEXT, author TEXT,
  title TEXT, timeline_complete INTEGER NOT NULL, PRIMARY KEY(repo, number)) STRICT;
CREATE INDEX prs_created ON prs(repo, created_at);
CREATE TABLE pr_timeline(
  repo TEXT NOT NULL, number INTEGER NOT NULL, type TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(repo, number, type, at, actor, detail)) STRICT;

CREATE TABLE ci_run_attempts(
  repo TEXT NOT NULL, run_id INTEGER NOT NULL, run_attempt INTEGER NOT NULL, workflow_name TEXT, event TEXT, status TEXT, conclusion TEXT,
  tested_sha TEXT, head_branch TEXT, pr_numbers TEXT, requested_at TEXT, started_at TEXT, updated_at TEXT, jobs_read INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo, run_id, run_attempt)) STRICT;
CREATE INDEX ci_runs_requested ON ci_run_attempts(repo, requested_at);
CREATE TABLE ci_jobs(
  repo TEXT NOT NULL, run_id INTEGER NOT NULL, run_attempt INTEGER NOT NULL, job_id INTEGER NOT NULL, name TEXT, workflow_name TEXT,
  tested_sha TEXT, head_branch TEXT, status TEXT, conclusion TEXT, queued_at TEXT, started_at TEXT, completed_at TEXT,
  runner_name TEXT, labels TEXT, PRIMARY KEY(repo, job_id, run_attempt)) STRICT;
CREATE INDEX ci_jobs_queued ON ci_jobs(repo, queued_at);
"""


def iso(seconds):
    return datetime.datetime.fromtimestamp(int(seconds), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(text):
    if not isinstance(text, str) or not text:
        return None
    try:
        return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def data_dir():
    return os.environ.get("FLIGHTOPS_DATA_DIR") or os.path.expanduser("~/.local/share/flightops")


class Store:
    def __init__(self, directory=None):
        directory = directory or data_dir()
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, FILE)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.pass_id = None
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.db.executescript(f"BEGIN IMMEDIATE; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
        elif version != SCHEMA_VERSION:
            raise RuntimeError(f"store schema {version} is not {SCHEMA_VERSION}")

    @contextlib.contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def close(self):
        self.db.close()

    def cursor(self, name, default=None):
        row = self.db.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_cursor(self, name, value):
        self.db.execute("INSERT INTO cursors(name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, json.dumps(value)))

    def etag(self, path):
        row = self.db.execute("SELECT etag FROM etags WHERE path = ?", (path,)).fetchone()
        return row[0] if row else None

    def set_etag(self, path, etag):
        if etag:
            self.db.execute("INSERT INTO etags(path, etag) VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET etag = excluded.etag", (path, etag))
        else:
            self.db.execute("DELETE FROM etags WHERE path = ?", (path,))

    def read_ok(self, repo, part, now):
        self.db.execute("""INSERT INTO reads(repo, part, ok_at, error) VALUES (?, ?, ?, NULL)
                           ON CONFLICT(repo, part) DO UPDATE SET ok_at = excluded.ok_at, error = NULL""", (repo, part, iso(now)))

    def read_failed(self, repo, part, error, now):
        self.db.execute("""INSERT INTO reads(repo, part, failed_at, error) VALUES (?, ?, ?, ?)
                           ON CONFLICT(repo, part) DO UPDATE SET failed_at = excluded.failed_at, error = excluded.error""", (repo, part, iso(now), str(error)[:200]))

    def reads(self, repo):
        return {row["part"]: dict(row) for row in self.db.execute("SELECT * FROM reads WHERE repo = ?", (repo,))}
