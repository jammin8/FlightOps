


import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from gh_samples import A, B, RULES, T0, check, commit, hydrated, iso, load, passing, pr, protection, status, suite

ev = load("ghevents")
gf = load("ghfacts")
pf = load("prfacts")
vf = load("verify")
NOW = T0 + 3600


def budget(**extra):
    out = {"discovered": 1, "discovery_complete": True, "discovered_at": NOW, "waiting": 0, "waiting_keys": [], "unfetched": [],
           "stale": [], "retry": {}, "points_last_hour": 0, "cap": 100, "held": 0, "errors": [], "last_clean_read": NOW,
           "due_heads": 0, "due_prs": [], "rules_read": {}}
    out.update(extra)
    return out


class Setup(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.events, self.out = os.path.join(root, "events"), os.path.join(root, "pr-facts.json")
        self.store = gf.Store(os.path.join(root, "facts"))
        os.makedirs(self.events)

        self.store_pr("backend#1", pr([commit(A)], updated=T0), {A: passing(T0 + 5)})
        self.store.write(os.path.join(self.store.d, "rules.json"),
                         {"fetched": T0, "repos": {k: {"read": T0, "rules": protection(*RULES)} for k in gf.REPOS}})
        with open(os.path.join(self.events, "receipts.jsonl"), "w") as f:
            f.write(json.dumps({"id": "9", "repo": "backend", "type": "PullRequestEvent", "at": iso(T0), "t": T0, "actor": "x",
                                "action": "opened", "number": 1, "head_sha": A, "head_ref": "feat", "base_ref": "main"}) + "\n")
        ev.save_state({"repos": {"backend": {"coverage": [[T0 - 600, NOW]]}}, "counted": []}, self.events)

    def store_pr(self, key, p, heads):
        p["fetched_at"] = int(T0)
        self.store.write(self.store.path(key), p)
        self.store.write(self.store.path(key, heads=True), {sha: {"fetched": int(T0), "commit": c} for sha, c in heads.items()})

    def publish(self, **extra):
        return pf.run(NOW, self.events, self.store, self.out, poll=lambda now, d: {"repos": {}, "counted_last_hour": 0, "cap": 36},
                      refresh=lambda now, store: budget(**extra))

    def verify(self, *args, out=None):
        buf = io.StringIO()
        code = vf.main(list(args), out or self.out, self.events, self.store, NOW, buf)
        return code, buf.getvalue()


class Collector(Setup):
    def test_a_run_writes_the_records_and_their_sources_and_prints_only_a_summary(self):
        s = self.publish()
        self.assertEqual((s["schema"], s["prs"], s["errors"]), ("prfacts-run/2", 1, []))
        with open(self.out) as f:
            data = json.load(f)
        self.assertEqual((data["schema"], data["prs"]["backend#1"]["r0"]["status"]), ("pr-facts/2", "verified"))
        self.assertEqual((data["sources"]["facts"]["discovery_complete"], data["sources"]["run"]["at"]), (True, int(NOW)))

    def test_a_failing_feed_or_fetch_still_publishes_from_what_is_stored_and_says_so(self):
        def broken(*_):
            raise RuntimeError("offline")
        s = pf.run(NOW, self.events, self.store, self.out, poll=broken, refresh=broken)
        self.assertEqual((s["prs"], s["errors"][:2]), (1, ["events: offline", "facts: offline"]))
        with open(self.out) as f:
            facts = json.load(f)["sources"]["facts"]
        self.assertEqual((facts["discovery_complete"], facts["errors"]), (False, ["this run's fetch failed"]))


class Retention(Setup):
    def test_a_pass_retires_old_settled_evidence_and_says_so_only_on_failure(self):
        self.publish()
        retired = NOW + gf.KEEP + 90 * 86400
        p = self.store.facts("backend#1")
        p.update(settled=True, mergedAt=iso(NOW), state="MERGED")
        self.store.write(self.store.path("backend#1"), p)
        s = pf.run(retired, self.events, self.store, self.out, poll=lambda *_: {}, refresh=lambda *_: budget())
        self.assertEqual((s["prs"], s["errors"]), (0, []))
        self.assertEqual(list(gf.load(self.store, archived=["backend#1"])), ["backend#1"])

    def test_a_retention_failure_still_publishes(self):
        self.publish()
        with patch.object(pf.ghevents, "archive_receipts", side_effect=OSError("disk full")):
            s = pf.run(NOW, self.events, self.store, self.out, poll=lambda *_: {}, refresh=lambda *_: budget())
        self.assertEqual((s["prs"], s["errors"]), (1, ["retention: disk full"]))


class Verify(Setup):
    def test_published_records_reproduce_from_complete_inputs(self):
        self.publish()
        code, out = self.verify("--offline")
        self.assertEqual(code, 0, out)
        self.assertIn("backend#1: first-ready commit aaaaaaa verified (opened event) · first try pass · heads 1, complete history 1", out)
        self.assertIn("Checked 1 of 1 published records", out)

    def test_a_changed_number_is_a_contradiction(self):
        self.publish()
        with open(self.out) as f:
            data = json.load(f)
        data["prs"]["backend#1"]["r0"]["first_attempt"]["outcome"] = "fail"
        with open(self.out, "w") as f:
            json.dump(data, f)
        code, out = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("differs at r0.first_attempt.outcome", out)

    def test_inputs_that_moved_on_are_incomplete_not_a_contradiction(self):
        self.publish()
        with open(os.path.join(self.events, "receipts.jsonl"), "a") as f:
            f.write(json.dumps({"id": "10", "repo": "backend", "type": "PushEvent", "at": iso(T0 - 60), "t": T0 - 60, "actor": "x",
                                "ref": "refs/heads/feat", "before": "0" * 40, "head": A, "push_id": 1}) + "\n")
        code, out = self.verify("backend#1")
        self.assertEqual(code, 2)
        self.assertIn("published from earlier inputs", out)

    def test_a_pr_without_stored_inputs_is_incomplete(self):
        self.publish()
        code, out = self.verify("--json", "web#7")
        self.assertEqual((code, json.loads(out)["results"]), (2, [{"pr": "web#7", "verdict": "incomplete", "detail": "no stored inputs"}]))

    def test_a_refresh_already_running_is_said_plainly(self):
        with patch.object(vf.ghfacts, "refetch", side_effect=vf.ghfacts.Busy("another facts refresh is running")):
            code, text = self.verify("--refresh", "backend#1")
        self.assertEqual(code, 2)
        self.assertIn("another facts refresh is running", text)

    def test_refresh_needs_named_prs(self):
        self.assertEqual(self.verify("--refresh")[0], 2)


class PolicyFreshness(Setup):
    def publish_at(self, now):
        pf.run(now, self.events, self.store, self.out, poll=lambda *_: {},
               refresh=lambda *_: budget(last_clean_read=now))
        with open(self.out) as f:
            return json.load(f)["prs"]["backend#1"]

    def verify_at(self, now):
        buf = io.StringIO()
        code = vf.main(["--offline", "--json", "backend#1"], self.out, self.events, self.store, now, buf)
        return code, json.loads(buf.getvalue())

    def test_expired_rules_publish_unknown_and_verify_incomplete_without_hiding_failures(self):
        now = T0 + 3 * gf.RULES_EVERY
        for state, expected in (("SUCCESS", "unknown"), ("FAILURE", "fail")):
            with self.subTest(state=state):
                self.store_pr("backend#1", pr([commit(A)]), {A: passing(T0 + 5, state=state)})
                r = self.publish_at(now)
                self.assertEqual(r["policy_source"], "observed")
                self.assertEqual(r["heads"][0]["first_attempt"]["outcome"], expected)
                self.assertIn("required checks not evidenced", r["incomplete"])
                code, result = self.verify_at(now)
                self.assertEqual((code, result["verdict"]), (2, "incomplete"))
                self.assertIn("reproduced; inputs not complete", result["results"][0]["detail"])

    def test_offline_verification_rejects_a_pass_published_before_rules_expired(self):
        self.publish_at(T0 + 2 * gf.RULES_EVERY - 1)
        self.assertEqual(self.verify_at(T0 + 2 * gf.RULES_EVERY - 1)[0], 0)
        code, result = self.verify_at(T0 + 2 * gf.RULES_EVERY)
        self.assertEqual((code, result["verdict"]), (2, "incomplete"))
        self.assertIn("published from earlier inputs", result["results"][0]["detail"])

    def test_successful_rule_refresh_restores_a_verifiable_pass(self):
        from test_ghfacts import FakeGitHub
        now = T0 + 3 * gf.RULES_EVERY
        self.publish_at(now)
        self.assertEqual(self.verify_at(now)[0], 2)
        gf.refresh_rules(gf.Fetcher(self.store, now, FakeGitHub(), lambda: 0))
        self.assertEqual(self.store.rules()["repos"]["backend"]["read"], now)
        r = self.publish_at(now)
        self.assertEqual((r["policy_source"], r["heads"][0]["first_attempt"]["outcome"]), ("rules", "pass"))
        self.assertEqual(self.verify_at(now)[0], 0)


class ExpiredPolicyFailures(Setup):
    publish_at = PolicyFreshness.publish_at
    verify_at = PolicyFreshness.verify_at

    def mixed(self, state="FAILURE", app="github-actions", app_id=None):
        return hydrated(suite(T0, check("fast", state, T0, required=None, i=1), app=app, app_id=app_id),
                        suite(T0, check("integration", "SUCCESS", T0, required=True, i=2)))

    def record_at(self, history, age=172800):
        self.store_pr("backend#1", pr([commit(A)]), {A: history})
        return self.publish_at(T0 + age)

    def test_mixed_flags_retain_failure_and_first_decided_at_expiry(self):
        for age in (172799.999, 172800, 259200):
            with self.subTest(age=age):
                r = self.record_at(self.mixed(), age)
                for mode in ("first_attempt", "first_completed", "latest"):
                    self.assertEqual(r["heads"][0][mode]["failed"], ["fast"])
                    self.assertEqual(r["heads"][0][mode]["outcome"], "fail")
                self.assertEqual(r["r0"]["first_attempt"]["failed"], ["fast"])
                self.assertEqual(r["first_decided"]["failed"], ["fast"])
                code, result = self.verify_at(T0 + age)
                self.assertEqual(code, 0 if age < 172800 else 2)
                self.assertNotEqual(result["verdict"], "contradiction")
                if age >= 172800:
                    self.assertIn("expired_policy", r)
                    self.assertEqual(r["expired_policy"]["read"], T0)
                    self.assertIn("required checks not evidenced", r["incomplete"])
                    self.assertFalse(r["heads"][0]["complete"])
                    self.assertEqual(r["history_complete"], 0)

    def test_app_bot_status_retains_latest_failure_without_inventing_first_attempt(self):
        h = hydrated(suite(T0, check("integration", "SUCCESS", T0, i=2)),
                     statuses=[status("fast", "FAILURE", T0, required=None,
                                      creator=("Bot", "github-actions[bot]"))])
        r = self.record_at(h)
        self.assertEqual(r["heads"][0]["latest"], {"outcome": "fail", "reason": None, "failed": ["fast"]})
        self.assertEqual(r["heads"][0]["first_attempt"]["outcome"], "unknown")
        self.assertEqual(r["heads"][0]["first_completed"]["outcome"], "unknown")
        self.assertIsNone(r["first_decided"])
        code, result = self.verify_at(T0 + 172800)
        self.assertEqual(code, 2)
        self.assertIn("reproduced; inputs not complete", result["results"][0]["detail"])

    def test_optional_other_app_and_unidentified_status_failures_are_not_added(self):
        for h in (self.mixed(app="other-app"),
                  hydrated(suite(T0, check("integration", "SUCCESS", T0, i=2)),
                           statuses=[status("fast", "FAILURE", T0, required=None, creator=("Bot", "other[bot]"))]),
                  hydrated(suite(T0, check("integration", "SUCCESS", T0, i=2)),
                           statuses=[status("fast", "FAILURE", T0, required=None)]),
                  hydrated(suite(T0, check("integration", "SUCCESS", T0, i=2),
                                 check("optional", "FAILURE", T0, required=False, i=3)))):
            with self.subTest(history=h):
                r = self.record_at(h)
                self.assertEqual(r["heads"][0]["latest"]["outcome"], "unknown")
                self.assertEqual(r["heads"][0]["latest"]["failed"], [])
                self.assertEqual(self.verify_at(T0 + 172800)[0], 2)

    def test_expired_app_id_rule_still_requires_matching_app_evidence(self):
        cache = self.store.rules()
        rules = protection(("github-actions", "integration", None))
        rules["rulesets"]["nodes"] = [{"enforcement": "ACTIVE", "target": "BRANCH",
            "conditions": {"refName": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": {"pageInfo": {"hasNextPage": False}, "nodes": [{"type": "REQUIRED_STATUS_CHECKS",
                "parameters": {"requiredStatusChecks": [{"context": "fast", "integrationId": 1234}]}}]}}]
        cache["repos"]["backend"]["rules"] = rules
        self.store.write(os.path.join(self.store.d, "rules.json"), cache)
        for app_id, expected in ((1234, "fail"), (4321, "unknown"), (None, "unknown")):
            with self.subTest(app_id=app_id):
                r = self.record_at(self.mixed(app="ci-app", app_id=app_id))
                self.assertEqual(r["heads"][0]["latest"]["outcome"], expected)
                self.assertEqual(self.verify_at(T0 + 172800)[0], 2)

    def test_no_true_flags_do_not_turn_optional_jobs_into_known_obligations(self):
        for state, expected in (("SUCCESS", []), ("FAILURE", ["fast"])):
            with self.subTest(state=state):
                h = hydrated(suite(T0, check("fast", state, T0, required=None, i=1),
                                   check("integration", "SUCCESS", T0, required=None, i=2),
                                   check("optional", "FAILURE", T0, required=False, i=3)),
                             suite(T0, check("fast", "FAILURE", T0, required=None, i=4), app="other-app"))
                r = self.record_at(h)
                self.assertEqual(r["heads"][0]["latest"]["failed"], expected)
                self.assertEqual(r["heads"][0]["latest"]["outcome"], "fail" if expected else "unknown")
                self.assertEqual(self.verify_at(T0 + 172800)[0], 2)

    def test_incomplete_or_unordered_history_never_gains_a_failure_from_stale_rules(self):
        for incomplete in (True, False):
            with self.subTest(incomplete=incomplete):
                h = self.mixed()
                if incomplete:
                    h["checkSuites"]["pageInfo"]["hasNextPage"] = True
                else:
                    runs = h["checkSuites"]["nodes"][0]["checkRuns"]["nodes"]
                    runs[0]["databaseId"] = None
                    runs.append(check("fast", "SUCCESS", T0 + 60, required=None, i=3))
                r = self.record_at(h)
                self.assertEqual(r["heads"][0]["latest"]["outcome"], "unknown")
                self.assertEqual(r["heads"][0]["latest"]["failed"], [])
                self.assertEqual(self.verify_at(T0 + 172800)[0], 2)

    def test_expired_healthy_rules_never_authorize_passing_or_complete_inputs(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    cache = self.store.rules()
                    cache["repos"]["backend"]["rules"] = protection()
                    self.store.write(os.path.join(self.store.d, "rules.json"), cache)
                r = self.record_at(self.mixed(state="SUCCESS"))
                self.assertEqual(r["heads"][0]["latest"]["outcome"], "unknown")
                self.assertFalse(r["heads"][0]["complete"])
                self.assertEqual(r["history_complete"], 0)
                self.assertEqual(self.verify_at(T0 + 172800)[0], 2)

    def test_future_policy_is_not_used_to_retain_unflagged_failures(self):
        r = self.record_at(self.mixed(), age=-0.001)
        self.assertEqual(r["heads"][0]["latest"]["outcome"], "unknown")
        self.assertNotIn("expired_policy", r)
        self.assertEqual(self.verify_at(T0 - 0.001)[0], 2)

    def test_rerun_modes_and_current_flagged_failures_stay_separate(self):
        h = self.mixed()
        h["checkSuites"]["nodes"][0]["checkRuns"]["nodes"].append(
            check("fast", "SUCCESS", T0 + 60, required=None, i=3))
        r = self.record_at(h)
        self.assertEqual(r["heads"][0]["first_attempt"]["failed"], ["fast"])
        self.assertEqual(r["heads"][0]["latest"]["outcome"], "unknown")
        self.assertEqual(r["first_decided"]["failed"], ["fast"])
        h = self.mixed()
        h["checkSuites"]["nodes"][1]["checkRuns"]["nodes"].append(
            check("new-required", "FAILURE", T0, required=True, i=3))
        r = self.record_at(h)
        self.assertEqual(r["heads"][0]["latest"]["failed"], ["fast", "new-required"])
        self.assertEqual(self.verify_at(T0 + 172800)[0], 2)

    def test_stale_policy_is_an_input_and_fresh_policy_replaces_it(self):
        r = self.record_at(self.mixed(), age=259200)
        self.assertEqual(r["heads"][0]["latest"]["outcome"], "fail")
        cache = self.store.rules()
        cache["repos"]["backend"]["rules"] = protection(("github-actions", "integration", None))
        self.store.write(os.path.join(self.store.d, "rules.json"), cache)
        code, result = self.verify_at(T0 + 259200)
        self.assertEqual(code, 2)
        self.assertIn("published from earlier inputs", result["results"][0]["detail"])
        cache["repos"]["backend"]["read"] = T0 + 259200
        cache["fetched"] = T0 + 259200
        self.store.write(os.path.join(self.store.d, "rules.json"), cache)
        r = self.publish_at(T0 + 259200)
        self.assertEqual(r["heads"][0]["latest"]["outcome"], "pass")
        self.assertNotIn("expired_policy", r)
        self.assertEqual(self.verify_at(T0 + 259200)[0], 0)

    def test_explicit_historical_time_ignores_later_wall_clock(self):
        with patch("time.time", return_value=T0 + 30 * 86400):
            r = self.record_at(self.mixed(state="SUCCESS"), age=1)
            self.assertEqual(r["heads"][0]["latest"]["outcome"], "pass")
            self.assertEqual(self.verify_at(T0 + 1)[0], 0)


class Scope(Setup):


    def test_no_publication_and_no_cache_is_incomplete_not_success(self):
        empty = gf.Store(tempfile.mkdtemp())
        buf = io.StringIO()
        code = vf.main([], os.path.join(tempfile.mkdtemp(), "missing.json"), tempfile.mkdtemp(), empty, NOW, buf)
        self.assertEqual(code, 2)
        self.assertIn("no readable publication", buf.getvalue())

    def test_an_older_publication_schema_is_incomplete(self):
        with open(self.out, "w") as f:
            json.dump({"schema": "pr-facts/1", "prs": {}}, f)
        self.assertEqual(self.verify()[0], 2)

    def test_an_evidenced_empty_dataset_verifies(self):
        os.remove(self.store.path("backend#1"))
        os.remove(self.store.path("backend#1", heads=True))
        self.publish(discovered=0)
        code, out = self.verify()
        self.assertEqual(code, 0, out)
        self.assertIn("discovery found no PRs", out)

    def test_an_empty_dataset_without_evidence_is_incomplete(self):
        os.remove(self.store.path("backend#1"))
        os.remove(self.store.path("backend#1", heads=True))
        self.publish(discovered=0, discovery_complete=False)
        self.assertEqual(self.verify()[0], 2)

    def test_a_status_only_history_reproduces_but_isn_t_complete(self):
        self.store.write(os.path.join(self.store.d, "rules.json"),
                         {"fetched": T0, "repos": {k: {"read": T0, "rules": protection((None, "fast"))} for k in gf.REPOS}})
        self.store_pr("backend#1", pr([commit(A)], updated=T0), {A: hydrated(statuses=[status("fast", "SUCCESS", T0)])})
        self.publish()
        code, out = self.verify("backend#1")
        self.assertEqual(code, 2)
        self.assertIn("reproduced; inputs not complete: 1 heads without complete history", out)

    def test_history_waiting_to_be_read_again_is_incomplete(self):
        p = pr([commit(A)], updated=T0)
        p["reread_heads"] = True
        self.store_pr("backend#1", p, {A: passing(T0 + 5)})
        self.publish()
        code, out = self.verify("backend#1")
        self.assertEqual(code, 2)
        self.assertIn("history to read again", out)

    def test_main_s_rules_not_read_is_incomplete(self):
        os.remove(os.path.join(self.store.d, "rules.json"))
        self.publish()
        code, out = self.verify()
        self.assertEqual(code, 2)
        self.assertIn("required checks not evidenced", out)


class Backlog(Setup):


    def test_prs_waiting_for_facts_make_the_whole_window_incomplete(self):
        self.publish(discovered=2, unfetched=["backend#2"], waiting=1, waiting_keys=["backend#2"])
        with open(self.out) as f:
            self.assertEqual(json.load(f)["sources"]["facts"]["unfetched"], ["backend#2"])
        code, out = self.verify()
        self.assertEqual(code, 2)
        self.assertIn("1 PRs discovered but not fetched yet", out)
        self.assertEqual(self.verify("backend#1")[0], 0)

    def test_unfinished_discovery_or_stale_prs_make_it_incomplete(self):
        self.publish(discovery_complete=False)
        self.assertIn("discovery didn't read every page", self.verify()[1])
        self.publish(stale=["backend#1"])
        self.assertIn("1 PRs changed since they were fetched", self.verify()[1])


if __name__ == "__main__":
    unittest.main()


class Collected(unittest.TestCase):


    def setUp(self):
        from test_ghfacts import FakeGitHub
        root = tempfile.mkdtemp()
        self.store, self.events, self.out = gf.Store(os.path.join(root, "facts")), os.path.join(root, "events"), os.path.join(root, "p.json")
        os.makedirs(self.events)
        self.gh = FakeGitHub()

    def collect(self, now, head=None, rules=None):
        if head is not None:
            self.gh.add("backend#1", pr([commit(A)], updated=T0 + 86400 - 60), {A: head})
        if rules is not None:
            self.gh.rules["backend"] = rules
        s = gf.refresh(now, self.store, self.gh, lambda: 0)
        pf.run(now, self.events, self.store, self.out, poll=lambda *_: {}, refresh=lambda *_: s)
        buf = io.StringIO()
        code = vf.main(["--offline", "--json"], self.out, self.events, self.store, now, buf)
        with open(self.out) as f:
            return code, json.loads(buf.getvalue()), json.load(f)["prs"]["backend#1"]


class EndToEnd(Collected):


    def test_a_failing_status_named_like_a_required_check(self):
        h = passing(T0)
        h["status"]["contexts"] = [status("fast", "FAILURE", T0)]
        code, _, rec = self.collect(T0 + 86400, h, protection((None, "fast"), ("github-actions", "integration")))
        self.assertEqual((code, rec["heads"][0]["latest"]["outcome"], rec["history_complete"]), (2, "fail", 0))

    def test_a_ruleset_bound_to_an_app_that_never_reported(self):
        rules = protection()
        rules["rulesets"]["nodes"] = [{"name": "app-bound", "enforcement": "ACTIVE", "target": "BRANCH",
                                       "conditions": {"refName": {"include": ["refs/heads/main"], "exclude": []}},
                                       "rules": {"pageInfo": {"hasNextPage": False}, "nodes": [{"type": "REQUIRED_STATUS_CHECKS",
                                                 "parameters": {"requiredStatusChecks": [{"context": "fast", "integrationId": 1234}]}}]}}]
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0), app="unrelated-app"))
        code, _, rec = self.collect(T0 + 86400, h, rules)
        self.assertEqual((code, rec["heads"][0]["first_attempt"]["outcome"]), (2, "unknown"))

    def test_null_rule_connections(self):
        code, _, rec = self.collect(T0 + 86400, passing(T0), {"branchProtectionRules": None, "rulesets": None})
        self.assertEqual((code, rec["policy_source"]), (2, "observed"))

    def test_a_failed_periodic_read_then_a_successful_retry(self):
        now = T0 + 86400
        self.assertEqual(self.collect(now, passing(T0))[0], 0)
        self.gh.heads[A] = passing(T0, state="FAILURE")
        self.gh.errors[("backend", "h0")] = "read failed"
        code, v, rec = self.collect(now + 3601)
        self.assertEqual((code, rec["heads"][0]["first_attempt"]["outcome"], rec["heads"][0]["refresh_failed"]), (2, "pass", "GitHub error"))
        self.assertIn("1 heads whose last read failed (older history shown)", rec["incomplete"])
        self.gh.errors.clear()
        code, v, rec = self.collect(now + 3601 + gf.RECHECK)
        self.assertEqual((code, rec["heads"][0]["first_attempt"]["outcome"], rec["incomplete"]), (0, "fail", []))

    def test_a_partial_answer_after_a_good_read(self):
        now = T0 + 86400
        self.collect(now, passing(T0))
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=70), check("integration", "SUCCESS", T0, i=71)), suite(T0, workflow="lint", run_id=8))
        h["checkSuites"]["nodes"][1]["checkRuns"] = None
        self.gh.heads[A] = h
        self.gh.errors[("backend", "h0", "checkSuites", "nodes", 1, "checkRuns")] = "partial query"
        self.assertEqual(self.collect(now + 3601)[0], 2)

    def test_a_run_held_by_the_budget_isn_t_a_fresh_read(self):
        now = T0 + 86400
        self.collect(now, passing(T0))
        with open(os.path.join(self.store.d, "cost.jsonl"), "a") as f:
            f.write(json.dumps({"id": "hold", "t": now + 3600, "what": "x", "reserved": gf.CAP}) + "\n")
        code, v, _ = self.collect(now + 3601)
        self.assertEqual(code, 2)
        self.assertIn("1 head histories due to be read again", v["backlog"])


class StatusSource(Collected):



    def ruleset(self, app_id):
        rules = protection()
        rules["rulesets"]["nodes"] = [{"name": "app-bound", "enforcement": "ACTIVE", "target": "BRANCH",
                                       "conditions": {"refName": {"include": ["refs/heads/main"], "exclude": []}},
                                       "rules": {"pageInfo": {"hasNextPage": False}, "nodes": [{"type": "REQUIRED_STATUS_CHECKS",
                                                 "parameters": {"requiredStatusChecks": [{"context": "fast", "integrationId": app_id}]}}]}}]
        return rules

    def test_a_numeric_id_rule_and_a_status_github_says_isn_t_required(self):
        code, _, rec = self.collect(T0 + 86400, hydrated(statuses=[status("fast", "SUCCESS", T0, required=False)]), self.ruleset(1234))
        self.assertEqual((code, rec["heads"][0]["latest"]["outcome"]), (2, "unknown"))

    def test_a_numeric_id_rule_and_a_status_of_unknown_origin(self):
        code, _, rec = self.collect(T0 + 86400, hydrated(statuses=[status("fast", "SUCCESS", T0, required=None)]), self.ruleset(1234))
        self.assertEqual((code, rec["heads"][0]["latest"]["reason"]), (2, "required app not identified"))

    def test_a_slug_bound_rule_and_a_status_github_says_isn_t_required(self):
        code, _, rec = self.collect(T0 + 86400, hydrated(statuses=[status("fast", "SUCCESS", T0, required=False)]),
                                    protection(("ci-app", "fast")))
        self.assertEqual((code, rec["heads"][0]["latest"]["outcome"]), (2, "unknown"))

    def test_control_a_rule_naming_no_app_takes_the_status(self):
        code, _, rec = self.collect(T0 + 86400, hydrated(statuses=[status("fast", "SUCCESS", T0, required=False)]),
                                    protection((None, "fast")))
        self.assertEqual((code, rec["heads"][0]["latest"]["outcome"]), (2, "pass"))

    def test_control_a_status_from_the_named_app_counts(self):
        h = hydrated(statuses=[status("fast", "SUCCESS", T0, required=None, creator=("Bot", "ci-app[bot]"))])
        code, _, rec = self.collect(T0 + 86400, h, protection(("ci-app", "fast")))
        self.assertEqual((code, rec["heads"][0]["latest"]["outcome"]), (2, "pass"))

    def test_control_a_check_run_from_the_matching_app_verifies(self):
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), app="ci-app", app_id=1234))
        code, _, rec = self.collect(T0 + 86400, h, self.ruleset(1234))
        self.assertEqual((code, rec["heads"][0]["first_attempt"]["outcome"], rec["heads"][0]["latest"]["outcome"]), (0, "pass", "pass"))
