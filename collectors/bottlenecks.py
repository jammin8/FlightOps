import bisect
import heapq
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from store import epoch, iso  # noqa: E402

DAY = 86400
WINDOW_DAYS = 28
LEARNING = 20
DAY_SAMPLES = 5
RECENT_SAMPLES = 20
LATE_SHARE = 0.1
PARKED = {"waiting_ci": 2 * DAY, "in_ci": 2 * DAY, "in_review": 3 * DAY}
RECENT = DAY
HOLD = 1800
STEP = 1800
LOAD_DAY = DAY
WAVE = 2.0
WAVE_MIN = 10
SLOWING = 1.5
STUCK = 3.0
FLOOR = {"waiting_ci": 15 * 60, "in_ci": 15 * 60, "in_review": 12 * 3600}
JUDGED = tuple(FLOOR)
RANK = {"learning": 0, "clear": 1, "busy": 2, "slowing": 3, "stuck": 4}
NOT_WORK = ("skipped", "cancelled", "neutral", "stale")
LATE = {"waiting_ci": 2, "in_ci": 2, "in_review": 3}
BOTS = ("dependabot[bot]", "renovate[bot]", "github-actions[bot]", "dependabot", "renovate")


def gather(store, repos):
    found = {stage: [] for stage in JUDGED}
    if not repos:
        return found
    marks = ",".join("?" * len(repos))
    for row in store.db.execute(f"""SELECT j.repo, j.queued_at, j.started_at, j.completed_at, j.status, j.conclusion, a.started_at AS attempt_at
                                    FROM ci_jobs j LEFT JOIN ci_run_attempts a USING (repo, run_id, run_attempt)
                                    WHERE j.repo IN ({marks}) AND j.queued_at IS NOT NULL""", repos):
        queued, started, done = epoch(row["queued_at"]), epoch(row["started_at"]), epoch(row["completed_at"])
        if queued is not None and epoch(row["attempt_at"]) is not None:
            queued = max(queued, epoch(row["attempt_at"]))
        if row["conclusion"] in NOT_WORK or queued is None or started is None:
            continue
        found["waiting_ci"].append((queued, max(queued, started), row["repo"], None))
        if done is not None:
            found["in_ci"].append((started, max(started, done), row["repo"], None))
    reviews = {}
    for row in store.db.execute(f"""SELECT repo, number, type, at, actor FROM pr_timeline WHERE repo IN ({marks})
                                    AND type IN ('Review', 'ReadyForReviewEvent')""", repos):
        reviews.setdefault((row["repo"], row["number"]), []).append((row["type"], epoch(row["at"]), row["actor"]))
    for row in store.db.execute(f"SELECT repo, number, created_at, merged_at, closed_at, author FROM prs WHERE repo IN ({marks})", repos):
        if (row["author"] or "") in BOTS:
            continue
        marks_of = reviews.get((row["repo"], row["number"]), [])
        ready = [at for kind, at, _ in marks_of if kind == "ReadyForReviewEvent" and at is not None]
        enter = max(ready) if ready else epoch(row["created_at"])
        if enter is None:
            continue
        seen = [at for kind, at, who in marks_of if kind == "Review" and at is not None and at >= enter and who != row["author"]]
        ended = [t for t in (epoch(row["merged_at"]),) if t is not None and t >= enter]
        leave = min(seen + ended) if seen or ended else None
        if leave is not None:
            found["in_review"].append((enter, leave, row["repo"], f"{row['repo']}#{row['number']}"))
    return found


def p90(values):
    if len(values) < DAY_SAMPLES:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]


class Stage:
    def __init__(self, stage, items):
        self.stage = stage
        self.items = sorted(items, key=lambda i: i[0])
        self.enters = [i[0] for i in self.items]
        done = sorted((i[1], i[1] - i[0], i[3]) for i in self.items if i[1] is not None)
        self.leaves = [d[0] for d in done]
        self.waits = [d[1] for d in done]
        self.by_day = {}
        for at, wait, _ in done:
            self.by_day.setdefault(at // DAY, []).append(wait)
        self.cache = {}

    def normal(self, day):
        if day in self.cache:
            return self.cache[day]
        window = [self.by_day.get(d, []) for d in range(day - WINDOW_DAYS, day)]
        samples = sum(len(w) for w in window)
        daily = [v for v in (p90(w) for w in window) if v is not None]
        if samples < LEARNING:
            value = (None, samples)
        elif len(daily) >= 3:
            value = (sorted(daily)[(len(daily) - 1) // 3], samples)
        else:
            value = (p90([x for w in window for x in w]), samples)
        self.cache[day] = value
        return value

    def usual_load(self, day):
        start = (day - WINDOW_DAYS) * DAY
        return (bisect.bisect_left(self.enters, day * DAY) - bisect.bisect_left(self.enters, start)) / WINDOW_DAYS

    def judge(self, t, active, extra=None):
        normal, samples = self.normal(t // DAY)
        load = bisect.bisect_right(self.enters, t) - bisect.bisect_right(self.enters, t - LOAD_DAY)
        usual = self.usual_load(t // DAY)
        lo, hi = bisect.bisect_right(self.leaves, t - RECENT), bisect.bisect_right(self.leaves, t)
        recent = [w for w in self.waits[lo:hi] if w <= PARKED[self.stage]]
        slow_end = p90(recent) if len(recent) >= RECENT_SAMPLES else None
        ages = sorted([t - e for e in active] + list(extra or ()), reverse=True)
        oldest = ages[0] if ages else None
        facts = {"load_24h": load, "usual_load": round(usual, 1), "oldest_min": minutes(oldest), "slow_end_min": minutes(slow_end),
                 "normal_min": minutes(normal)}
        if normal is None:
            return "learning", facts, f"Learning normal ({samples} of {LEARNING} samples)"
        bar = max(normal, FLOOR[self.stage])
        need = LATE[self.stage] if extra is not None else max(LATE[self.stage], math.ceil(LATE_SHARE * load))
        late = ages[need - 1] if len(ages) >= need else 0
        many = f"{need} or more waiting over {span(late)}, normal is {span(normal)}"
        if late > STUCK * bar or (slow_end or 0) > STUCK * bar:
            return "stuck", facts, many if late > STUCK * bar else f"Slowest tenth waited {span(slow_end)}, normal is {span(normal)}"
        if late > SLOWING * bar:
            return "slowing", facts, many
        if (slow_end or 0) > SLOWING * bar:
            return "slowing", facts, f"Slowest tenth waited {span(slow_end)}, normal is {span(normal)}"
        if load >= WAVE * usual and load >= WAVE_MIN:
            return "busy", facts, f"{load} came in over 24h, about {round(load / usual) if usual else load}× usual, still flowing"
        return "clear", facts, "Normal"


def minutes(seconds):
    return None if seconds is None else round(seconds / 60, 1)


def span(seconds):
    s = int(seconds or 0)
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < DAY:
        return f"{s // 3600}h {(s % 3600) // 60}m" if s % 3600 >= 600 else f"{s // 3600}h"
    return f"{s // DAY}d"


def sweep(stage, times):
    leaving, active, index, items = [], [], 0, stage.items
    for t in times:
        while index < len(items) and items[index][0] <= t:
            enter, leave = items[index][0], items[index][1] if items[index][1] is not None else math.inf
            if leave > t:
                heapq.heappush(leaving, (leave, enter))
                bisect.insort(active, enter)
            index += 1
        while leaving and leaving[0][0] <= t:
            _, enter = heapq.heappop(leaving)
            del active[bisect.bisect_left(active, enter)]
        yield t, active[bisect.bisect_left(active, t - PARKED[stage.stage]):]


def series(stage, start, end, step=STEP):
    times = list(range(start - start % step, end + 1, step))
    return [(t,) + stage.judge(t, active) for t, active in sweep(stage, times)]


def episodes(stage, points, live_until=None):
    found, current = [], None
    for t, state, facts, _ in points:
        bad = state in ("slowing", "stuck")
        if bad and current is None:
            current = {"stage": stage.stage, "state": state, "start": t, "end": None, "last_bad": t}
        elif bad:
            current["state"] = max(current["state"], state, key=RANK.get)
            current["last_bad"] = t
        elif current is not None and t - current["last_bad"] > HOLD:
            current["end"] = current["last_bad"]
            found.append(current)
            current = None
    if current is not None:
        if live_until is not None and live_until - current["last_bad"] <= HOLD + STEP:
            current["end"] = None
        else:
            current["end"] = current["last_bad"]
        found.append(current)
    for e in found:
        bar = max(stage.normal(e["start"] // DAY)[0] or 0, FLOOR[stage.stage])
        end = e["end"] if e["end"] is not None else live_until
        lost = 0.0
        for enter, leave, _, _ in stage.items:
            stop = leave if leave is not None else end
            if stop is None or stop - enter > PARKED[stage.stage]:
                continue
            lost += max(0, min(stop, end + STEP) - max(enter + bar, e["start"]))
        e["hours_lost"] = round(lost / 3600, 1)
        e.pop("last_bad")
    return found


def daily(points, days):
    index = {d: i for i, d in enumerate(days)}
    worst = [None] * len(days)
    for t, state, _, _ in points:
        i = index.get(iso(t)[:10])
        if i is not None and (worst[i] is None or RANK[state] > RANK[worst[i]]):
            worst[i] = state
    return worst


def assess(store, repos, now, live, history_days=90, days=()):
    gathered = gather(store, repos)
    verdicts, log, by_day = {}, [], {}
    for name in JUDGED:
        stage = Stage(name, gathered[name])
        live_ages = [now - epoch(r["stage_since"]) for r in live if r["stage"] == name and epoch(r["stage_since"]) is not None
                     and now - epoch(r["stage_since"]) <= PARKED[name]]
        points = series(stage, now - history_days * DAY, now)
        held = [p for p in points if p[0] > now - HOLD]
        state, facts, reason = stage.judge(now, next(a for _, a in sweep(stage, [now])), live_ages)
        worst = max([(state, reason)] + [(s, r) for _, s, _, r in held if s not in ("learning",)], key=lambda x: RANK[x[0]])
        if state == "learning":
            worst = (state, reason)
        oldest_pr = None
        if live_ages:
            oldest = min((r for r in live if r["stage"] == name and epoch(r["stage_since"]) is not None), key=lambda r: epoch(r["stage_since"]))
            oldest_pr = oldest["id"]
        by_day[name] = daily(points + [(now, state, facts, reason)], days)
        found = episodes(stage, points + [(now, state, facts, reason)], live_until=now)
        ongoing = next((e for e in found if e["end"] is None), None)
        verdicts[name] = dict(facts, state=worst[0], reason=worst[1], since=iso(ongoing["start"]) if ongoing and worst[0] in ("slowing", "stuck") else None,
                              oldest_pr=oldest_pr)
        for e in found:
            log.append({"stage": name, "state": e["state"], "start": iso(e["start"]), "end": iso(e["end"]) if e["end"] is not None else None,
                        "hours_lost": e["hours_lost"]})
    log.sort(key=lambda e: (-e["hours_lost"], e["start"]))
    return verdicts, log, by_day


def replay(store, repos, start, end):
    gathered = gather(store, repos)
    days = {}
    for name in JUDGED:
        stage = Stage(name, gathered[name])
        for t, state, facts, reason in series(stage, start, end):
            row = days.setdefault((t // DAY, name), {"state": "learning", "slowing": None, "stuck": None, "oldest_min": 0, "slow_end_min": 0,
                                                    "normal_min": facts["normal_min"], "load": 0})
            row["state"] = max(row["state"], state, key=RANK.get)
            if state in ("slowing", "stuck") and row[state] is None:
                row[state] = iso(t)[11:16]
            row["oldest_min"] = max(row["oldest_min"], facts["oldest_min"] or 0)
            row["slow_end_min"] = max(row["slow_end_min"], facts["slow_end_min"] or 0)
            row["load"] = max(row["load"], facts["load_24h"])
    return days
