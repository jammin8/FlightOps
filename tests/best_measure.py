#!/usr/bin/env python3
import http.server
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import best_support as support
import best
import best_evidence as evidence

STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"')
STRUCTURE = re.compile(r"[,:\[{]")
MB = 1000000
LARGEST_REVIEW = int(2.8 * MB)
LIMIT_BODY = best.THREAD_BYTES - 4096
OVER_LIMIT_BODY = 80 * MB
ITEM_BYTES = 11700
DENSE_ITEM_BYTES = 200
SHELL_ROWS = 2025
COLLECTORS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collectors")
CAPS = (evidence.LEDGER_PRS, evidence.LEDGER_HEADS, evidence.LEDGER_ROWS)
NODES = 395000
PR_ROW = {"repo": "backend", "number": 1, "title": "x" * 80, "head": "a" * 40, "ci": "passed", "created": "2026-10-20T00:00:00Z", "merged_at": "2026-10-21T00:00:00Z"}
RUN_ROW = {"thread": "mcp:" + "a" * 36, "model": "claude-sonnet-5-5", "effort": "medium", "harness": "claudeAgent", "status": "completed", "tokens": {"input": 1, "output": 1, "cache_write": 1, "cache_read": 1, "reasoning": 1}}
BASE = {"body": LARGEST_REVIEW, "item": ITEM_BYTES, "stall": 0, "prs": 0, "ledger": None, "files": 0}
SCENARIOS = {"real": BASE,
             "ledger-at-caps": BASE | {"prs": 480, "ledger": CAPS},
             "bodies-at-limit": BASE | {"body": LIMIT_BODY, "prs": 480},
             "dense-bodies-at-limit": BASE | {"body": LIMIT_BODY, "item": DENSE_ITEM_BYTES, "prs": 480},
             "files-at-byte-limit": BASE | {"prs": 480, "ledger": CAPS, "files": best.SOURCE_BYTES - 4096},
             "bodies-over-limit": BASE | {"body": OVER_LIMIT_BODY, "prs": 480},
             "everything-at-caps": BASE | {"body": LIMIT_BODY, "prs": 480, "ledger": CAPS},
             "dense-everything-at-caps": BASE | {"body": LIMIT_BODY, "item": DENSE_ITEM_BYTES, "prs": 480, "ledger": CAPS},
             "ledger-past-its-limit": BASE | {"prs": 480, "ledger": CAPS, "ledger_padding": 2000},
             "tiny-rows-under-node-limit": BASE | {"nodes": NODES},
             "pr-rows-under-node-limit": BASE | {"nodes": NODES, "row": PR_ROW},
             "run-rows-under-node-limit": BASE | {"nodes": NODES, "row": RUN_ROW},
             "everything-under-node-limit": BASE | {"nodes": NODES, "row": RUN_ROW, "body": LIMIT_BODY, "prs": 480, "ledger": CAPS},
             "stalled-orch": BASE | {"stall": 7.5}}


def nodes(value):
    return sum(5 if piece == "{" else 1 for piece in STRUCTURE.findall(STRINGS.sub('""', json.dumps(value))))


def padded(snapshot, scenario):
    item = {"id": "padding", "type": "command_execution", "output": "x" * scenario["item"]}
    copy = json.loads(json.dumps(snapshot))
    room = scenario["body"] - len(json.dumps(copy)) - 64
    copy["projection"].setdefault("turnItems", []).extend(item for _ in range(room // (len(json.dumps(item)) + 2)))
    return json.dumps(copy).encode()


def serve(data, scenario):
    shell = dict(data["shell"])
    del shell[evidence.STAND_IN_MARK]
    filler = dict(shell["threads"][0], id="padding", title="padding", projectId="none")
    shell["threads"] = shell["threads"] + [filler] * max(0, SHELL_ROWS - len(shell["threads"]))
    shell_body = json.dumps(shell).encode()
    bodies = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            thread = self.path.rsplit("/", 1)[1].replace("%3A", ":")
            if self.path.endswith("/shell"):
                body = shell_body
            elif thread not in data["snapshots"]:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            else:
                if scenario["stall"]:
                    time.sleep(scenario["stall"])
                body = bodies.setdefault(thread, padded(data["snapshots"][thread], scenario)) if len(bodies) < 4 or thread in bodies else padded(data["snapshots"][thread], scenario)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def check(index, padding=0):
    return {"kind": "check_run", "name": f"{'integration-and-end-to-end-suite':<{40 + padding}}{index}", "id": index, "status": "COMPLETED", "conclusion": "SUCCESS", "started": "2026-10-31T01:00:00Z"}


def prepare(data, scenario):
    extra = [{"repo": ("backend", "web", "ios")[n % 3], "number": 100000 + n, "head": f"{n:040x}", "ci": "passed", "check_runs": [check(i) for i in range(20)], "check_runs_complete": True,
              "created": "2026-10-20T00:00:00Z"} for n in range(scenario["prs"])]
    data["repos"]["prs"] += extra
    for row in data["repos"]["repos"]:
        row["open"] = sum(pr["repo"] == row["name"] for pr in data["repos"]["prs"])
    root, token = support.data_dir(data)
    for name, value in (("repos", data["repos"]), ("models", data["models"])):
        row = scenario.get("row", {"padding": 1})
        if scenario["files"]:
            copies = (scenario["files"] - len(json.dumps(value)) - 64) // (len(json.dumps(row)) + 2)
        else:
            copies = (scenario.get("nodes", 0) - nodes(value)) // (nodes(row) + 1)
        shape = {"prs": {}, "coverage": {}} if name == "models" else {}
        with open(os.path.join(root, "feed-" + name + ".json"), "w") as f:
            json.dump(value | shape | {"padding": [row] * max(0, copies)}, f)
    if scenario["ledger"]:
        prs, heads, rows = scenario["ledger"]
        at = data["repos"]["fetched"]
        padding = scenario.get("ledger_padding", 0)
        checks = {f"check_run:{check(i, padding)['name']}:{i}:2026-10-31T01:00:00Z": check(i, padding) for i in range(rows)}
        ledger = {f"example-org/example-backend#{n}": {f"{n:020x}{h:020x}": {"first_seen": at, "last_seen": at, "ci": "passed", "check_runs": checks, "complete": True} for h in range(heads)} for n in range(prs)}
        with open(os.path.join(root, "cache", "best-ledger.json"), "w") as f:
            json.dump(ledger, f, separators=(",", ":"))
    return root, token


def fake_gh(root):
    folder = os.path.join(root, "bin")
    os.makedirs(folder)
    path = os.path.join(folder, "gh")
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(path, 0o755)
    return folder


def tree(root_pid):
    parents = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                with open(f"/proc/{name}/stat") as f:
                    parents[int(name)] = int(f.read().rsplit(")", 1)[1].split()[1])
            except OSError:
                pass
    found, grew = {root_pid}, True
    while grew:
        more = {pid for pid, parent in parents.items() if parent in found} - found
        found |= more
        grew = bool(more)
    return found


def memory(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            text = f.read()
    except OSError:
        return None
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    pick = lambda key: int(fields.get(key, "0 kB").split()[0]) // 1024
    return fields["Name"].strip(), pick("VmRSS"), pick("VmHWM")


def sampled(process):
    peak, high = 0, {}
    while process.poll() is None:
        total = 0
        for pid in tree(process.pid):
            seen = memory(pid)
            if seen:
                total += seen[1]
                high[pid] = (seen[0], max(seen[2], high.get(pid, ("", 0))[1]))
        peak = max(peak, total)
        time.sleep(0.01)
    return peak, high


def collectors(root):
    folder = os.path.join(root, "collectors")
    shutil.copytree(COLLECTORS, folder, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("repos", "models"):
        path = os.path.join(folder, name + ".py")
        with open(path) as f:
            real = f.read()
        with open(path, "w") as f:
            f.write(f"import sys\nif __name__ == '__main__':\n    sys.stdout.buffer.write(open({os.path.join(root, 'feed-' + name + '.json')!r}, 'rb').read())\n    raise SystemExit(0)\n" + real)
    return folder


def measure(name, binary):
    scenario = SCENARIOS[name]
    data = support.capture()
    now = time.time()
    data["repos"]["fetched"], data["models"]["generated"] = now, int(now)
    root, token = prepare(data, scenario)
    port = serve(data, scenario)
    status = os.path.join(root, "flightops-status.json")
    env = dict(os.environ, FLIGHTOPS_DATA_DIR=root, FLIGHTOPS_COLLECTORS=collectors(root), FLIGHTOPS_COLLECTORS_ENABLED="vm,repos,models,best,ledger", FLIGHTOPS_ORCH_URL=f"http://127.0.0.1:{port}", FLIGHTOPS_ORCH_TOKEN_FILE=token,
               FLIGHTOPS_STATUS_FILE=status, FLIGHTOPS_PUBLISH_TEXT="1", FLIGHTOPS_LEDGER_IMPORT_ROOT=os.path.join(root, "none"), PATH=fake_gh(root) + ":" + os.environ["PATH"])
    began = time.monotonic()
    process = subprocess.Popen([binary, "collect", "--publish"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    peak, high = sampled(process)
    wall = time.monotonic() - began
    used = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = used.ru_utime + used.ru_stime
    parent = high.get(process.pid, ("", 0))[1]
    largest = max((mib for pid, (_, mib) in high.items() if pid != process.pid), default=0)
    fact = json.load(open(status))["facts"]["best"] if os.path.exists(status) else {}
    print(json.dumps({"scenario": name, "exit": process.returncode, "published": bool(fact.get("value")), "error": fact.get("error"), "wall_s": round(wall, 1), "wall_at_cpu_quota_50_s": round(wall + cpu, 1),
                      "tree_rss_peak_mib": peak, "parent_peak_mib": parent, "largest_child_peak_mib": largest, "tree_bound_mib": parent + largest, "all_descendants_peak_mib": round(used.ru_maxrss / 1024)}))
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    measure(sys.argv[1], sys.argv[2])
