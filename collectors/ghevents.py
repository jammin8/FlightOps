#!/usr/bin/env python3

























import datetime
import fcntl
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

GH = next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
DIR = os.path.expanduser("~/Library/Application Support/FlightOps/github-events")
OWNER = settings.OWNER
REPOS = settings.REPOS
EVERY = 300
PAGES = 3
CAP = 36
KEEP = 90 * 86400
KINDS = {"PullRequestEvent", "PushEvent"}
ACTIONS = {"opened", "reopened", "closed", "merged"}


def ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except (ValueError, AttributeError):
        return None


def fetch(repo, page, etag=None):

    args = [GH, "api", "-i", f"repos/{OWNER}/{REPOS[repo]}/events?per_page=100&page={page}"]
    if etag:
        args[2:2] = ["-H", f"If-None-Match: {etag}"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    head, _, body = r.stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return 0, {}, (r.stderr.strip() or "no answer")[:160]
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:]) if k}
    if status != 200:
        return status, headers, None
    try:
        return status, headers, json.loads(body)
    except ValueError:
        return 0, headers, "unreadable body"


def receipt(repo, e):

    pl = e.get("payload") or {}
    base = {"id": str(e.get("id")), "repo": repo, "type": e.get("type"), "at": e.get("created_at"), "t": ts(e.get("created_at")),
            "actor": (e.get("actor") or {}).get("login")}
    if e.get("type") == "PushEvent":
        return {**base, "ref": pl.get("ref"), "before": pl.get("before"), "head": pl.get("head"), "push_id": pl.get("push_id")}
    if e.get("type") == "PullRequestEvent" and pl.get("action") in ACTIONS:
        pr = pl.get("pull_request") or {}
        return {**base, "action": pl.get("action"), "number": pl.get("number") or pr.get("number"),
                "head_sha": (pr.get("head") or {}).get("sha"), "head_ref": (pr.get("head") or {}).get("ref"),
                "base_ref": (pr.get("base") or {}).get("ref")}
    return None


def merge(coverage, keep_after):

    out = []
    for a, b in sorted(c for c in coverage if c[1] >= keep_after):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def poll_repo(st, repo, now, counted, get=fetch, persist=lambda: None, stored=frozenset()):




    if now - (st.get("polled") or 0) < max(EVERY, st.get("poll_interval") or 0):
        return [], "not due"
    seen = set(st.get("seen") or [])
    newest = int(st["newest_id"]) if st.get("newest_id") else None

    def known(i):


        return i in seen or i in stored

    def reached(i):


        return known(i) or (not seen and newest is not None and int(i) <= newest)

    first = not seen and newest is None
    fetched, overlap, page, etag = [], False, 1, None
    while True:
        if len(counted) + 1 > CAP:
            if page == 1:
                return [], "deferred (hourly cap)"
            break
        counted.append(now)
        persist()
        status, headers, body = get(repo, page, st.get("etag") if page == 1 else None)
        if status == 304:
            counted.remove(now)
            persist()
        if page == 1:
            st["polled"] = now
            st["poll_interval"] = int(headers.get("x-poll-interval") or 0) or None
        if status == 304:
            st["coverage"] = merge(st.get("coverage", []) + ([[st["coverage"][-1][1], now]] if st.get("coverage") else []), now - KEEP)
            st.pop("error", None)
            return [], "not modified"
        if status != 200:
            st["error"] = f"HTTP {status}: {body}" if isinstance(body, str) else f"HTTP {status}"
            return [], st["error"]
        if page == 1:
            etag = headers.get("etag")
        fetched += body
        if any(reached(str(e["id"])) for e in body):
            overlap = True
        reached_end = len(body) < 100 or 'rel="next"' not in headers.get("link", "")
        if overlap or reached_end or page >= PAGES:
            break
        page += 1


    new = sorted((e for e in fetched if not known(str(e["id"]))), key=lambda e: int(e["id"]))
    oldest = min((ts(e["created_at"]) for e in fetched), default=now)
    cov = st.get("coverage", [])
    if not first and overlap and cov:
        cov = cov + [[cov[-1][1], now]]
    else:
        if not first:
            st.setdefault("gaps", []).append({"after": cov[-1][1] if cov else None, "before": oldest, "detected": now})
        cov = cov + [[oldest, now]]
    st["coverage"] = merge(cov, now - KEEP)
    st["gaps"] = [g for g in st.get("gaps", []) if g["detected"] >= now - KEEP]
    if fetched:
        st["newest_id"] = str(max([int(e["id"]) for e in fetched] + ([newest] if newest else [])))
    st["seen"] = sorted(seen | {str(e["id"]) for e in fetched}, key=int)[-1000:]
    st["etag"] = etag
    st.pop("error", None)
    return [r for r in (receipt(repo, e) for e in new) if r], f"{len(new)} new"


def load_state(d=DIR):
    try:
        with open(os.path.join(d, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"repos": {}, "counted": []}


def save_state(state, d=DIR):
    tmp = os.path.join(d, f"state.json.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, os.path.join(d, "state.json"))


def seen_ids(d=DIR):
    out = set()
    try:
        with open(os.path.join(d, "receipts.jsonl")) as f:
            for line in f:
                try:
                    out.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return out


def valid_receipt(x):
    if not isinstance(x, dict) or x.get("repo") not in REPOS or type(x.get("t")) not in (int, float):
        return False
    if x.get("type") == "PushEvent":
        return all(key in x for key in ("ref", "before", "head"))
    return x.get("type") == "PullRequestEvent" and x.get("action") in ACTIONS and type(x.get("number")) is int


def parse_receipts(d=DIR, path=None):


    good, bad = [], []
    try:
        with open(path or os.path.join(d, "receipts.jsonl")) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    x = json.loads(line)
                    int(x["id"])
                    if not valid_receipt(x):
                        raise ValueError
                    good.append(x)
                except (ValueError, KeyError, TypeError):
                    bad.append(line)
    except OSError:
        pass
    return good, bad


def lost_at(line):
    try:
        x = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(x, dict) or x.get("repo") not in REPOS or type(x.get("t")) not in (int, float):
        return None, None
    return x["repo"], x["t"]


def split(coverage, t):
    out = []
    for a, b in coverage:
        if a <= t <= b:
            out += [c for c in ([a, t - 1], [t + 1, b]) if c[0] <= c[1]]
        else:
            out.append([a, b])
    return out


def invalidate(state, bad, now):
    repos = state.setdefault("repos", {})
    for line in bad:
        repo, t = lost_at(line)
        for name, st in repos.items():
            if repo is None or name == repo:
                st["coverage"] = split(st.get("coverage", []), t) if repo else []
                st.setdefault("gaps", []).append({"after": t, "before": t, "detected": now, "why": "damaged receipt"})


def repair_receipts(d=DIR, now=None):


    good, bad = parse_receipts(d)
    path = os.path.join(d, "receipts.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            torn = f.tell() > 0 and (f.seek(-1, os.SEEK_END) or f.read(1)) != b"\n"
    except OSError:
        return
    if not bad and not torn:
        return
    if bad:
        state = load_state(d)
        invalidate(state, bad, time.time() if now is None else now)
        save_state(state, d)
        with open(os.path.join(d, "receipts.corrupt.jsonl"), "a") as f:
            f.writelines(l if l.endswith("\n") else l + "\n" for l in bad)
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.writelines(json.dumps(x) + "\n" for x in good)
    os.replace(tmp, path)


def held(x, hold):
    h = hold.get(x["repo"]) or {}
    return x.get("number") in h.get("numbers", ()) or x.get("ref") in h.get("refs", ())


def archive_receipts(d=DIR, now=None, hold=None):
    now = time.time() if now is None else now
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        repair_receipts(d, now)
        good, _ = parse_receipts(d)
        old = [x for x in good if x["t"] < now - KEEP and not held(x, hold or {})]
        if not old:
            return 0
        folder = os.path.join(d, "archive")
        os.makedirs(folder, exist_ok=True)
        months = {}
        for x in old:
            months.setdefault(time.strftime("%Y-%m", time.gmtime(x["t"])), []).append(x)
        for month, rows in months.items():
            with open(os.path.join(folder, f"receipts-{month}.jsonl"), "a") as f:
                f.writelines(json.dumps(x) + "\n" for x in rows)
                f.flush()
                os.fsync(f.fileno())
        moved = {x["id"] for x in old}
        path = os.path.join(d, "receipts.jsonl")
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.writelines(json.dumps(x) + "\n" for x in good if x["id"] not in moved)
        os.replace(tmp, path)
        return len(old)


def poll(now=None, d=DIR, get=fetch):

    now = now or time.time()
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        repair_receipts(d, now)
        state = load_state(d)
        counted = state["counted"] = [t for t in state.get("counted", []) if t > now - 3600]
        seen, done = seen_ids(d), {}
        with open(os.path.join(d, "receipts.jsonl"), "a") as out:
            for repo in REPOS:
                st = state["repos"].setdefault(repo, {})
                before = json.loads(json.dumps(st))
                saved = json.loads(json.dumps(state))

                def persist():


                    saved["counted"] = list(counted)
                    save_state(saved, d)

                try:
                    new, what = poll_repo(st, repo, now, counted, get, persist=persist, stored=seen)
                except (subprocess.SubprocessError, OSError) as e:
                    state["repos"][repo] = st = {**before, "error": str(e)[:160], "polled": st.get("polled"),
                                                 "poll_interval": st.get("poll_interval")}
                    new, what = [], str(e)[:160]
                for r in new:
                    if r["id"] not in seen:
                        out.write(json.dumps(r) + "\n")
                        seen.add(r["id"])
                out.flush()
                os.fsync(out.fileno())
                save_state(state, d)
                done[repo] = what
        state["counted"] = counted
        save_state(state, d)
    return summary(state, now, done)


def summary(state, now, done=None):
    return {"schema": "github-events/1", "at": int(now), "counted_last_hour": len([t for t in state.get("counted", []) if t > now - 3600]),
            "cap": CAP, "repos": {r: {"polled": st.get("polled"), "covered_since": (st.get("coverage") or [[None]])[-1][0],
                                      "gaps": len(st.get("gaps", [])), "gap_list": st.get("gaps", [])[-20:],
                                      "error": st.get("error"), "last": (done or {}).get(r)}
                                  for r, st in state.get("repos", {}).items()}}


def load_receipts(d=DIR, archived=False):


    out = {r: {"opened": {}, "pushes": [], "coverage": []} for r in REPOS}
    lines, bad = parse_receipts(d)
    if archived:
        for path in sorted(glob.glob(os.path.join(d, "archive", "receipts-*.jsonl"))):
            lines += parse_receipts(d, path)[0]
        lines = list({x["id"]: x for x in lines}.values())
    for x in sorted(lines, key=lambda x: (x.get("t") or 0, int(x["id"]))):
        r = out.get(x.get("repo"))
        if r is None:
            continue
        if x["type"] == "PushEvent":
            r["pushes"].append({"ref": x["ref"], "before": x["before"], "head": x["head"], "t": x["t"], "id": x["id"]})
        elif x.get("action") == "opened" and x.get("head_sha"):
            r["opened"].setdefault(x["number"], []).append({"sha": x["head_sha"], "t": x["t"], "id": x["id"]})
    for repo, st in load_state(d).get("repos", {}).items():
        if repo in out:

            out[repo]["coverage"] = [] if bad else [list(c) for c in st.get("coverage", [])]
    return out


if __name__ == "__main__":
    json.dump(poll(), sys.stdout)
