import codecs
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
from ledger_store import normal_run

ROOT = os.path.expanduser("~/ci-program/data/daily")
REPOS = {v: k for k, v in settings.REPOS.items()}
SOURCE = "ci-program-daily"
STEP_SECONDS = 15
CHUNK = 1024 * 1024
ELEMENT_LIMIT = 8 * 1024 * 1024
COMMIT_EVERY = 500
LIMITS = ("Run-level copy of GET actions/runs from a daily ci-program snapshot. One row per run at the attempt GitHub listed that day, "
          "so earlier attempts are absent unless an earlier snapshot saw them. No jobs, check runs, statuses or required-check policy. "
          "Conclusions can be stale. Sampled job detail in the same snapshot is not imported.")


class Malformed(Exception):
    pass


def window(root, snapshot):
    try:
        with open(os.path.join(root, snapshot, "config.json")) as f:
            config = json.load(f)
        return config.get("start"), config.get("end")
    except (OSError, ValueError, AttributeError):
        return None, None


def freeze_manifest(store, root, now):
    if store.cursor("import:manifest"):
        return True
    if not os.path.isdir(root):
        with store.tx():
            store.open_gap("import", "all", root, "source-missing", "snapshot folder not found", now)
        return False
    with store.tx():
        for snapshot in sorted(os.listdir(root)):
            for folder, repo in REPOS.items():
                path = os.path.join(root, snapshot, folder, "runs.json")
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                start, end = window(root, snapshot)
                store.db.execute("INSERT OR IGNORE INTO import_files(path, snapshot, repo, bytes, mtime_ns, window_start, window_end, state, limits) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                                 (path, snapshot, repo, info.st_size, info.st_mtime_ns, start, end, LIMITS))
        store.set_cursor("import:manifest", {"frozen": True})
        store.close_gap("import", "all", root, "source-missing", now)
    return True


class Elements:
    def __init__(self, path, offset):
        self.file = open(path, "rb")
        self.file.seek(offset)
        self.offset = offset
        self.decoder = json.JSONDecoder()
        self.utf8 = codecs.getincrementaldecoder("utf-8")()
        self.text = ""
        self.at = 0
        self.ended = False
        self.opened = offset > 0

    def close(self):
        self.file.close()

    def fill(self):
        chunk = self.file.read(CHUNK)
        if not chunk:
            self.ended = True
            return False
        self.text = self.text[self.at:] + self.utf8.decode(chunk)
        self.at = 0
        return True

    def separators(self):
        while True:
            while self.at < len(self.text) and self.text[self.at] in " \t\r\n,":
                self.offset += 1
                self.at += 1
            if self.at < len(self.text) or not self.fill():
                return

    def opening(self):
        self.separators()
        if self.at >= len(self.text) or self.text[self.at] != "[":
            raise Malformed("not a JSON array")
        self.at += 1
        self.offset += 1
        self.opened = True

    def next(self):
        if not self.opened:
            self.opening()
        self.separators()
        if self.at >= len(self.text):
            raise Malformed("array never closed")
        if self.text[self.at] == "]":
            return None
        while True:
            try:
                value, end = self.decoder.raw_decode(self.text, self.at)
                break
            except ValueError:
                if len(self.text) - self.at > ELEMENT_LIMIT or not self.fill():
                    raise Malformed("unreadable element") from None
        self.offset += len(self.text[self.at:end].encode("utf-8"))
        self.at = end
        return value


def import_file(store, row, deadline, clock, now):
    try:
        info = os.stat(row["path"])
    except OSError:
        info = None
    if info is None or info.st_size != row["bytes"] or info.st_mtime_ns != row["mtime_ns"]:
        with store.tx():
            store.db.execute("UPDATE import_files SET state = 'changed' WHERE id = ?", (row["id"],))
            store.open_gap("import", row["repo"], row["path"], "source-changed", "file differs from the frozen manifest", now)
        return True
    reader = Elements(row["path"], row["offset"])
    read, new, bad = 0, 0, 0
    try:
        done = False
        while not done:
            with store.tx():
                for _ in range(COMMIT_EVERY):
                    try:
                        element = reader.next()
                    except Malformed as problem:
                        store.db.execute("UPDATE import_files SET state = 'failed', offset = ?, rows_read = rows_read + ?, rows_new = rows_new + ?, rows_bad = rows_bad + ? WHERE id = ?",
                                         (reader.offset, read, new, bad, row["id"]))
                        store.open_gap("import", row["repo"], row["path"], "file-unreadable", str(problem), now)
                        return True
                    if element is None:
                        done = True
                        break
                    normal = normal_run(row["repo"], element) if isinstance(element, dict) else None
                    read += 1
                    if normal is None:
                        bad += 1
                    else:
                        new += store.add_run(normal, SOURCE, str(row["id"]), row["window_end"])
                store.db.execute("UPDATE import_files SET state = ?, offset = ?, rows_read = rows_read + ?, rows_new = rows_new + ?, rows_bad = rows_bad + ?, finished_pass = ? WHERE id = ?",
                                 ("done" if done else "pending", reader.offset, read, new, bad, store.pass_id if done else None, row["id"]))
                read, new, bad = 0, 0, 0
            if not done and clock() >= deadline:
                return False
        return True
    finally:
        reader.close()


def run(store, root=ROOT, seconds=STEP_SECONDS, clock=time.monotonic, now=None):
    now = time.time() if now is None else now
    if not freeze_manifest(store, root, now):
        return {"files": 0, "pending": None}
    deadline = clock() + seconds
    while clock() < deadline:
        row = store.db.execute("SELECT * FROM import_files WHERE state = 'pending' ORDER BY snapshot, repo LIMIT 1").fetchone()
        if row is None or not import_file(store, row, deadline, clock, now):
            break
    return dict(zip(("files", "pending"), store.db.execute("SELECT count(*), sum(state = 'pending') FROM import_files").fetchone()))
