import http.server
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collectors"))
import best
import best_evidence as evidence
import ledger_store

CAPTURE = ROOT / "tests" / "fixtures" / "best-slice" / "synthetic-capture.json"
TARGET = "example-org/example-backend#6991"
PUBLICATION = ROOT / "tests" / "fixtures" / "best-slice" / "publication.json"


def capture():
    return json.loads(CAPTURE.read_text())


class StandInOrch:
    def __init__(self, data):
        if evidence.STAND_IN_MARK not in data["shell"]:
            raise ValueError("a recording must carry the stand-in mark")
        self.data, self.calls = data, []

    def shell(self):
        self.calls.append("shell")
        return self.data["shell"]

    def thread(self, thread_id):
        self.calls.append(thread_id)
        return self.data["snapshots"][thread_id]


class Transport:
    def __init__(self, data):
        self.shell, self.snapshots, self.calls = data["shell"], data["snapshots"], []

    def __call__(self, request, timeout=None):
        self.calls.append(request.get_method())
        path = urllib.parse.unquote(urllib.parse.urlparse(request.full_url).path)
        body = self.shell if path.endswith("/shell") else self.snapshots[path.rsplit("/", 1)[1]]
        return _Answer(io.BytesIO(json.dumps(body).encode()))


class _Answer:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self.stream

    def __exit__(self, *_):
        return False


def loopback(data):
    shell = {key: value for key, value in data["shell"].items() if key != evidence.STAND_IN_MARK}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = urllib.parse.unquote(self.path)
            body = json.dumps(shell if path.endswith("/shell") else data["snapshots"][path.rsplit("/", 1)[1]]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def through_live_client(data, passes=4):
    root, token = data_dir(data)
    server, base = loopback(data)
    orch = best.LiveOrch(base, token)
    try:
        for _ in range(passes):
            out = best.collect(root, orch, clock=lambda: data["capture"]["made_at"])
    finally:
        server.shutdown()
        server.server_close()
    return out


def seed_ledger(root, witness):
    store = ledger_store.Store(root)
    for row in witness["passes"]:
        store.db.execute("INSERT INTO passes(id, started_at, finished_at, outcome) VALUES (:id, :started_at, :finished_at, :outcome)", row)
    for row in witness["rows"]:
        store.db.execute(f"INSERT INTO pr_witness({', '.join(row)}) VALUES ({', '.join(':' + key for key in row)})", row)
    for gap in witness["gaps"]:
        store.db.execute("INSERT INTO gap_events(source, repo, subject, kind, event, detail, pass, at) VALUES (:source, :repo, :subject, :kind, 'opened', :detail, :opened_pass, :opened_at)", gap)
    store.checkpoint()
    store.close()


def data_dir(data):
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "raw"))
    os.makedirs(os.path.join(root, "cache"))
    if data.get("witness"):
        seed_ledger(root, data["witness"])
    ledger = {}
    for generation in data.get("generations") or []:
        ledger = evidence.sighted(ledger, generation)
    if ledger:
        with open(os.path.join(root, "cache", "best-ledger.json"), "w") as f:
            json.dump(ledger, f)
    with open(os.path.join(root, "raw", "repos.json"), "w") as f:
        json.dump(data["repos"], f)
    with open(os.path.join(root, "raw", "models.json"), "w") as f:
        json.dump(data["models"], f)
    token = os.path.join(root, "token")
    with open(token, "w") as f:
        f.write("stand-in")
    return root, token


def published(data, passes=4, source=StandInOrch):
    root, _ = data_dir(data)
    orch = source(data)
    out = None
    for _ in range(passes):
        out = best.collect(root, orch, clock=lambda: data["capture"]["made_at"])
    return out, root, orch


class Origin:
    def __init__(self, answer):
        self.seen = []
        origin = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                origin.seen.append(self.headers.get("Authorization"))
                code, headers, body = answer
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
