


import json
import io
import os
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from gh_samples import A, B, C, RULES, T0, check, commit, hydrated, iso, load, passing, pr, protection, receipts, status, suite

gf = load("ghfacts")
rc = load("receipts")
NOW = T0 + 86400


class FakeGitHub:




    def __init__(self, cost=1):
        self.prs, self.heads, self.cost, self.queries = {}, {}, cost, []
        self.errors, self.fail, self.rules = {}, None, {k: protection(*RULES) for k in gf.REPOS}

    def add(self, key, facts, heads=None):
        self.prs[key] = facts
        self.heads.update(heads or {})

    def __call__(self, q):
        self.queries.append(q)
        if self.fail:
            failure, self.fail = self.fail, None
            return {}, [], failure
        data = {"rateLimit": {"cost": self.cost, "remaining": 4000}}
        if "branchProtectionRules" in q:
            data.update({k: json.loads(json.dumps(v)) for k, v in self.rules.items()})
        elif "pullRequests(first: 100" in q:
            for alias in re.findall(r'(\w+): repository\(owner: "[^"]+", name: "[^"]+"\) \{ pullRequests', q):
                nodes = [{"number": p["number"], "state": p["state"], "updatedAt": p["updatedAt"], "mergedAt": p["mergedAt"],
                          "closedAt": p["closedAt"], "headRefOid": p["headRefOid"]}
                         for k, p in self.prs.items() if k.startswith(alias + "#")]
                data[alias] = {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
        elif "node(id:" in q:
            data["node"] = None
        for alias, repo, n in re.findall(r'(p\d+): repository\(owner: "[^"]+", name: "example-(\w+)"\) \{ pullRequest\(number: (\d+)\)', q):
            p = self.prs.get(f"{repo}#{n}")
            data[alias] = {"pullRequest": json.loads(json.dumps(p)) if p else None}
        parts = list(re.finditer(r'(\w+): repository\(owner: "[^"]+", name: "example-(\w+)"\) \{ h', q))
        for i, m in enumerate(parts):
            seg = q[m.start():parts[i + 1].start() if i + 1 < len(parts) else len(q)]
            data[m.group(1)] = {a: json.loads(json.dumps(self.heads.get(sha))) for a, sha in re.findall(r'(h\d+): object\(oid: "(\w+)"\)', seg)}
        errors = [{"message": msg, "path": list(path)} for path, msg in self.errors.items()
                  if path[0] in data and (len(path) == 1 or isinstance(data[path[0]], dict) and path[1] in data[path[0]])]
        return data, errors, None


def kinds(fake):
    out = []
    for q in fake.queries:
        out.append("rules" if "branchProtectionRules" in q else "discover" if "pullRequests(first: 100" in q
                   else "facts" if "pullRequest(number" in q else "page" if "node(id:" in q else "history")
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.store = gf.Store(tempfile.mkdtemp())
        self.gh = FakeGitHub()

    def refresh(self, now, clock=None):
        return gf.refresh(now, self.store, self.gh, clock or (lambda: 0), events={})

    def record(self, key="backend#1"):
        p, heads = gf.load(self.store)[key]
        return rc.pr_record(key, p, heads, None, gf.load_rules(self.store, NOW)[key.split("#")[0]])


class Refresh(Base):
    def test_a_new_open_pr_is_fetched_with_its_history_and_main_s_rules(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        s = self.refresh(NOW)
        self.assertEqual(kinds(self.gh), ["rules", "discover", "facts", "history"])
        self.assertEqual((s["fetched"], s["heads_read"], s["discovery_complete"]), (1, 1, True))
        self.assertEqual(gf.load_rules(self.store, NOW)["backend"], RULES)
        r = self.record()
        self.assertEqual((r["heads"][0]["first_attempt"]["outcome"], r["incomplete"]), ("pass", []))

    def test_an_unchanged_open_pr_isn_t_fetched_again_and_discovery_waits_15_minutes(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        self.refresh(NOW + 300)
        self.assertEqual(kinds(self.gh), ["rules", "discover", "facts", "history"])
        self.refresh(NOW + 901)
        self.assertEqual(kinds(self.gh)[4:], ["discover"])

    def test_a_merged_pr_is_fetched_once_an_hour_after_it_ended_then_sealed(self):
        self.gh.add("web#2", pr([commit(A)], number=2, state="MERGED", merged=NOW - 600, updated=NOW - 600), {A: passing(T0)})
        self.refresh(NOW)
        self.assertEqual(kinds(self.gh), ["rules", "discover"])
        self.refresh(NOW + 3000 + 901)
        self.assertEqual(kinds(self.gh)[2:], ["discover", "facts", "history"])
        self.assertTrue(gf.load(self.store)["web#2"][0]["settled"])
        self.refresh(NOW + 3000 + 2 * 901)
        self.assertEqual(kinds(self.gh)[-1], "discover")

    def test_an_old_pr_outside_the_window_isn_t_discovered(self):
        self.gh.add("ios#3", pr([commit(A)], number=3, updated=NOW - 29 * 86400), {A: passing(T0)})
        self.assertEqual(self.refresh(NOW)["discovered"], 0)

    def test_only_trailers_are_kept_from_commit_messages(self):
        body = "Why this change.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
        self.gh.add("backend#1", pr([commit(A, body=body)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        c = gf.load(self.store)["backend#1"][0]["commits"]["nodes"][0]["commit"]
        self.assertEqual(c["messageBody"], "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")

    def test_the_summary_says_what_is_still_waiting(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.gh.add("backend#2", pr([commit(B)], number=2, updated=NOW - 120), {B: passing(T0)})
        ticks = iter([0, 0, 0])
        s = gf.refresh(NOW, self.store, self.gh, lambda: next(ticks, gf.RUNTIME + 1))
        self.assertEqual((s["fetched"], s["unfetched"], s["waiting"]), (0, ["backend#1", "backend#2"], 2))
        self.assertGreater(s["held"], 0)


class ReceiptOnlyHeads(Base):

    def setUp(self):
        super().setUp()
        self.p = pr([commit(C)], updated=NOW - 60, timeline=[{
            "__typename": "HeadRefForcePushedEvent", "createdAt": iso(T0 + 120),
            "beforeCommit": commit(B), "afterCommit": {"oid": C}}])
        self.p["isCrossRepository"] = False
        self.events = {"backend": receipts(opened={1: A}, pushes=[("feat", A, T0 - 60), ("feat", B, T0 + 60)])}
        self.gh.add("backend#1", self.p, {
            A: {**commit(A, body="Private body not retained.\nCo-Authored-By: Codex <codex@example.test>"),
                **passing(T0 + 5, state="FAILURE")}, B: passing(T0 + 60), C: passing(T0 + 120)})

    def fetch(self):
        return gf.refresh(NOW, self.store, self.gh, lambda: 0, events=self.events)

    def test_opening_head_is_fetched_published_and_verified_after_force_push(self):
        ev, pf, vf = load("ghevents"), load("prfacts"), load("verify")
        events_dir = os.path.join(self.store.d, "events")
        os.makedirs(events_dir)

        rows = [{"id": "1", "repo": "backend", "type": "PullRequestEvent", "action": "opened", "number": 1,
                 "head_sha": A, "t": T0},
                {"id": "2", "repo": "backend", "type": "PushEvent", "ref": "refs/heads/feat", "head": A,
                 "before": "0" * 40, "t": T0 - 60},
                {"id": "3", "repo": "backend", "type": "PushEvent", "ref": "refs/heads/feat", "head": B,
                 "before": A, "t": T0 + 60}]
        with open(os.path.join(events_dir, "receipts.jsonl"), "w") as f:
            f.writelines(json.dumps(x) + "\n" for x in rows)
        self.events = ev.load_receipts(events_dir)
        published = os.path.join(self.store.d, "flightops-receipt-publication.json")
        def refresh(now, store):
            return gf.refresh(now, store, self.gh, lambda: 0, events=self.events)
        pf.run(NOW, events_dir, self.store, published, poll=lambda *_: {}, refresh=refresh)
        self.assertEqual(set(self.store.heads("backend#1")), {A, B, C})
        self.assertEqual(kinds(self.gh), ["rules", "discover", "facts", "history"])
        stored = self.store.heads("backend#1")[A]["commit"]
        self.assertEqual(stored["messageBody"], "Co-Authored-By: Codex <codex@example.test>")
        with open(published) as f:
            data = json.load(f)
        r = data["prs"]["backend#1"]
        self.assertEqual((data["schema"], data["deriver"]), ("pr-facts/2", 3))
        self.assertEqual((r["r0"]["sha"], r["r0"]["status"], r["r0"]["family"], r["r0"]["sync"]),
                         (A, "verified", "codex", False))
        self.assertEqual((r["r0"]["first_attempt"]["outcome"], r["first_decided"]["sha"], r["history_complete"]),
                         ("fail", A, 3))
        buf = io.StringIO()
        with patch.object(gf, "gh", side_effect=AssertionError("offline verify attempted GitHub I/O")):
            code = vf.main(["--offline", "--json"], published, events_dir, self.store, NOW, buf)
        result = json.loads(buf.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["results"][0]["verdict"], "verified")

    def test_missing_hydration_cannot_pass_or_finish_sealing(self):
        self.p.update(state="MERGED", mergedAt=iso(NOW - 7200), closedAt=iso(NOW - 7200))
        self.gh.heads.pop(A)
        s = self.fetch()
        self.assertIn(A, self.store.heads("backend#1"))
        self.assertEqual((s["heads_read"], self.store.heads("backend#1")[A]["error"]), (3, "object not returned"))
        self.assertFalse(self.store.facts("backend#1").get("settled"))
        p, hyd = gf.load(self.store)["backend#1"]
        r = rc.pr_record("backend#1", p, hyd, self.events["backend"], RULES)
        self.assertEqual((r["r0"]["first_attempt"]["outcome"], r["r0"]["family"], r["r0"]["sync"]),
                         ("unknown", None, None))
        self.assertTrue(r["incomplete"])

    def test_push_only_head_hydrates_only_with_same_repo_lifetime_evidence(self):
        self.events["backend"]["opened"] = {}
        self.events["backend"]["pushes"][0]["t"] = T0 + 5
        self.assertEqual(set(gf.heads_to_read(self.p, {}, NOW, self.events["backend"])), {A, B, C})
        self.fetch()
        self.assertEqual(set(self.store.heads("backend#1")), {A, B, C})
        r = rc.pr_record("backend#1", *gf.load(self.store)["backend#1"], self.events["backend"], RULES)
        self.assertEqual((r["r0"]["sha"], r["r0"]["status"]), (A, "candidate"))

    def test_hydration_queue_rejects_fork_reuse_and_unrelated_receipts(self):
        self.events["backend"]["opened"] = {}
        for cross, ref, at in ((True, "feat", T0 + 5), (None, "feat", T0 + 5),
                               (False, "other", T0 + 5), (False, "feat", T0 - 1), (False, "feat", NOW + 1)):
            with self.subTest(cross=cross, ref=ref, at=at):
                self.p["isCrossRepository"] = cross
                events = receipts(pushes=[(ref, A, at)])
                self.assertNotIn(A, gf.heads_to_read(self.p, {}, NOW, events))

    def test_hydration_query_requests_real_commit_metadata_and_repo_identity(self):
        fields = gf.head_fields(1)
        for field in ("committedDate", "messageHeadline", "messageBody", "parents { totalCount }"):
            self.assertIn(field, fields)
        self.assertIn("isCrossRepository", gf.pr_fields(1))

    def test_receipt_arriving_after_sealing_still_gets_hydrated(self):
        self.p.update(state="MERGED", mergedAt=iso(NOW - 7200), closedAt=iso(NOW - 7200))
        events = self.events
        self.events = {}
        self.fetch()
        self.assertTrue(self.store.facts("backend#1")["settled"])
        self.assertNotIn(A, self.store.heads("backend#1"))
        self.events = events
        s = self.fetch()
        self.assertEqual(s["heads_read"], 1)
        self.assertIn(A, self.store.heads("backend#1"))
        self.assertEqual(kinds(self.gh)[4:], ["history"])
        self.assertEqual(self.fetch()["heads_read"], 0)

    def test_late_receipt_budget_hold_is_reported_without_a_request_or_fake_history(self):
        self.p.update(state="MERGED", mergedAt=iso(NOW - 7200), closedAt=iso(NOW - 7200))
        events, self.events = self.events, {}
        self.fetch()
        self.events = events
        with open(os.path.join(self.store.d, "cost.jsonl"), "a") as f:
            f.write(json.dumps({"id": "hold", "t": NOW, "what": "fixture", "reserved": gf.CAP}) + "\n")
        count = len(self.gh.queries)
        s = self.fetch()
        self.assertEqual((len(self.gh.queries), s["heads_read"], s["due_heads"]), (count, 0, 1))
        self.assertGreater(s["held"], 0)
        self.assertNotIn(A, self.store.heads("backend#1"))

    def test_expired_policy_retains_receipt_head_failure_without_unknown_sync_becoming_work(self):
        p, hyd = self.p, {A: passing(T0 + 5, state="FAILURE")}
        r = rc.pr_record("backend#1", p, hyd, self.events["backend"], None)
        gf.retain_expired_failures(r, p, hyd, self.events["backend"], {"read": T0 - 3 * 86400, "rules": RULES})
        self.assertIsNotNone(r["r0"])
        self.assertEqual((r["r0"]["first_attempt"]["outcome"], r["r0"]["sync"], r["history_complete"]),
                         ("fail", None, 0))
        self.assertIsNone(r["first_decided"])


class OpenIntervalPipeline(unittest.TestCase):

    def pipeline(self, offset, cached=False):
        ev, pf, vf = load("ghevents"), load("prfacts"), load("verify")
        now = T0 + 500
        p = pr([commit(C)], updated=now, timeline=[
            {"__typename": "ClosedEvent", "createdAt": iso(T0 + 100)},
            {"__typename": "ReopenedEvent", "createdAt": iso(T0 + 300)}])
        p["isCrossRepository"] = False
        gh = FakeGitHub()
        bad = {**commit(A, committed=T0 + offset), **passing(T0 + offset, state="FAILURE")}
        gh.add("backend#1", p, {A: bad, C: passing(T0 + 450)})
        with tempfile.TemporaryDirectory() as root:
            events_dir = os.path.join(root, "events")
            os.makedirs(events_dir)
            row = {"repo": "backend", "type": "PushEvent", "id": "9", "t": T0 + offset,
                   "ref": "refs/heads/feat", "before": B, "head": A}
            with open(os.path.join(events_dir, "receipts.jsonl"), "w") as f:
                f.write(json.dumps(row) + "\n")
            events = ev.load_receipts(events_dir)
            store = gf.Store(os.path.join(root, "facts"))
            if cached:
                store.write(store.path("backend#1", heads=True), {A: {"fetched": now, "commit": bad}})
            output = os.path.join(root, "flightops-receipt-c1-publication.json")
            def refresh(at, store):
                return gf.refresh(at, store, gh, lambda: 0, events=events)
            pf.run(now, events_dir, store, output, poll=lambda *_: {}, refresh=refresh)
            with open(output) as f:
                pub = json.load(f)
            r = pub["prs"]["backend#1"]
            if offset == 200:
                self.assertFalse(any('object(oid: "' + A + '")' in q for q in gh.queries))
                self.assertEqual({h["sha"] for h in r["heads"]}, {C})
                self.assertEqual((r["first_decided"]["sha"], r["first_decided"]["outcome"]), (C, "pass"))
                if not cached:
                    self.assertNotIn(A, store.heads("backend#1"))
            else:
                self.assertTrue(any('object(oid: "' + A + '")' in q for q in gh.queries))
                self.assertEqual({h["sha"] for h in r["heads"]}, {A, C})
                self.assertEqual((r["first_decided"]["sha"], r["first_decided"]["outcome"]), (A, "fail"))
            self.assertEqual((pub["schema"], pub["deriver"]), ("pr-facts/2", 3))
            buf = io.StringIO()
            code = vf.main(["--offline", "--json"], output, events_dir, store, now, buf)
            self.assertEqual(code, 0, buf.getvalue())
            self.assertEqual(json.loads(buf.getvalue())["results"][0]["verdict"], "verified")

    def test_closed_interval_push_absent_from_fetch_publication_and_first_decided(self):
        self.pipeline(200)

    def test_matching_open_interval_push_hydrates_and_preserves_its_failure(self):
        self.pipeline(50)

    def test_cached_closed_interval_head_does_not_reenter_publication(self):
        self.pipeline(200, cached=True)


class Partial(Base):


    def test_a_head_answered_with_an_error_keeps_no_partial_history(self):
        h = hydrated(suite(T0, check("lint", "SUCCESS", T0, i=1)), suite(T0, workflow="other", run_id=8))
        h["checkSuites"]["nodes"][1]["checkRuns"] = None
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: h})
        self.gh.errors[("backend", "h0", "checkSuites", "nodes", 1, "checkRuns")] = "Something went wrong while executing your query"
        self.refresh(NOW)
        stored = self.store.heads("backend#1")[A]
        self.assertEqual((stored.get("commit"), stored["error"]), (None, "GitHub error"))
        r = self.record()
        self.assertEqual((r["history_complete"], r["heads"][0]["first_attempt"]["outcome"]), (0, "unknown"))

    def test_a_null_connection_without_an_error_isn_t_complete_either(self):
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), check("integration", "SUCCESS", T0, i=2)),
                     suite(T0, workflow="lint", run_id=8))
        h["checkSuites"]["nodes"][1]["checkRuns"] = None
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: h})
        self.refresh(NOW)
        r = self.record()
        self.assertEqual((r["heads"][0]["first_attempt"]["outcome"], r["history_complete"]), ("unknown", 0))

    def test_an_error_keeps_the_last_good_history_with_its_time_and_retries(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        self.gh.add("backend#1", pr([commit(A)], updated=NOW + 100), {A: passing(T0, state="FAILURE")})
        self.gh.errors[("backend", "h0")] = "timeout"
        self.refresh(NOW + 901)
        stored = self.store.heads("backend#1")[A]
        self.assertEqual((stored["fetched"], stored["error"]), (int(NOW), "GitHub error"))
        self.assertIn("history to read again", self.record()["incomplete"])
        self.gh.errors.clear()
        self.refresh(NOW + 901 + gf.RECHECK)
        r = self.record()
        self.assertEqual((r["heads"][0]["latest"]["outcome"], r["incomplete"]), ("fail", []))

    def test_a_merged_pr_with_a_partial_head_isn_t_sealed(self):
        self.gh.add("web#2", pr([commit(A)], number=2, state="MERGED", merged=NOW - 7200, updated=NOW - 7200), {A: passing(T0)})
        self.gh.errors[("web", "h0")] = "Something went wrong"
        self.refresh(NOW)
        self.assertFalse(gf.load(self.store)["web#2"][0].get("settled"))
        self.gh.errors.clear()
        self.refresh(NOW + gf.RECHECK)
        self.assertTrue(gf.load(self.store)["web#2"][0]["settled"])

    def test_a_pr_answered_with_an_error_isn_t_stored_and_is_retried(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.gh.errors[("p0", "pullRequest", "commits")] = "Something went wrong"
        s = self.refresh(NOW)
        self.assertEqual((s["fetched"], list(s["retry"])), (0, ["backend#1"]))
        self.assertIsNone(self.store.facts("backend#1"))
        self.gh.errors.clear()
        self.assertEqual(self.refresh(NOW + 60)["fetched"], 1)

    def test_an_unusable_answer_stores_nothing(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        self.gh.add("backend#1", pr([commit(A)], updated=NOW + 100), {A: passing(T0, state="FAILURE")})
        self.gh.fail = "Bad gateway"
        s = self.refresh(NOW + 901)
        self.assertFalse(s["discovery_complete"])
        self.assertEqual(self.store.heads("backend#1")[A]["fetched"], int(NOW))


class Lifecycle(Base):


    def test_a_rerun_after_the_pr_changed_is_read(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        rerun = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=70), check("integration", "SUCCESS", T0, i=71),
                               check("fast", "FAILURE", T0 + 600, i=72)))
        self.gh.add("backend#1", pr([commit(A)], updated=NOW + 100), {A: rerun})
        self.refresh(NOW + 901)
        self.assertEqual(kinds(self.gh)[-2:], ["facts", "history"])
        self.assertEqual(self.record()["heads"][0]["latest"]["outcome"], "fail")

    def test_the_current_head_is_read_again_every_hour_for_reruns(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        self.gh.heads[A] = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=70), check("integration", "SUCCESS", T0, i=71),
                                          check("fast", "FAILURE", T0 + 600, i=72)))
        self.refresh(NOW + 1800)
        self.assertEqual(kinds(self.gh)[-1], "discover")
        self.refresh(NOW + gf.CURRENT_EVERY)
        self.assertEqual(self.record()["heads"][0]["latest"]["outcome"], "fail")

    def test_an_empty_history_is_read_again(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: hydrated()})
        self.refresh(NOW)
        self.gh.heads[A] = passing(T0)
        self.refresh(NOW + gf.RECHECK)
        self.assertEqual(self.record()["heads"][0]["first_attempt"]["outcome"], "pass")

    def test_unfinished_checks_are_read_again_every_15_minutes(self):
        running = hydrated(suite(T0, check("fast", None, T0, status="IN_PROGRESS")))
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: running})
        self.refresh(NOW)
        self.gh.heads[A] = passing(T0)
        self.refresh(NOW + 300)
        self.assertEqual(len(self.gh.queries), 4)
        self.refresh(NOW + gf.RECHECK)
        self.assertEqual(kinds(self.gh)[-1], "history")

    def test_a_page_that_can_t_be_read_keeps_the_pr_in_the_retry_queue(self):
        p = pr([commit(A)], updated=NOW - 60)
        p["commits"]["pageInfo"] = {"hasNextPage": True, "endCursor": "x"}
        self.gh.add("backend#1", p, {A: passing(T0)})
        s = self.refresh(NOW)
        self.assertEqual((s["retry"]["backend#1"]["why"], self.store.facts("backend#1")), ("a page couldn't be read", None))
        self.refresh(NOW + 60)
        self.assertEqual(kinds(self.gh).count("page"), 2)

    def following(self, pages, page_info):
        calls = []

        def answer(q):
            calls.append(q)
            return {"node": {"commits": pages[min(len(calls), len(pages)) - 1]}}, [], None
        p = pr([commit(A)], updated=NOW - 60)
        p["commits"]["pageInfo"] = page_info
        f = gf.Fetcher(self.store, NOW, query=answer, clock=lambda: 0)
        return gf.follow(f, p, "backend#1"), calls, f, p

    def test_a_repeated_cursor_stops_after_one_page_with_one_error(self):
        page = {"pageInfo": {"hasNextPage": True, "endCursor": "same"}, "nodes": [{"commit": commit(B)}]}
        done, calls, f, p = self.following([page], {"hasNextPage": True, "endCursor": "same"})
        self.assertEqual((done, len(calls), len(f.errors)), (False, 1, 1))
        self.assertLess(self.store.spent(NOW), 5)
        self.assertEqual(len(p["commits"]["nodes"]), 2)

    def test_a_missing_cursor_never_requests_the_first_page_again(self):
        done, calls, f, _ = self.following([], {"hasNextPage": True, "endCursor": None})
        self.assertEqual((done, calls, len(f.errors)), (False, [], 1))

    def test_a_page_that_repeats_rows_adds_each_once(self):
        first = {"pageInfo": {"hasNextPage": True, "endCursor": "b"}, "nodes": [{"commit": commit(A)}, {"commit": commit(B)}]}
        last = {"pageInfo": {"hasNextPage": False, "endCursor": "c"}, "nodes": [{"commit": commit(B)}, {"commit": commit(C)}]}
        done, calls, _, p = self.following([first, last], {"hasNextPage": True, "endCursor": "a"})
        self.assertEqual((done, len(calls)), (True, 2))
        self.assertEqual([n["commit"]["oid"] for n in p["commits"]["nodes"]], [A, B, C])

    def test_a_repeated_discovery_cursor_stops_and_is_incomplete(self):
        calls = []

        def answer(q):
            calls.append(q)
            conn = {"pageInfo": {"hasNextPage": True, "endCursor": "same"},
                    "nodes": [{"number": 1, "state": "OPEN", "updatedAt": iso(NOW), "mergedAt": None, "closedAt": None, "headRefOid": A}]}
            return {"backend": {"pullRequests": conn}}, [], None
        state = {}
        found = gf.discover(gf.Fetcher(self.store, NOW, query=answer, clock=lambda: 0), state)
        self.assertEqual((len(calls), len(found), state["discovery_complete"]), (2, 1, False))

    def test_a_missing_discovery_cursor_rereads_no_first_page(self):
        calls = []

        def answer(q):
            calls.append(q)
            conn = {"pageInfo": {"hasNextPage": True, "endCursor": None},
                    "nodes": [{"number": 1, "state": "OPEN", "updatedAt": iso(NOW), "mergedAt": None, "closedAt": None, "headRefOid": A}]}
            return {"backend": {"pullRequests": conn}}, [], None
        state = {}
        gf.discover(gf.Fetcher(self.store, NOW, query=answer, clock=lambda: 0), state)
        self.assertEqual((len(calls), state["discovery_complete"]), (1, False))

    def test_a_sealed_pr_that_reopens_is_fetched_again(self):
        self.gh.add("web#2", pr([commit(A)], number=2, state="CLOSED", merged=NOW - 7200, updated=NOW - 7200), {A: passing(T0)})
        self.refresh(NOW)
        self.assertTrue(gf.load(self.store)["web#2"][0]["settled"])
        reopened = pr([commit(A), commit(B)], number=2, state="OPEN", updated=NOW + 100)
        self.gh.add("web#2", reopened, {B: passing(T0 + 60)})
        self.refresh(NOW + 901)
        p = gf.load(self.store)["web#2"][0]
        self.assertEqual((p["state"], p["headRefOid"], p.get("settled")), ("OPEN", B, None))


class Budget(Base):


    def seed(self, points):
        with open(os.path.join(self.store.d, "cost.jsonl"), "w") as f:
            f.write(json.dumps({"id": "seed", "t": NOW - 100, "what": "earlier", "reserved": points}) + "\n")

    def test_two_fetchers_can_t_both_take_the_last_point(self):
        self.seed(gf.CAP - 1)
        a, b = gf.Fetcher(self.store, NOW, self.gh), gf.Fetcher(gf.Store(self.store.d), NOW, self.gh)
        got = []
        threads = [threading.Thread(target=lambda f=f: got.append(f.run("x", "", "discover", 1)[0])) for f in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for g in got if g is not None), 1)
        self.assertEqual(self.store.spent(NOW), gf.CAP)

    def test_a_timeout_keeps_its_reservation(self):
        self.gh.fail = "timeout"
        f = gf.Fetcher(self.store, NOW, self.gh)
        self.assertEqual(f.run("x", "", "history", 30), (None, None))
        self.assertEqual(self.store.spent(NOW), gf.COST["history"](30))

    def test_an_exception_keeps_its_reservation(self):
        def broken(q):
            raise OSError("gh vanished")
        gf.Fetcher(self.store, NOW, broken).run("x", "", "facts", 10)
        self.assertEqual(self.store.spent(NOW), gf.COST["facts"](10))

    def test_the_answer_s_cost_replaces_the_reservation(self):
        gf.Fetcher(self.store, NOW, self.gh).run("x", "", "history", 30)
        self.assertEqual(self.store.spent(NOW), 1)

    def test_a_refresh_never_admits_past_the_cap(self):
        self.seed(gf.CAP - 3)
        for n in range(1, 30):
            self.gh.add(f"backend#{n}", pr([commit(f"{n:040x}")], number=n, updated=NOW - n), {f"{n:040x}": passing(T0)})
        s = self.refresh(NOW)
        self.assertLessEqual(self.store.spent(NOW), gf.CAP)
        self.assertGreater(s["held"], 0)

    def test_reservations_cover_the_documented_cost(self):
        self.assertEqual(gf.COST["history"](30), 6)
        self.assertGreaterEqual(gf.COST["facts"](10), 1)
        self.assertEqual(gf.COST["discover"](3), 1)

    def test_the_hour_rolls_over(self):
        self.seed(gf.CAP)
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.assertEqual(self.refresh(NOW)["fetched"], 0)
        self.assertEqual(self.gh.queries, [])
        self.assertEqual(self.refresh(NOW + 3600)["fetched"], 1)

    def test_the_ledger_is_compacted(self):
        with open(os.path.join(self.store.d, "cost.jsonl"), "w") as f:
            for i in range(4000):
                f.write(json.dumps({"id": f"o{i}", "t": NOW - 86400, "what": "old", "reserved": 1}) + "\n")
        self.store.reserve("x", 1, NOW)
        self.assertLess(os.path.getsize(os.path.join(self.store.d, "cost.jsonl")), 1000)


class Ledger(Base):


    def write(self, *lines):
        with open(os.path.join(self.store.d, "cost.jsonl"), "w") as f:
            for l in lines:
                f.write(l if isinstance(l, str) else json.dumps(l) + "\n")

    def test_a_recent_legacy_line_is_a_charge_and_doesn_t_break_collection(self):
        self.write({"t": NOW - 10, "what": "discover", "cost": 1})
        self.assertEqual(self.store.spent(NOW), 1)
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.assertEqual(self.refresh(NOW)["fetched"], 1)

    def test_an_expired_legacy_line_is_no_charge(self):
        self.write({"t": NOW - 7200, "what": "discover", "cost": 40})
        self.assertEqual(self.store.spent(NOW), 0)
        self.assertIsNotNone(self.store.reserve("x", 1, NOW))

    def test_mixed_formats(self):
        self.write({"t": NOW - 10, "what": "old", "cost": 3}, {"id": "r1", "t": NOW - 5, "what": "new", "reserved": 5},
                   {"id": "r1", "cost": 2, "error": None}, {"id": "r2", "t": NOW - 4, "what": "new", "reserved": 5})
        self.assertEqual(self.store.spent(NOW), 3 + 2 + 5)

    def test_a_write_cut_short_is_charged_and_repaired_under_the_lock(self):
        self.write({"id": "full", "t": NOW - 10, "what": "x", "reserved": gf.CAP}, '{"id":"interrupted"')
        self.assertEqual(self.store.spent(NOW), gf.CAP + gf.MAX_QUERY)
        self.assertIsNone(self.store.reserve("x", 1, NOW))
        with open(os.path.join(self.store.d, "cost.jsonl")) as f:
            lines = [json.loads(l) for l in f]
        self.assertEqual([l.get("what") for l in lines], ["x", "unreadable ledger line"])
        self.assertEqual(self.store.spent(NOW + 3601), 0)

    def test_compaction_keeps_recent_legacy_charges(self):
        self.write(*([{"t": NOW - 86400, "what": "old", "cost": 1}] * 6000 + [{"t": NOW - 10, "what": "old", "cost": 7}]))
        self.store.reserve("x", 1, NOW)
        self.assertEqual(self.store.spent(NOW), 7 + 1)
        self.assertLess(os.path.getsize(os.path.join(self.store.d, "cost.jsonl")), 1000)


class Held(Base):



    def test_a_held_run_keeps_the_last_read_time_and_reports_due_heads(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        first = self.refresh(NOW)
        with open(os.path.join(self.store.d, "cost.jsonl"), "a") as f:
            f.write(json.dumps({"id": "hold", "t": NOW + 3600, "what": "x", "reserved": gf.CAP}) + "\n")
        s = self.refresh(NOW + 3601)
        self.assertEqual((s["queries"], s["held"] > 0, s["errors"]), (0, True, []))
        self.assertEqual(s["last_clean_read"], first["last_clean_read"])
        self.assertEqual((s["due_heads"], s["due_prs"]), (1, ["backend#1"]))


class Rules(Base):
    def test_null_rule_connections_aren_t_stored_as_an_empty_policy(self):
        self.gh.rules["backend"] = {"branchProtectionRules": None, "rulesets": None}
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        self.assertIsNone(gf.load_rules(self.store, NOW)["backend"])
        r = self.record()
        self.assertEqual((r["policy_source"], r["heads"][0]["first_attempt"]["reason"]), ("observed", "required checks not evidenced"))

    def test_rules_stored_in_an_older_shape_are_read_again(self):
        self.store.write(os.path.join(self.store.d, "rules.json"),
                         {"fetched": NOW, "repos": {k: {"read": NOW, "rules": protection(*RULES)} for k in gf.REPOS}})
        self.refresh(NOW + 60)
        self.assertEqual(kinds(self.gh)[0], "rules")
        self.assertEqual(self.store.rules()["shape"], gf.RULES_SHAPE)


class Unfinished(Base):
    def test_a_pending_commit_status_is_read_again_after_15_minutes(self):
        h = passing(T0)
        h["status"] = {"contexts": [status("integration", "PENDING", T0)]}
        p = pr([commit(A)], updated=NOW - 60)
        p["fetched_at"] = NOW
        heads = {A: {"fetched": int(NOW), "commit": h}}
        self.assertEqual(gf.heads_to_read(p, heads, NOW + 901), [A])
        self.assertEqual(gf.heads_to_read(p, heads, NOW + 60), [])

    def test_a_head_only_a_push_receipt_names_is_scheduled(self):
        p = pr([commit(A), commit(B)], updated=NOW - 60)
        rec = receipts(pushes=[("feat", A, T0 + 5)])
        self.assertEqual(gf.heads_to_read(p, {}, NOW), [B])
        self.assertEqual(sorted(gf.heads_to_read(p, {}, NOW, rec)), [A, B])


class RulesPerRepo(Base):
    def test_a_repo_whose_rules_failed_is_retried_after_15_minutes_not_a_day(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        old = self.store.rules()
        old["repos"]["backend"]["read"] = NOW - 90000
        old["fetched"] = NOW - 90000
        self.store.write(os.path.join(self.store.d, "rules.json"), old)
        self.gh.rules["backend"] = {"branchProtectionRules": None, "rulesets": None}
        self.refresh(NOW + 100)
        self.assertEqual(self.store.rules()["repos"]["backend"]["read"], NOW - 90000)
        n = kinds(self.gh).count("rules")
        self.refresh(NOW + 200)
        self.assertEqual(kinds(self.gh).count("rules"), n)
        self.refresh(NOW + 100 + gf.RECHECK)
        self.assertEqual(kinds(self.gh).count("rules"), n + 1)


class RulesFreshness(Base):
    def rules_at(self, now):
        with patch.object(gf.time, "time", return_value=now):
            return gf.load_rules(self.store)

    def test_rules_expire_at_two_periods_even_when_the_cache_has_not_changed(self):
        self.refresh(NOW)
        before = self.store.rules()
        for age in (0, gf.RULES_EVERY, 2 * gf.RULES_EVERY - 1):
            with self.subTest(age=age):
                self.assertEqual(self.rules_at(NOW + age)["backend"], RULES)
        for age in (2 * gf.RULES_EVERY, 3 * gf.RULES_EVERY):
            with self.subTest(age=age):
                self.assertIsNone(self.rules_at(NOW + age)["backend"])
        self.assertEqual(self.store.rules(), before)

    def test_a_failed_refresh_does_not_extend_the_rules_lifetime(self):
        self.refresh(NOW)
        self.gh.fail = "fixture: rules unavailable"
        now = NOW + 3 * gf.RULES_EVERY
        f = gf.Fetcher(self.store, now, self.gh, lambda: 0)
        gf.refresh_rules(f)
        self.assertEqual(self.store.rules()["fetched"], NOW)
        self.assertEqual(f.errors, ["rules: fixture: rules unavailable"])
        self.assertIsNone(self.rules_at(now)["backend"])

    def test_a_budget_held_refresh_does_not_extend_the_rules_lifetime(self):
        self.refresh(NOW)
        now = NOW + 3 * gf.RULES_EVERY
        with open(os.path.join(self.store.d, "cost.jsonl"), "a") as f:
            f.write(json.dumps({"id": "hold", "t": now, "what": "fixture", "reserved": gf.CAP}) + "\n")
        n = len(self.gh.queries)
        f = gf.Fetcher(self.store, now, self.gh, lambda: 0)
        gf.refresh_rules(f)
        self.assertEqual((f.held, len(self.gh.queries)), (1, n))
        self.assertEqual(self.store.rules()["fetched"], NOW)
        self.assertIsNone(self.rules_at(now)["backend"])

    def test_each_repo_uses_its_own_read_time_and_future_evidence_is_unknown(self):
        self.refresh(NOW)
        old = self.store.rules()
        old["repos"]["web"]["read"] = NOW - 2 * gf.RULES_EVERY
        old["repos"]["ios"]["read"] = NOW + 1
        self.store.write(os.path.join(self.store.d, "rules.json"), old)
        rules = self.rules_at(NOW)
        self.assertEqual(rules["backend"], RULES)
        self.assertIsNone(rules["web"])
        self.assertIsNone(rules["ios"])


class ExpiredRules(Base):
    def test_expired_policy_keeps_provenance_separate_from_fresh_policy(self):
        self.refresh(NOW)
        before = self.store.rules()
        for age in (-0.001, 0, 172799.999, 172800, 259200):
            with self.subTest(age=age):
                expired = gf.load_expired_rules(self.store, NOW + age)
                if age >= 172800:
                    self.assertEqual(expired["backend"], {"read": NOW, "rules": [list(r) for r in RULES]})
                    self.assertIsNone(gf.load_rules(self.store, NOW + age)["backend"])
                else:
                    self.assertNotIn("backend", expired)
        with patch.object(gf.time, "time", return_value=NOW + 172800):
            self.assertIn("backend", gf.load_expired_rules(self.store))
        self.assertEqual(self.store.rules(), before)

    def test_each_repo_requires_a_past_read_and_whole_policy(self):
        self.refresh(NOW)
        cache = self.store.rules()
        cache["repos"]["web"]["rules"] = {"branchProtectionRules": None, "rulesets": None}
        cache["repos"]["ios"].pop("read")
        self.store.write(os.path.join(self.store.d, "rules.json"), cache)
        self.assertEqual(set(gf.load_expired_rules(self.store, NOW + 172800)), {"backend"})


class Overlap(Base):
    def setUp(self):
        super().setUp()
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})

    def test_an_older_answer_never_replaces_a_newer_head(self):
        store, gh = self.store, self.gh

        def old_answer(q):
            data, errors, failure = gh(q)
            newer = pr([commit(A), commit(B)], updated=NOW + 100)
            gh.add("backend#1", newer, {B: passing(T0 + 60)})
            gf.fetch_facts(gf.Fetcher(store, NOW + 200, query=gh, clock=lambda: 0), [("backend#1", "new")], {})
            return data, errors, failure
        gf.fetch_facts(gf.Fetcher(store, NOW + 100, query=old_answer, clock=lambda: 0), [("backend#1", "new")], {})
        stored = store.facts("backend#1")
        self.assertEqual((stored["headRefOid"], stored["fetched_at"]), (B, int(NOW + 200)))

    def test_an_older_fetch_is_not_reported_as_done(self):
        self.store.write(self.store.path("backend#1"), {"fetched_at": int(NOW + 500), "number": 1})
        state = {}
        done = gf.fetch_facts(gf.Fetcher(self.store, NOW, query=self.gh, clock=lambda: 0), [("backend#1", "new")], state)
        self.assertEqual((done, self.store.facts("backend#1")["fetched_at"]), ([], int(NOW + 500)))

    def test_refreshes_take_turns_across_processes(self):
        with gf.transaction(self.store):
            with self.assertRaises(gf.Busy):
                with gf.transaction(gf.Store(self.store.d), wait=0):
                    pass
        with gf.transaction(gf.Store(self.store.d), wait=0):
            pass

    def test_a_busy_store_leaves_refetch_and_refresh_alone(self):
        with gf.transaction(self.store):
            with self.assertRaises(gf.Busy):
                gf.refresh(NOW, gf.Store(self.store.d), query=self.gh, events={}, wait=0)
            with self.assertRaises(gf.Busy):
                gf.refetch(["backend#1"], NOW, gf.Store(self.store.d), query=self.gh, wait=0)
        self.assertEqual(self.gh.queries, [])


class Retention(Base):
    def settled(self, key="web#2", ended=NOW - gf.KEEP - 86400, number=2):
        self.store.write(self.store.path(key), {"number": number, "settled": True, "mergedAt": iso(ended), "closedAt": None, "headRefName": "feat", "state": "MERGED"})
        self.store.write(self.store.path(key, heads=True), {A: {"fetched": 1, "commit": {"oid": A}}})

    def test_old_settled_evidence_leaves_the_hot_store_and_stays_readable(self):
        self.settled()
        self.assertEqual(gf.retire_settled(self.store, NOW), ["web#2"])
        self.assertEqual(self.store.keys(), [])
        self.assertEqual(gf.load(self.store), {})
        self.assertEqual(list(gf.load(self.store, archived=["web#2"])), ["web#2"])

    def test_open_unsettled_or_recent_prs_stay(self):
        self.settled(ended=NOW - gf.KEEP + 86400)
        self.store.write(self.store.path("web#3"), {"number": 3, "state": "OPEN", "mergedAt": None, "closedAt": None})
        self.store.write(self.store.path("web#4"), {"number": 4, "settled": False, "mergedAt": iso(NOW - gf.KEEP - 86400), "closedAt": None})
        self.assertEqual(gf.retire_settled(self.store, NOW), [])
        self.assertEqual(self.store.keys(), ["web#2", "web#3", "web#4"])

    def test_a_retired_pr_that_returns_is_fetched_again(self):
        self.settled()
        gf.retire_settled(self.store, NOW)
        found = [{"key": "web#2", "state": "OPEN", "updatedAt": iso(NOW), "ended": None, "headRefOid": A}]
        self.assertEqual(gf.wanted(self.store, found, {}, NOW), [("web#2", "new")])

    def test_hot_prs_hold_their_receipts(self):
        self.settled()
        self.assertEqual(gf.receipt_holds(self.store), {"web": {"numbers": {2}, "refs": {"refs/heads/feat"}}})


class Generations(Base):
    def stored(self, value, sha):
        self.store.write(self.store.path("backend#1"), {"number": 1, "value": value})
        self.store.write(self.store.path("backend#1", heads=True), {sha: {"fetched": 1, "commit": {"oid": sha}}})

    def kept(self):
        return [(self.store.read(path)["value"], list(self.store.read(heads, {}))) for path, heads in self.store.generations("backend#1")]

    def test_a_repeated_retirement_keeps_both_generations(self):
        self.stored("first", A)
        self.store.retire("backend#1")
        self.stored("second", B)
        self.store.retire("backend#1")
        self.assertEqual(self.kept(), [("first", [A]), ("second", [B])])

    def test_retire_reopen_retire_through_the_real_paths(self):
        settled = {"number": 1, "settled": True, "state": "MERGED", "mergedAt": iso(NOW - gf.KEEP - 86400), "closedAt": None, "value": "first"}
        self.store.write(self.store.path("backend#1"), settled)
        self.store.write(self.store.path("backend#1", heads=True), {A: {"fetched": 1, "commit": {"oid": A}}})
        self.assertEqual(gf.retire_settled(self.store, NOW), ["backend#1"])
        found = [{"key": "backend#1", "state": "OPEN", "updatedAt": iso(NOW), "ended": None, "headRefOid": B}]
        self.assertEqual(gf.wanted(self.store, found, {}, NOW), [("backend#1", "new")])
        later = NOW + gf.KEEP + 86400
        self.store.write(self.store.path("backend#1"), dict(settled, mergedAt=iso(NOW), value="second"))
        self.store.write(self.store.path("backend#1", heads=True), {B: {"fetched": 2, "commit": {"oid": B}}})
        self.assertEqual(gf.retire_settled(self.store, later), ["backend#1"])
        self.assertEqual(self.kept(), [("first", [A]), ("second", [B])])
        self.assertEqual(gf.load(self.store, archived=["backend#1"])["backend#1"][0]["value"], "second")
        self.assertEqual(list(gf.load(self.store, archived=["backend#1"])["backend#1"][1]), [B])

    def test_generations_order_by_number_past_nine(self):
        for i in range(11):
            self.stored(i, A)
            self.store.retire("backend#1")
        self.assertEqual([value for value, _ in self.kept()], list(range(11)))

    def test_a_crash_before_the_generation_is_published_loses_nothing(self):
        self.stored("first", A)
        with patch.object(gf.os, "rename", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.store.retire("backend#1")
        self.assertEqual((self.store.facts("backend#1")["value"], list(self.store.heads("backend#1")), self.kept()), ("first", [A], []))
        self.store.retire("backend#1")
        self.assertEqual((self.kept(), self.store.keys()), ([("first", [A])], []))

    def test_a_crash_after_publishing_keeps_a_recoverable_copy(self):
        self.stored("first", A)
        real = os.remove

        def flaky(path):
            if path == self.store.path("backend#1"):
                raise OSError("disk")
            real(path)
        with patch.object(gf.os, "remove", flaky):
            with self.assertRaises(OSError):
                self.store.retire("backend#1")
        self.assertEqual(self.kept(), [("first", [A])])
        self.assertEqual(self.store.keys(), ["backend#1"])
        self.store.retire("backend#1")
        self.assertEqual((self.store.keys(), self.kept()), ([], [("first", [A]), ("first", [A])]))
        self.assertEqual(list(gf.load(self.store, archived=["backend#1"])["backend#1"][1]), [A])

    def test_a_pr_without_heads_still_retires(self):
        self.store.write(self.store.path("backend#1"), {"number": 1, "value": "first"})
        self.store.retire("backend#1")
        self.assertEqual(self.store.read(self.store.generations("backend#1")[0][0])["value"], "first")
        self.assertEqual(list(gf.load(self.store, archived=["backend#1"])["backend#1"][1]), [])


class Refetch(Base):
    def test_verify_refetches_every_head_and_leaves_an_open_pr_unsealed(self):
        self.gh.add("backend#1", pr([commit(A)], updated=NOW - 60), {A: passing(T0)})
        self.refresh(NOW)
        done = gf.refetch(["backend#1"], NOW + 60, self.store, self.gh, lambda: 0)
        self.assertEqual((done, kinds(self.gh)[-2:]), (["backend#1"], ["facts", "history"]))
        self.assertFalse(gf.load(self.store)["backend#1"][0].get("settled"))


if __name__ == "__main__":
    unittest.main()
