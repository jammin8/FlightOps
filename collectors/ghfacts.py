#!/usr/bin/env python3
































import contextlib
import datetime
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import receipts

GH = next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
DIR = os.path.expanduser("~/Library/Application Support/FlightOps/github-facts")
OWNER = settings.OWNER
REPOS = settings.REPOS
WINDOW = 28 * 86400
DISCOVER_EVERY = 900
RECHECK = 900
CURRENT_EVERY = 3600
RULES_EVERY = 86400
SETTLE = 3600
CAP = 100
RUNTIME = 40
BATCH = 10
HEAD_BATCH = 30
KEEP = 180 * 86400
TURN_SECONDS = 30
SUITES = 15


def ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except (ValueError, AttributeError):
        return None




def checks(_pr_number=None):


    return "statusCheckRollup { state }"


TIMELINE_TYPES = "[HEAD_REF_FORCE_PUSHED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, PULL_REQUEST_REVIEW, MERGED_EVENT, CLOSED_EVENT, REOPENED_EVENT]"


def timeline(n, after=None):
    a = f', after: "{after}"' if after else ""
    return f"""timelineItems(first: 100{a}, itemTypes: {TIMELINE_TYPES}) {{
    totalCount pageInfo {{ hasNextPage endCursor }} nodes {{ __typename
      ... on HeadRefForcePushedEvent {{ createdAt beforeCommit {{ oid committedDate messageHeadline messageBody parents {{ totalCount }} {checks(n)} }} afterCommit {{ oid }} }}
      ... on ReadyForReviewEvent {{ createdAt }}
      ... on ConvertToDraftEvent {{ createdAt }}
      ... on PullRequestReview {{ databaseId state submittedAt commit {{ oid }} }}
      ... on MergedEvent {{ createdAt }}
      ... on ClosedEvent {{ createdAt }}
      ... on ReopenedEvent {{ createdAt }} }} }}"""


def commits(n, after=None):
    a = f', after: "{after}"' if after else ""
    return f"""commits(first: 40{a}) {{ totalCount pageInfo {{ hasNextPage endCursor }} nodes {{ commit {{
    oid committedDate messageHeadline messageBody parents {{ totalCount }} {checks(n)} }} }} }}"""


def pr_fields(n):
    return f"""id number state isDraft createdAt updatedAt mergedAt closedAt additions deletions changedFiles headRefName
  headRefOid isCrossRepository baseRefName {commits(n)} {timeline(n)}"""


def head_fields(n):
    return f"""... on Commit {{ oid committedDate messageHeadline messageBody parents {{ totalCount }}
  checkSuites(first: {SUITES}) {{ totalCount pageInfo {{ hasNextPage }} nodes {{ databaseId createdAt app {{ slug databaseId }} branch {{ name }}
    workflowRun {{ databaseId event createdAt runNumber workflow {{ name }} }}
    checkRuns(first: 50, filterBy: {{checkType: ALL}}) {{ totalCount pageInfo {{ hasNextPage }}
      nodes {{ databaseId name status conclusion startedAt completedAt isRequired(pullRequestNumber: {n}) }} }} }} }}
  status {{ contexts {{ context state createdAt isRequired(pullRequestNumber: {n}) creator {{ __typename login }} }} }} }}"""


def discovery(after=None):
    return " ".join(f'{k}: repository(owner: "{OWNER}", name: "{v}") {{ pullRequests(first: 100, baseRefName: "main", '
                    f'orderBy: {{field: UPDATED_AT, direction: DESC}}{f", after: {json.dumps(after[k])}" if after and after.get(k) else ""}) '
                    f'{{ pageInfo {{ hasNextPage endCursor }} nodes {{ number state updatedAt mergedAt closedAt headRefOid }} }} }}'
                    for k, v in REPOS.items() if after is None or after.get(k))


def rules_query():
    return " ".join(f'{k}: repository(owner: "{OWNER}", name: "{v}") {{ '
                    'branchProtectionRules(first: 10) { pageInfo { hasNextPage } nodes { pattern requiresStatusChecks '
                    'requiredStatusCheckContexts requiredStatusChecks { context app { slug } } } } '
                    'rulesets(first: 20) { pageInfo { hasNextPage } nodes { name enforcement target '
                    'conditions { refName { include exclude } } rules(first: 30) { pageInfo { hasNextPage } nodes { type parameters { '
                    '... on RequiredStatusChecksParameters { requiredStatusChecks { context integrationId } } } } } } } }'
                    for k, v in REPOS.items())


def cost(requests):


    return max(1, math.ceil(requests / 100))


COST = {"discover": lambda n: cost(n),
        "facts": lambda n: cost(2 * n) + 1,
        "page": lambda n: cost(1) + 1,
        "history": lambda n: cost(n * (2 + SUITES)),
        "rules": lambda n: cost(n * 3 + n * 20)}
MAX_QUERY = max(COST["history"](HEAD_BATCH), COST["facts"](BATCH), COST["page"](1), COST["discover"](3), COST["rules"](3))




def synced(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Busy(RuntimeError):
    pass


@contextlib.contextmanager
def transaction(store, wait=TURN_SECONDS):
    with open(os.path.join(store.d, ".refresh.lock"), "w") as lock:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy("another facts refresh is running") from None
                time.sleep(0.2)
        yield


class Store:



    def __init__(self, d=DIR):
        self.d = d
        os.makedirs(os.path.join(d, "prs"), exist_ok=True)

    def path(self, key, heads=False):
        return os.path.join(self.d, "prs", key.replace("#", "-") + (".heads.json" if heads else ".json"))

    def read(self, path, default=None):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def write(self, path, v):
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(v, f)
        os.replace(tmp, path)

    def accept(self, key, pr):
        with open(os.path.join(self.d, ".write.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            held = self.facts(key) or {}
            if (held.get("fetched_at") or 0) > (pr.get("fetched_at") or 0):
                return False
            self.write(self.path(key), pr)
            return True

    def facts(self, key):
        return self.read(self.path(key))

    def heads(self, key):
        return self.read(self.path(key, heads=True), {})

    def state(self):
        return self.read(os.path.join(self.d, "state.json"), {})

    def save_state(self, state):
        self.write(os.path.join(self.d, "state.json"), state)

    def rules(self):
        return self.read(os.path.join(self.d, "rules.json"), {})

    def generations(self, key):
        folder = self.archive(key)
        try:
            numbers = sorted(int(name) for name in os.listdir(folder) if name.isdigit())
        except OSError:
            return []
        return [(os.path.join(folder, str(n), "facts.json"), os.path.join(folder, str(n), "heads.json")) for n in numbers]

    def archive(self, key):
        return os.path.join(self.d, "archive", key.replace("#", "-"))

    def retire(self, key):
        folder, staging = self.archive(key), os.path.join(self.archive(key), ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        hot = [(self.path(key), "facts.json")]
        if os.path.exists(self.path(key, heads=True)):
            hot.append((self.path(key, heads=True), "heads.json"))
        for path, name in hot:
            shutil.copy2(path, os.path.join(staging, name))
            synced(os.path.join(staging, name))
        synced(staging)
        kept = self.generations(key)
        number = int(os.path.basename(os.path.dirname(kept[-1][0]))) + 1 if kept else 1
        os.rename(staging, os.path.join(folder, str(number)))
        synced(folder)
        for path, _ in hot:
            os.remove(path)

    def keys(self):
        return sorted(f[:-5].replace("-", "#", 1) for f in os.listdir(os.path.join(self.d, "prs"))
                      if f.endswith(".json") and not f.endswith(".heads.json"))





    def _read(self):

        entries, torn = [], 0
        try:
            with open(os.path.join(self.d, "cost.jsonl")) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        torn += 1
                        continue
                    if isinstance(e, dict):
                        entries.append(e)
                    else:
                        torn += 1
        except OSError:
            pass
        return entries, torn

    @staticmethod
    def _charges(entries, now):


        res, real, legacy = {}, {}, 0
        for e in entries:
            if "id" not in e:
                if (e.get("t") or 0) > now - 3600:
                    legacy += e.get("cost") or 0
            elif "reserved" in e:
                if (e.get("t") or 0) > now - 3600:
                    res[e["id"]] = e["reserved"]
            elif e.get("cost") is not None:
                real[e["id"]] = e["cost"]
        return sum(real.get(i, r) for i, r in res.items()) + legacy

    def spent(self, now):

        entries, torn = self._read()
        return self._charges(entries, now) + torn * MAX_QUERY

    def reserve(self, what, points, now):


        with open(os.path.join(self.d, ".budget.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._tidy(now)
            if self.spent(now) + points > CAP:
                return None
            rid = uuid.uuid4().hex[:12]
            self._append({"id": rid, "t": now, "what": what, "reserved": points})
            return rid

    def _tidy(self, now):



        path = os.path.join(self.d, "cost.jsonl")
        entries, torn = self._read()
        try:
            big = os.path.getsize(path) >= 256_000
        except OSError:
            return
        if not torn and not big:
            return
        if big:
            keep = {e["id"] for e in entries if "reserved" in e and (e.get("t") or 0) > now - 7200}
            entries = [e for e in entries if ("id" not in e and (e.get("t") or 0) > now - 7200) or e.get("id") in keep]
        entries += [{"id": f"unreadable-{uuid.uuid4().hex[:8]}", "t": now, "what": "unreadable ledger line", "reserved": MAX_QUERY}
                    for _ in range(torn)]
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def settle(self, rid, points, error=None):
        self._append({"id": rid, "cost": points, "error": error})

    def _append(self, entry):
        with open(os.path.join(self.d, "cost.jsonl"), "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())


def gh(q):


    try:
        r = subprocess.run([GH, "api", "graphql", "-f", "query=" + q], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {}, [], "timeout"
    try:
        body = json.loads(r.stdout or "{}")
    except ValueError:
        return {}, [], (r.stderr.strip() or "unreadable answer")[:160]
    return body.get("data") or {}, body.get("errors") or [], None if body.get("data") else (r.stderr.strip() or "no data")[:160]


class Fetcher:


    def __init__(self, store, now, query=gh, clock=time.monotonic):
        self.store, self.now, self.query, self.clock = store, now, query, clock
        self.started = clock()
        self.errors = []
        self.queries = 0
        self.events = {}
        self.held = 0

    def run(self, what, q, kind, n):



        if self.clock() - self.started >= RUNTIME:
            self.held += 1
            return None, None
        rid = self.store.reserve(what, COST[kind](n), self.now)
        if rid is None:
            self.held += 1
            return None, None
        try:
            data, errors, failure = self.query("query { rateLimit { cost remaining } " + q + " }")
        except Exception as e:
            self.store.settle(rid, None, str(e)[:160])
            self.errors.append(f"{what}: {e}"[:160])
            return None, None
        self.queries += 1
        self.store.settle(rid, (data.get("rateLimit") or {}).get("cost"), failure or (errors[0].get("message") if errors else None))
        unplaced = [e for e in errors if not e.get("path")]
        if failure or unplaced:
            self.errors.append(f"{what}: {failure or unplaced[0].get('message')}"[:160])
            return None, None
        if errors:
            self.errors.append(f"{what}: {errors[0].get('message')}"[:160])
        return data, {tuple(str(x) for x in e["path"][:2]) for e in errors}


def touched(bad, *path):

    return any(b[:len(path)] == tuple(path) for b in bad or ())




def advancing(cursor, seen):
    if not isinstance(cursor, str) or not cursor or cursor in seen:
        return False
    seen.add(cursor)
    return True


def discover(f, state):



    if f.now - (state.get("discovered") or 0) < DISCOVER_EVERY and state.get("prs") is not None:
        return state["prs"]
    out, after, complete, cursors = [], None, True, set()
    for _ in range(5):
        data, bad = f.run("discover", discovery(after), "discover", len(after) if after else len(REPOS))
        if data is None:
            complete = False
            break
        nxt = {}
        for k in REPOS:
            if after is not None and k not in after:
                continue
            conn = (data.get(k) or {}).get("pullRequests")
            if not conn or touched(bad, k):
                complete = False
                continue
            for n in conn["nodes"]:
                if (ts(n["updatedAt"]) or 0) >= f.now - WINDOW:
                    out.append({"key": f"{k}#{n['number']}", "state": n["state"], "updatedAt": n["updatedAt"],
                                "ended": n.get("mergedAt") or n.get("closedAt"), "headRefOid": n.get("headRefOid")})
            last = conn["nodes"][-1] if conn["nodes"] else None
            if conn["pageInfo"]["hasNextPage"] and last and (ts(last["updatedAt"]) or 0) >= f.now - WINDOW:
                cursor = conn["pageInfo"].get("endCursor")
                if advancing(cursor, cursors):
                    nxt[k] = cursor
                else:
                    complete = False
                    f.errors.append(f"discover {k}: missing or repeated cursor")
        after = nxt
        if not after:
            break
    else:
        complete = False
    out = list({x["key"]: x for x in out}.values())
    if not complete and state.get("prs") is not None:

        known = {x["key"]: x for x in state["prs"]}
        known.update({x["key"]: x for x in out})
        out = list(known.values())
    out.sort(key=lambda x: (x["state"] != "OPEN", -(ts(x["updatedAt"]) or 0)))
    state["prs"], state["discovered"], state["discovery_complete"] = out, f.now, complete
    return out


RULES_SHAPE = 2


def refresh_rules(f):


    old = f.store.rules()
    reads = old.get("repos") or {}

    current = old.get("shape") == RULES_SHAPE and all(f.now - ((reads.get(k) or {}).get("read") or 0) < RULES_EVERY for k in REPOS)
    if current or (old.get("shape") == RULES_SHAPE and reads and f.now - (old.get("fetched") or 0) < RECHECK):
        return old
    data, bad = f.run("rules", rules_query(), "rules", len(REPOS))
    if data is None:
        return old
    repos = dict(old.get("repos") or {}) if old.get("shape") == RULES_SHAPE else {}
    for k in REPOS:
        r = data.get(k)
        if r and not touched(bad, k) and receipts.required_checks(r) is not None:
            repos[k] = {"read": f.now, "rules": r}
    new = {"fetched": f.now, "shape": RULES_SHAPE, "repos": repos}
    f.store.write(os.path.join(f.store.d, "rules.json"), new)
    return new


def wanted(store, found, state, now):

    out, retry = [], state.get("retry") or {}
    for x in found:
        key, cached = x["key"], store.facts(x["key"])
        changed = bool(cached) and (cached.get("state") != x["state"] or cached.get("headRefOid") != x["headRefOid"])
        if cached and (cached.get("settled") or cached.get("seal_pending")) and not changed:
            continue
        ended = ts(x["ended"]) if x["state"] != "OPEN" else None
        if ended:
            if now - ended >= SETTLE:
                out.append((key, "seal"))
        elif not cached:
            out.append((key, "new"))
        elif key in retry or changed or cached.get("updatedAt") != x["updatedAt"]:
            out.append((key, "retry" if key in retry else "changed"))
    return out


def follow(f, pr, key):


    for conn, build in (("timelineItems", timeline), ("commits", commits)):
        cursors, rows = set(), {json.dumps(n, sort_keys=True) for n in pr[conn]["nodes"]}
        while pr[conn]["pageInfo"]["hasNextPage"]:
            cursor = pr[conn]["pageInfo"].get("endCursor")
            if not advancing(cursor, cursors):
                f.errors.append(f"{key} {conn}: missing or repeated cursor"[:160])
                return False
            q = f'node(id: "{pr["id"]}") {{ ... on PullRequest {{ {build(pr["number"], cursor)} }} }}'
            data, bad = f.run(f"{key} {conn}", q, "page", 1)
            page = ((data or {}).get("node") or {}).get(conn)
            if not page or bad:
                return False
            for node in page["nodes"]:
                row = json.dumps(node, sort_keys=True)
                if row not in rows:
                    rows.add(row)
                    pr[conn]["nodes"].append(node)
            pr[conn]["pageInfo"] = page["pageInfo"]
    return True


def trim_commit(c):

    if c and c.get("messageBody") is not None:
        c["messageBody"] = "\n".join(l for l in c["messageBody"].splitlines() if l.lower().startswith("co-authored-by:"))


def trim(pr):

    for n in pr["commits"]["nodes"]:
        trim_commit(n.get("commit"))
    for e in pr["timelineItems"]["nodes"]:
        trim_commit(e.get("beforeCommit"))
    return pr


def fetch_facts(f, todo, state):


    done, retry = [], state.setdefault("retry", {})
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        parts = []
        for j, (k, _) in enumerate(chunk):
            repo, n = k.split("#")
            parts.append(f'p{j}: repository(owner: "{OWNER}", name: "{REPOS[repo]}") {{ pullRequest(number: {int(n)}) {{ {pr_fields(int(n))} }} }}')
        data, bad = f.run(f"facts {len(chunk)}", " ".join(parts), "facts", len(chunk))
        if data is None:
            break
        for j, (k, reason) in enumerate(chunk):
            pr = (data.get(f"p{j}") or {}).get("pullRequest")
            if not pr or touched(bad, f"p{j}"):
                retry[k] = {"why": "GitHub error" if touched(bad, f"p{j}") else "not returned", "at": int(f.now)}
                continue
            if not follow(f, pr, k):
                retry[k] = {"why": "a page couldn't be read", "at": int(f.now)}
                continue
            trim(pr)
            ended = bool(pr.get("mergedAt") or pr.get("closedAt"))
            pr["fetched_at"] = int(f.now)
            pr["reread_heads"] = True
            pr["seal_pending"] = ended and reason in ("seal", "verify")
            retry.pop(k, None)
            if f.store.accept(k, pr):
                done.append(k)
    return done


def collected(commit):
    return bool(commit) and receipts.attempts_hydrated(commit)["collected"]


def load_events():

    import ghevents
    return ghevents.load_receipts()


def heads_to_read(p, heads, now, rec=None):


    out = []
    open_ = p.get("state") == "OPEN"
    for h in receipts.heads_of(p, {}, rec):
        got = heads.get(h["sha"])
        if not got:
            out.append(h["sha"])
            continue
        age = now - (got.get("fetched") or 0)
        c = got.get("commit")
        if p.get("reread_heads") and (got.get("fetched") or 0) < (p.get("fetched_at") or 0):
            out.append(h["sha"])
        elif got.get("error") or not collected(c):
            if now - (got.get("error_at") or got.get("fetched") or 0) >= RECHECK:
                out.append(h["sha"])
        elif open_:
            suites = (c.get("checkSuites") or {}).get("nodes") or []
            runs = [r for s in suites for r in ((s.get("checkRuns") or {}).get("nodes") or [])]
            pending = any(s.get("state") in ("PENDING", "EXPECTED") for s in ((c.get("status") or {}).get("contexts") or []))
            if (not suites or pending or any(r.get("status") != "COMPLETED" for r in runs)) and age >= RECHECK:
                out.append(h["sha"])
            elif h["sha"] == p.get("headRefOid") and age >= CURRENT_EVERY:
                out.append(h["sha"])
    return out


def fetch_heads(f, keys):



    todo = []
    for k in keys:
        p = f.store.facts(k)
        if p:
            todo += [(k, p["number"], sha) for sha in heads_to_read(p, f.store.heads(k), f.now, f.events.get(k.split("#")[0]))]
    got = {}
    for i in range(0, len(todo), HEAD_BATCH):
        chunk = todo[i:i + HEAD_BATCH]
        by_repo = {}
        for j, (k, n, sha) in enumerate(chunk):
            by_repo.setdefault(k.split("#")[0], []).append(f'h{j}: object(oid: "{sha}") {{ {head_fields(n)} }}')
        q = " ".join(f'{r}: repository(owner: "{OWNER}", name: "{REPOS[r]}") {{ {" ".join(parts)} }}' for r, parts in by_repo.items())
        data, bad = f.run(f"history {len(chunk)}", q, "history", len(chunk))
        if data is None:
            break
        for j, (k, n, sha) in enumerate(chunk):
            repo = k.split("#")[0]
            c = (data.get(repo) or {}).get(f"h{j}")
            if touched(bad, repo, f"h{j}") or (repo,) in bad:
                got.setdefault(k, {})[sha] = {"error": "GitHub error", "error_at": int(f.now)}
            elif c is None:
                got.setdefault(k, {})[sha] = {"error": "object not returned", "error_at": int(f.now)}
            else:
                trim_commit(c)
                got.setdefault(k, {})[sha] = {"fetched": int(f.now), "commit": c}
    for k, new in got.items():
        heads = f.store.heads(k)
        for sha, v in new.items():
            heads[sha] = v if "commit" in v else {**heads.get(sha, {}), **v}
        f.store.write(f.store.path(k, heads=True), heads)
    for k in keys:
        p = f.store.facts(k)
        if not p or not p.get("reread_heads"):
            continue
        heads = f.store.heads(k)
        whole = all((heads.get(h["sha"]) or {}).get("fetched", 0) >= p["fetched_at"] and not heads[h["sha"]].get("error")
                    and collected(heads[h["sha"]].get("commit")) for h in receipts.heads_of(p, {}, f.events.get(k.split("#")[0])))
        if whole:
            p["reread_heads"] = False
            if p.get("seal_pending"):
                p["settled"], p["seal_pending"] = True, False
            f.store.accept(k, p)
    return sum(len(v) for v in got.values())


def refresh(now=None, store=None, query=gh, clock=time.monotonic, events=None, wait=TURN_SECONDS):
    store = store or Store()
    with transaction(store, wait):
        return refresh_once(now, store, query, clock, events)


def refresh_once(now, store, query, clock, events):


    now = now or time.time()
    f = Fetcher(store, now, query, clock)
    f.events = load_events() if events is None else events
    state = store.state()
    rules = refresh_rules(f)
    found = discover(f, state)
    todo = wanted(store, found, state, now)
    rank = {x["key"]: i for i, x in enumerate(found)}
    fetched, heads = [], 0

    opens = [t for t in todo if t[1] != "seal"]
    for i in range(0, len(opens), BATCH):
        got = fetch_facts(f, opens[i:i + BATCH], state)
        fetched += got
        heads += fetch_heads(f, got)


    pending = [k for k in store.keys() if k not in fetched]
    heads += fetch_heads(f, sorted(pending, key=lambda k: (rank.get(k, len(rank)), k)))

    seals = [t for t in todo if t[1] == "seal"]
    for i in range(0, len(seals), BATCH):
        got = fetch_facts(f, seals[i:i + BATCH], state)
        fetched += got
        heads += fetch_heads(f, got)


    if f.queries and not f.errors:
        state["last_clean_read"] = int(now)
    store.save_state(state)
    due = {k: len(heads_to_read(store.facts(k), store.heads(k), now, f.events.get(k.split("#")[0]))) for k in store.keys()
           if store.facts(k)}
    due = {k: n for k, n in due.items() if n}
    waiting = [k for k, _ in wanted(store, found, state, now)]
    stale = sorted(x["key"] for x in found if store.facts(x["key"]) and any(
        (store.facts(x["key"]) or {}).get(a) != x[a] for a in ("state", "headRefOid", "updatedAt"))
        and not (store.facts(x["key"]) or {}).get("settled"))
    return {"discovered": len(found), "discovery_complete": bool(state.get("discovery_complete")),
            "discovered_at": state.get("discovered"), "waiting": len(waiting), "waiting_keys": waiting,
            "unfetched": sorted(x["key"] for x in found if not store.facts(x["key"])), "stale": stale,
            "retry": state.get("retry") or {}, "fetched": len(fetched), "heads_read": heads,
            "points_last_hour": store.spent(now), "cap": CAP, "queries": f.queries, "held": f.held,
            "errors": f.errors[:5], "last_clean_read": state.get("last_clean_read"),
            "due_heads": sum(due.values()), "due_prs": sorted(due)[:50],
            "rules_read": {k: (v or {}).get("read") for k, v in (rules.get("repos") or {}).items()}}


def refetch(keys, now=None, store=None, query=gh, clock=time.monotonic, wait=TURN_SECONDS):
    store = store or Store()
    with transaction(store, wait):
        return refetch_once(keys, now, store, query, clock)


def refetch_once(keys, now, store, query, clock):


    now = now or time.time()
    f = Fetcher(store, now, query, clock)
    state = store.state()
    done = fetch_facts(f, [(k, "verify") for k in keys], state)
    fetch_heads(f, done)
    store.save_state(state)
    return done


def retire_settled(store, now):
    done = []
    for k in store.keys():
        p = store.facts(k) or {}
        ended = ts(p.get("mergedAt") or p.get("closedAt"))
        if p.get("settled") and ended and now - ended >= KEEP:
            store.retire(k)
            done.append(k)
    return done


def receipt_holds(store):
    holds = {}
    for k in store.keys():
        p = store.facts(k) or {}
        hold = holds.setdefault(k.split("#")[0], {"numbers": set(), "refs": set()})
        hold["numbers"].add(p.get("number"))
        if p.get("headRefName"):
            hold["refs"].add("refs/heads/" + p["headRefName"])
    return holds


def load(store=None, archived=()):



    store = store or Store()
    out = {}
    for k in store.keys():
        p = store.facts(k)
        if p:
            out[k] = (p, {sha: {**v["commit"], "_read": v.get("fetched"), "_refresh_error": v.get("error")}
                          for sha, v in store.heads(k).items() if v.get("commit")})
    for k in archived:
        kept = store.generations(k)
        p = store.read(kept[-1][0]) if kept else None
        if p and k not in out:
            heads = store.read(kept[-1][1], {})
            out[k] = (p, {sha: {**v["commit"], "_read": v.get("fetched"), "_refresh_error": v.get("error")}
                          for sha, v in heads.items() if v.get("commit")})
    return out


def load_rules(store=None, now=None):

    store = store or Store()
    now = time.time() if now is None else now
    repos = store.rules().get("repos") or {}


    ok = lambda r: r and 0 <= now - (r.get("read") or 0) < 2 * RULES_EVERY
    return {k: receipts.required_checks((repos.get(k) or {}).get("rules")) if ok(repos.get(k)) else None for k in REPOS}


def load_expired_rules(store=None, now=None):


    store = store or Store()
    now = time.time() if now is None else now
    out = {}
    for k, r in (store.rules().get("repos") or {}).items():
        read = (r or {}).get("read")
        if read is not None and now - read >= 2 * RULES_EVERY:
            rules = receipts.required_checks(r.get("rules"))
            if rules is not None:
                out[k] = {"read": read, "rules": [list(rule) for rule in rules]}
    return out


def retain_expired_failures(record, p, hydrated, rec, expired):



    old = receipts.pr_record(record["pr"], p, hydrated, rec, expired["rules"])
    by_sha = {h["sha"]: h for h in old["heads"]}
    for h in record["heads"]:
        for mode in receipts.MODES:


            if record["policy"] == "tracked" and old["policy"] == "required" and h[mode]["outcome"] == "fail":
                h[mode] = {"outcome": "unknown", "reason": "required checks not evidenced", "failed": []}
            previous = by_sha[h["sha"]][mode]
            if previous["outcome"] == "fail":
                h[mode] = {"outcome": "fail", "reason": None,
                           "failed": sorted(set(h[mode]["failed"]) | set(previous["failed"]))}
        h["complete"] = False
    by_sha = {h["sha"]: h for h in record["heads"]}
    if record.get("r0"):
        h = by_sha[record["r0"]["sha"]]
        for mode in ("first_attempt", "first_completed"):
            record["r0"][mode] = h[mode]
    first = next((h for h in record["heads"] if h["sync"] is False
                  and h["first_completed"]["outcome"] in ("pass", "fail")), None)
    record["first_decided"] = None if first is None else {
        "sha": first["sha"], "outcome": first["first_completed"]["outcome"],
        "failed": first["first_completed"]["failed"], "family": first["family"]}
    record["history_complete"] = 0
    incomplete = {x for x in record["incomplete"] if not x.endswith(" heads without complete history")}
    incomplete.add("required checks expired; last known policy used only to retain failures")
    if record["heads"]:
        incomplete.add(f"{len(record['heads'])} heads without complete history")
    record["incomplete"] = sorted(incomplete)
    record["expired_policy"] = expired
    record["inputs"] = receipts.digest(record["inputs"], expired)
    return record


if __name__ == "__main__":
    print(json.dumps(refresh()))
