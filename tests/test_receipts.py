


import json
import copy
import os
import unittest

from gh_samples import (A, B, C, M, RULES, T0, check, commit, drafted, hydrated, iso, load, passing, pr, protection, ready,
                        receipts as rec, status, suite)

rc = load("receipts")
KEYS = {("gha", "fast", None), ("gha", "integration", None)}
REQ = ("required", KEYS, "rules")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def att(*states, first_id=1):
    return [{"order": first_id + i, "start": None, "end": None, "state": s, "required": True, "workflow": "ci"} for i, s in enumerate(states)]


def head(a, collected=True, why=()):
    return {"att": a, "collected": collected, "why": list(why)}


def ci(fast, integration=("SUCCESS",), policy=REQ, collected=True, why=()):
    a = {("gha", "fast"): att(*fast)}
    if integration is not None:
        a[("gha", "integration")] = att(*integration, first_id=100)
    r, _ = rc.head_ci(head(a, collected, why), policy)
    return {m: (v["outcome"], v["reason"]) for m, v in r.items()}


def record(p, hyd, r=None, rules=RULES):
    return rc.pr_record("backend#1", p, hyd, r, rules)


class ConnectionShape(unittest.TestCase):
    def test_a_connection_needs_readable_nodes_and_explicit_completeness(self):
        for conn in (None, {}, {"pageInfo": {"hasNextPage": False}}, {"pageInfo": {"hasNextPage": False}, "nodes": None},
                     {"pageInfo": {"hasNextPage": True}, "nodes": []}, {"pageInfo": {}, "nodes": []}, {"nodes": []}):
            self.assertFalse(rc.complete_connection(conn), conn)
        self.assertTrue(rc.complete_connection({"pageInfo": {"hasNextPage": False}, "nodes": []}))

    def test_rule_connections_with_no_nodes_are_unknown_not_empty(self):
        whole = {"pageInfo": {"hasNextPage": False}}
        self.assertIsNone(rc.required_checks({"branchProtectionRules": whole, "rulesets": whole}))

    def test_a_null_check_list_is_not_collected(self):
        commit = {"checkSuites": {"pageInfo": {"hasNextPage": False}, "nodes": None}, "status": {"contexts": []}}
        self.assertFalse(rc.attempts_hydrated(commit)["collected"])


class Verdicts(unittest.TestCase):
    def test_a_rerun_never_erases_a_first_failure(self):
        r = ci(("FAILURE", "SUCCESS"))
        self.assertEqual((r["first_attempt"][0], r["first_completed"][0], r["latest"][0]), ("fail", "fail", "pass"))

    def test_a_cancelled_first_attempt_is_unknown_and_first_completed_is_apart(self):
        r = ci(("CANCELLED", "SUCCESS"))
        self.assertEqual(r["first_attempt"], ("unknown", "cancelled"))
        self.assertEqual(r["first_completed"][0], "pass")

    def test_stale_skipped_and_missing_required_jobs_are_not_a_pass(self):
        self.assertEqual(ci(("STALE",))["first_attempt"][0], "unknown")
        self.assertEqual(ci(("SUCCESS",), integration=("SKIPPED",))["first_attempt"], ("unknown", "skipped"))
        self.assertEqual(ci(("SUCCESS",), integration=None)["first_attempt"], ("unknown", "missing"))

    def test_incomplete_collection_decides_no_mode(self):
        r = ci(("FAILURE",), collected=False, why=["check runs truncated"])
        for mode in rc.MODES:
            self.assertEqual(r[mode], ("unknown", "check runs truncated"))

    def test_a_newer_running_attempt_makes_latest_unknown(self):
        r = ci(("SUCCESS", "IN_PROGRESS"))
        self.assertEqual((r["first_attempt"][0], r["latest"]), ("pass", ("unknown", "in_progress")))

    def test_a_known_failure_settles_fail_while_another_job_runs(self):
        self.assertEqual(ci(("FAILURE",), integration=("QUEUED",))["first_attempt"][0], "fail")

    def test_optional_failures_are_ignored_under_the_required_policy(self):
        a = {("gha", "fast"): att("SUCCESS"), ("gha", "integration"): att("SUCCESS", first_id=5),
             ("gha", "lint"): att("FAILURE", first_id=9)}
        self.assertEqual(rc.head_ci(head(a), REQ)[0]["first_attempt"]["outcome"], "pass")

    def test_no_required_checks_on_main_is_the_tracked_policy(self):
        a = {("gha", "fast"): att("SUCCESS")}
        pol = rc.policy_of([], [a])
        self.assertEqual(pol, ("tracked", None, "rules"))
        self.assertEqual(rc.head_ci(head(a), pol)[0]["first_attempt"]["outcome"], "pass")

    def test_the_review_gate_is_not_ci(self):
        a = {("gha", "review-gate"): att("FAILURE")}
        self.assertEqual(rc.head_ci(head(a), ("tracked", None, "rules"))[0]["first_attempt"],
                         {"outcome": "unknown", "reason": "no checks", "failed": []})

    def test_failing_jobs_are_named(self):
        a = {("gha", "fast"): att("FAILURE"), ("gha", "integration"): att("TIMED_OUT", first_id=5)}
        self.assertEqual(rc.head_ci(head(a), REQ)[0]["first_attempt"]["failed"], ["fast", "integration"])


class AttemptOrder(unittest.TestCase):


    def test_a_retry_after_a_cancel_before_start_is_not_the_first_attempt(self):
        h = hydrated(suite(T0, check("fast", "CANCELLED", None, i=101), check("fast", "SUCCESS", T0 + 600, i=102),
                           check("integration", "SUCCESS", T0, i=103)))
        r = record(pr([commit(A)]), {A: h})["heads"][0]
        self.assertEqual((r["first_attempt"]["outcome"], r["first_attempt"]["reason"]), ("unknown", "cancelled"))
        self.assertEqual((r["first_completed"]["outcome"], r["latest"]["outcome"]), ("pass", "pass"))

    def test_attempts_without_ids_have_no_known_order(self):
        h = hydrated(suite(T0, check("fast", "FAILURE", T0, i=None), check("fast", "SUCCESS", T0 + 60, i=None),
                           check("integration", "SUCCESS", T0, i=5)))
        r = record(pr([commit(A)]), {A: h})["heads"][0]
        self.assertEqual([r[m]["reason"] for m in rc.MODES], ["attempt order unknown"] * 3)
        self.assertFalse(r["complete"])

    def test_one_attempt_needs_no_order(self):
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=None), check("integration", "SUCCESS", T0, i=None)))
        self.assertEqual(record(pr([commit(A)]), {A: h})["heads"][0]["first_attempt"]["outcome"], "pass")


class Completeness(unittest.TestCase):


    def test_a_missing_check_runs_connection_isn_t_an_empty_one(self):
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), check("integration", "SUCCESS", T0, i=2)),
                     suite(T0, workflow="lint", run_id=8))
        h["checkSuites"]["nodes"][1]["checkRuns"] = None
        r = record(pr([commit(A)]), {A: h})["heads"][0]
        self.assertEqual((r["first_attempt"]["outcome"], r["latest"]["outcome"], r["complete"]), ("unknown", "unknown", False))
        self.assertEqual(r["why"], ["check runs not returned"])

    def test_a_missing_suites_connection_or_a_github_error_isn_t_collected(self):
        self.assertFalse(rc.attempts_hydrated({"checkSuites": None})["collected"])
        h = passing(T0)
        h["_error"] = "Something went wrong"
        self.assertEqual(rc.attempts_hydrated(h)["why"], ["GitHub error: Something went wrong"])

    def test_truncated_pages_make_history_incomplete(self):
        got = rc.attempts_hydrated(hydrated(suite(T0, check("fast", "SUCCESS", T0), more=True)))
        self.assertEqual((got["collected"], got["why"]), (False, ["check runs truncated"]))
        self.assertEqual(rc.attempts_hydrated(hydrated(more=True))["why"], ["check suites truncated"])

    def test_a_required_failing_status_is_never_a_pass(self):
        rules = RULES + [("ci-host", "ci/host", None)]
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), check("integration", "SUCCESS", T0, i=2)),
                     statuses=[status("ci/host", "FAILURE", T0)])
        r = record(pr([commit(A)]), {A: h}, rules=rules)["heads"][0]
        self.assertEqual((r["first_attempt"]["outcome"], r["first_attempt"]["reason"]), ("unknown", "commit status history not kept"))
        self.assertEqual((r["latest"]["outcome"], r["latest"]["failed"]), ("fail", ["ci/host"]))
        self.assertFalse(r["complete"])

    def test_a_status_only_history_isn_t_complete(self):
        h = hydrated(statuses=[status("fast", "SUCCESS", T0), status("integration", "SUCCESS", T0)])
        r = record(pr([commit(A)]), {A: h}, rules=[(None, "fast", None), (None, "integration", None)])
        self.assertEqual((r["heads"][0]["first_attempt"]["reason"], r["history_complete"]), ("commit status history not kept", 0))

    def test_without_main_s_rules_nothing_passes_but_a_failure_still_fails(self):
        r = record(pr([commit(A)]), {A: passing(T0)}, rules=None)
        self.assertEqual((r["policy_source"], r["heads"][0]["first_attempt"]),
                         ("observed", {"outcome": "unknown", "reason": "required checks not evidenced", "failed": []}))
        self.assertIn("required checks not evidenced", r["incomplete"])
        self.assertEqual(record(pr([commit(A)]), {A: passing(T0, state="FAILURE")}, rules=None)["heads"][0]["first_attempt"]["outcome"], "fail")

    def test_a_required_job_that_never_ran_anywhere_is_missing(self):
        r = record(pr([commit(A)]), {A: passing(T0)}, rules=RULES + [("github-actions", "e2e", None)])
        self.assertEqual(r["heads"][0]["first_attempt"], {"outcome": "unknown", "reason": "missing", "failed": []})

    def test_main_s_rules_from_branch_protection_and_rulesets(self):
        repo = protection(("github-actions", "fast"), ("flightops-review-gate", "review-gate"))
        whole = {"pageInfo": {"hasNextPage": False}}
        repo["rulesets"]["nodes"] = [
            {"enforcement": "ACTIVE", "target": "BRANCH", "conditions": {"refName": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
             "rules": {**whole, "nodes": [{"type": "REQUIRED_STATUS_CHECKS", "parameters": {"requiredStatusChecks": [{"context": "integration"}]}}]}},
            {"enforcement": "EVALUATE", "target": "BRANCH", "conditions": {"refName": {"include": ["~ALL"], "exclude": []}},
             "rules": {**whole, "nodes": [{"type": "REQUIRED_STATUS_CHECKS", "parameters": {"requiredStatusChecks": [{"context": "dry-run"}]}}]}},
            {"enforcement": "ACTIVE", "target": "BRANCH", "conditions": {"refName": {"include": ["~ALL"], "exclude": ["refs/heads/main"]}},
             "rules": {**whole, "nodes": [{"type": "REQUIRED_STATUS_CHECKS", "parameters": {"requiredStatusChecks": [{"context": "others"}]}}]}}]
        self.assertEqual(rc.required_checks(repo), [("github-actions", "fast", None), (None, "integration", None),
                                                    ("flightops-review-gate", "review-gate", None)])
        self.assertEqual(rc.required_checks(protection(("github-actions", "fast"), pattern="release/*")), [])
        self.assertIsNone(rc.required_checks(None))

    def test_a_partial_timeline_leaves_readiness_unknown(self):
        p = pr([commit(A)])
        p["gaps"] = ["timelineItems not fully read"]
        r = record(p, {A: passing(T0 + 5)}, rec(opened={1: A}))
        self.assertEqual((r["anchor"], r["anchor_kind"], r["r0"]), (None, "incomplete", None))
        self.assertIn("readiness history not fully read", r["incomplete"])
        p = pr([commit(A)])
        p["timelineItems"]["pageInfo"] = {"hasNextPage": True}
        self.assertIsNone(record(p, {A: passing(T0 + 5)}, rec(opened={1: A}))["r0"])


class Obligations(unittest.TestCase):


    def test_a_failing_status_with_a_required_check_s_name_isn_t_hidden_by_a_passing_run(self):
        h = passing(T0)
        h["status"]["contexts"] = [status("fast", "FAILURE", T0)]
        r = record(pr([commit(A)]), {A: h}, rules=[(None, "fast", None), ("github-actions", "integration", None)])
        head = r["heads"][0]
        self.assertEqual((head["first_attempt"]["outcome"], head["first_attempt"]["reason"]), ("unknown", "commit status history not kept"))
        self.assertEqual((head["latest"]["outcome"], head["latest"]["failed"], head["complete"]), ("fail", ["fast"], False))

    def test_a_rule_bound_to_an_app_id_ignores_other_apps(self):
        rules = [(None, "fast", 1234)]
        other = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), app="unrelated-app", app_id=999))
        self.assertEqual(record(pr([commit(A)]), {A: other}, rules=rules)["heads"][0]["first_attempt"]["reason"], "missing")
        unknown = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), app="unrelated-app"))
        r = record(pr([commit(A)]), {A: unknown}, rules=rules)["heads"][0]
        self.assertEqual((r["first_attempt"]["reason"], r["complete"]), ("required app not identified", False))
        right = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), app="ci-app", app_id=1234))
        self.assertEqual(record(pr([commit(A)]), {A: right}, rules=rules)["heads"][0]["first_attempt"]["outcome"], "pass")

    def latest(self, h, rules):
        return record(pr([commit(A)]), {A: h}, rules=rules)["heads"][0]["latest"]

    def test_an_app_bound_rule_needs_evidence_a_status_came_from_that_app(self):

        for rules in ([(None, "fast", 1234)], [("ci-app", "fast", None)]):
            ineligible = hydrated(statuses=[status("fast", "SUCCESS", T0, required=False)])
            self.assertEqual(self.latest(ineligible, rules), {"outcome": "unknown", "reason": "missing", "failed": []}, rules)
            unidentified = hydrated(statuses=[status("fast", "SUCCESS", T0, required=None)])
            self.assertEqual(self.latest(unidentified, rules)["reason"], "required app not identified", rules)
            other = hydrated(statuses=[status("fast", "SUCCESS", T0, required=None, creator=("Bot", "other-app[bot]"))])
            self.assertEqual(self.latest(other, rules)["outcome"], "unknown", rules)
            person = hydrated(statuses=[status("fast", "SUCCESS", T0, required=None, creator=("User", "someone"))])
            self.assertEqual(self.latest(person, rules)["reason"], "missing", rules)

    def test_evidence_a_status_came_from_the_app_lets_it_count(self):
        required = hydrated(statuses=[status("fast", "SUCCESS", T0, required=True)])
        self.assertEqual(self.latest(required, [(None, "fast", 1234)])["outcome"], "pass")
        bot = hydrated(statuses=[status("fast", "SUCCESS", T0, required=None, creator=("Bot", "ci-app[bot]"))])
        self.assertEqual(self.latest(bot, [("ci-app", "fast", None)])["outcome"], "pass")
        by_id = hydrated(suite(T0, check("lint", "SUCCESS", T0, i=1), app="ci-app", app_id=1234),
                         statuses=[status("fast", "SUCCESS", T0, required=None, creator=("Bot", "ci-app[bot]"))])
        self.assertEqual(self.latest(by_id, [(None, "fast", 1234)])["outcome"], "pass")
        failing = hydrated(statuses=[status("fast", "FAILURE", T0, required=None, creator=("Bot", "ci-app[bot]"))])
        self.assertEqual(self.latest(failing, [("ci-app", "fast", None)])["outcome"], "fail")

    def test_a_rule_naming_no_app_takes_any_status(self):
        h = hydrated(statuses=[status("fast", "SUCCESS", T0, required=False)])
        r = record(pr([commit(A)]), {A: h}, rules=[(None, "fast", None)])["heads"][0]
        self.assertEqual((r["latest"]["outcome"], r["first_attempt"]["reason"]), ("pass", "commit status history not kept"))

    def test_a_matching_run_with_an_unidentified_status_isn_t_a_pass(self):
        h = hydrated(suite(T0, check("fast", "SUCCESS", T0, i=1), app="ci-app"), statuses=[status("fast", "SUCCESS", T0, required=None)])
        self.assertEqual(self.latest(h, [("ci-app", "fast", None)])["reason"], "required app not identified")

    def test_rulesets_keep_their_integration_id(self):
        repo = protection()
        repo["rulesets"]["nodes"] = [{"enforcement": "ACTIVE", "target": "BRANCH",
                                      "conditions": {"refName": {"include": ["refs/heads/main"], "exclude": []}},
                                      "rules": {"pageInfo": {"hasNextPage": False}, "nodes": [{"type": "REQUIRED_STATUS_CHECKS",
                                                "parameters": {"requiredStatusChecks": [{"context": "fast", "integrationId": 1234}]}}]}}]
        self.assertEqual(rc.required_checks(repo), [(None, "fast", 1234)])

    def test_rules_not_returned_whole_are_no_policy(self):
        self.assertIsNone(rc.required_checks({"branchProtectionRules": None, "rulesets": None}))
        repo = protection(("github-actions", "fast"))
        repo["rulesets"]["nodes"] = [{"enforcement": "ACTIVE", "target": "BRANCH",
                                      "conditions": {"refName": {"include": ["~ALL"], "exclude": []}}, "rules": None}]
        self.assertIsNone(rc.required_checks(repo))
        repo = protection(("github-actions", "fast"))
        repo["branchProtectionRules"]["pageInfo"] = {"hasNextPage": True}
        self.assertIsNone(rc.required_checks(repo))

    def test_a_head_whose_last_read_failed_shows_its_older_history_but_isn_t_complete(self):
        h = {**passing(T0), "_read": T0 + 60, "_refresh_error": "GitHub error"}
        r = record(pr([commit(A)]), {A: h})
        self.assertEqual((r["heads"][0]["first_attempt"]["outcome"], r["heads"][0]["complete"], r["heads"][0]["refresh_failed"]),
                         ("pass", False, "GitHub error"))
        self.assertIn("1 heads whose last read failed (older history shown)", r["incomplete"])


class Heads(unittest.TestCase):
    def test_heads_are_observed_tips_ordered_by_first_check_suite(self):
        p = pr([commit(A), commit(B), commit(C)])
        heads = rc.heads_of(p, {A: passing(T0 + 7200), C: passing(T0 + 3600)})
        self.assertEqual([h["sha"][0] for h in heads], ["c", "a"])
        self.assertEqual(heads[0]["observed_via"], "check suite")

    def test_a_push_event_gives_the_exact_push_time(self):
        h = rc.heads_of(pr([commit(A)], updated=T0 + 600), {A: passing(T0 + 600)}, rec(pushes=[("feat", A, T0 + 480)]))[0]
        self.assertEqual((h["observed_via"], h["observed"]), ("push event", T0 + 480))

    def test_a_push_to_another_branch_is_not_this_pr_s(self):
        h = rc.heads_of(pr([commit(A)]), {A: passing(T0 + 600)}, rec(pushes=[("other", A, T0 + 480)]))[0]
        self.assertEqual(h["observed_via"], "check suite")

    def test_sync_needs_two_parents_and_main(self):
        m = commit(M, parents=2, headline="Merge remote-tracking branch 'origin/main' into x")
        o = commit("o" * 40, parents=2, headline="Merge branch 'feature-a' into x")
        heads = rc.heads_of(pr([m, o]), {M: passing(T0), "o" * 40: passing(T0 + 60)})
        self.assertEqual([h["sync"] for h in heads], [True, False])

    def test_a_commit_a_receipt_names_is_a_head_without_checks(self):
        heads = rc.heads_of(pr([commit(A), commit(B)]), {}, rec(opened={1: A}))
        self.assertEqual([h["sha"] for h in heads], [A, B])

    def test_a_head_whose_history_isn_t_read_yet_says_so(self):
        h = record(pr([commit(A)]), {})["heads"][0]
        self.assertEqual((h["first_attempt"]["reason"], h["complete"]), ("history not read yet", False))

    def test_a_force_pushed_head_is_kept_as_replaced(self):
        gone = commit(C)
        p = pr([commit(A)], timeline=[{"__typename": "HeadRefForcePushedEvent", "createdAt": "2026-10-23T01:00:00Z",
                                       "beforeCommit": gone, "afterCommit": {"oid": A}}])
        heads = rc.heads_of(p, {A: passing(T0 + 7200), C: passing(T0 + 60)})
        self.assertEqual([(h["sha"][0], h["replaced"]) for h in heads], [("c", True), ("a", False)])

    def test_trailers_name_an_inferred_family(self):
        self.assertEqual(rc.family_of(["Claude Opus 5.5"]), "claude-opus")
        self.assertEqual(rc.family_of(["Claude Opus 5.5", "Codex"]), "mixed")
        self.assertIsNone(rc.family_of([]))


class Anchors(unittest.TestCase):
    def test_opened_ready(self):
        self.assertEqual(rc.anchor_of(pr([commit(A)])), (T0, "opened"))

    def test_opened_as_a_draft_anchors_at_the_first_ready(self):
        self.assertEqual(rc.anchor_of(pr([commit(A)], timeline=[ready(T0 + 900)])), (T0 + 900, "ready"))

    def test_opened_ready_then_drafted_keeps_the_opening(self):
        self.assertEqual(rc.anchor_of(pr([commit(A)], timeline=[drafted(T0 + 60), ready(T0 + 900)])), (T0, "opened"))

    def test_a_draft_never_ready_has_no_anchor(self):
        self.assertEqual(rc.anchor_of(pr([commit(A)], draft=True)), (None, None))


class R0(unittest.TestCase):
    def setUp(self):
        self.p = pr([commit(A), commit(B)])
        self.hyd = {A: passing(T0 + 5), B: passing(T0 + 86400, workflow="review-gate", run_id=8)}

    def r0(self, p=None, hyd=None, r=None):
        p = p or self.p
        heads = rc.heads_of(p, self.hyd if hyd is None else hyd, r)
        return rc.r0_of(p, heads, r)

    def test_the_opened_event_verifies_it(self):
        x = self.r0(r=rec(opened={1: A}))
        self.assertEqual((x["sha"], x["status"], x["evidence"]), (A, "verified", "opened event"))

    def test_an_opened_event_doesn_t_verify_a_pr_that_opened_as_a_draft(self):
        p = pr([commit(A), commit(B)], timeline=[ready(T0 + 3600)])
        self.assertNotEqual(self.r0(p=p, r=rec(opened={1: A}))["status"], "verified")

    def test_an_opened_receipt_from_another_lifetime_doesn_t_verify(self):
        r = rec(opened={1: C})
        r["opened"][1][0]["t"] = T0 - 86400
        self.assertEqual(self.r0(r=r)["status"], "candidate")

    def test_push_reconstruction_is_supported_not_verified(self):
        r = rec(pushes=[("feat", A, T0 - 60), ("feat", B, T0 + 86000)], coverage=[(T0 - 3600, T0 + 90000)])
        x = self.r0(r=r)
        self.assertEqual((x["sha"], x["status"], x["evidence"], x["delay_s"]), (A, "supported", "push events", 60))

    def test_a_push_before_the_covered_feed_doesn_t_support(self):
        r = rec(pushes=[("feat", A, T0 - 60)], coverage=[(T0 - 30, T0 + 90000)])
        self.assertEqual(self.r0(r=r)["status"], "candidate")

    def test_a_gap_between_the_push_and_the_anchor_doesn_t_support(self):
        r = rec(pushes=[("feat", A, T0 - 60)], coverage=[(T0 - 3600, T0 - 30), (T0 - 10, T0 + 90000)])
        self.assertEqual(self.r0(r=r)["status"], "candidate")

    def test_a_missing_opening_run_with_a_later_update_is_only_a_candidate(self):
        x = self.r0(p=pr([commit(B)]), hyd={B: self.hyd[B]})
        self.assertEqual((x["status"], x["workflow"], x["delay_s"]), ("candidate", "review-gate", 86400))

    def test_pull_request_target_without_a_receipt_is_only_a_candidate(self):
        x = self.r0(p=pr([commit(A)]), hyd={A: passing(T0 + 5, event="pull_request_target")})
        self.assertEqual((x["status"], x["event"], x["evidence"]), ("candidate", "pull_request_target", 'pull_request_target "ci" run on its branch'))

    def test_a_run_for_another_branch_is_no_candidate(self):
        self.assertIsNone(self.r0(p=pr([commit(A)]), hyd={A: passing(T0 + 5, branch="someone-else")}))
        self.assertIsNone(self.r0(p=pr([commit(A)]), hyd={A: passing(T0 + 5, branch=None)}))

    def test_no_receipt_and_no_pr_run_is_unknown(self):
        self.assertIsNone(self.r0(p=pr([commit(A)]), hyd={A: passing(T0 + 5, event="push")}))


class ReceiptOnlyHeads(unittest.TestCase):
    def setUp(self):
        self.p = pr([commit(C)], updated=T0 + 600, timeline=[{
            "__typename": "HeadRefForcePushedEvent", "createdAt": iso(T0 + 120),
            "beforeCommit": commit(B), "afterCommit": {"oid": C}}])
        self.p["isCrossRepository"] = False
        self.rec = rec(opened={1: A}, pushes=[("feat", A, T0 - 60), ("feat", B, T0 + 60)])

    def test_opening_head_survives_normal_then_force_push_without_invented_metadata(self):
        before = copy.deepcopy((self.p, self.rec))
        r = record(self.p, {}, self.rec)
        self.assertEqual({h["sha"] for h in r["heads"]}, {A, B, C})
        a = next(h for h in r["heads"] if h["sha"] == A)
        self.assertEqual((a["family"], a["trailers"], a["sync"], a["replaced"]), (None, [], None, False))
        self.assertEqual((r["r0"]["sha"], r["r0"]["status"], r["r0"]["first_attempt"]["outcome"]),
                         (A, "verified", "unknown"))
        self.assertFalse(a["complete"])
        self.assertIsNone(r["first_decided"])
        self.assertEqual((self.p, self.rec), before)

    def test_push_only_head_is_admitted_during_same_repo_pr_lifetime(self):
        self.rec["opened"] = {}
        self.rec["pushes"][0]["t"] = T0 + 5
        r = record(self.p, {}, self.rec)
        self.assertIn(A, [h["sha"] for h in r["heads"]])
        self.assertIsNone(r["r0"])

    def test_equal_second_pushes_keep_loader_order_for_supported_r0(self):
        self.rec = rec(opened={1: A}, pushes=[("feat", B, T0 - 5), ("feat", A, T0 - 5)],
                       coverage=[(T0 - 60, T0 + 60)])
        self.rec["opened"] = {}
        self.p["commits"]["nodes"].insert(0, {"commit": commit(A)})
        self.rec["pushes"][0]["id"], self.rec["pushes"][1]["id"] = "9", "10"
        r = record(self.p, {}, self.rec)
        self.assertEqual((r["r0"]["sha"], r["r0"]["status"], r["r0"]["receipt"]), (A, "supported", "10"))

    def test_fork_or_legacy_unknown_push_cannot_introduce_a_head(self):
        self.rec["opened"] = {}
        self.rec["pushes"][0]["t"] = T0 + 5
        for cross in (True, None, "false"):
            with self.subTest(cross=cross):
                self.p["isCrossRepository"] = cross
                self.assertNotIn(A, [h["sha"] for h in record(self.p, {}, self.rec)["heads"]])

    def test_number_bound_opening_still_proves_a_fork_head(self):
        self.p["isCrossRepository"] = True
        r = record(self.p, {}, self.rec)
        self.assertIsNotNone(r["r0"])
        self.assertEqual((r["r0"]["sha"], r["r0"]["status"]), (A, "verified"))
        self.assertEqual(r["r0"]["observed_via"], "none")

    def test_opening_event_may_be_timestamped_after_pr_creation(self):
        self.rec["opened"][1][0]["t"] = T0 + 1
        r = record(self.p, {}, self.rec)
        self.assertIsNotNone(r["r0"])
        self.assertEqual(r["r0"]["sha"], A)

    def test_branch_reuse_before_opening_and_after_close_cannot_add_heads(self):
        self.rec["opened"] = {}
        self.p.update(state="CLOSED", closedAt=iso(T0 + 300))
        self.p["timelineItems"]["nodes"].append({"__typename": "ClosedEvent", "createdAt": iso(T0 + 300)})
        for at in (T0 - 1, T0 + 301):
            with self.subTest(at=at):
                self.rec["pushes"][0]["t"] = at
                self.assertNotIn(A, [h["sha"] for h in record(self.p, {}, self.rec)["heads"]])
        self.rec["pushes"][0]["t"] = T0 + 299
        self.assertIn(A, [h["sha"] for h in record(self.p, {}, self.rec)["heads"]])
        self.rec["pushes"][0]["t"] = T0 + 300
        self.assertNotIn(A, [h["sha"] for h in record(self.p, {}, self.rec)["heads"]])

    def test_unrelated_branch_number_time_and_malformed_receipts_cannot_add_heads(self):
        variants = []
        for field, value in (("ref", "refs/heads/other"), ("t", T0 - 1), ("t", T0 + 601),
                             ("t", None), ("t", "bad"), ("t", float("nan")), ("head", "bad"), ("head", "0" * 40)):
            x = rec(pushes=[("feat", A, T0 + 5)])
            x["pushes"][0][field] = value
            variants.append(x)
        variants.append(rec(opened={2: A}))
        for at in (T0 - 1, T0 + 601, None, "bad", float("inf")):
            x = rec(opened={1: A})
            x["opened"][1][0]["t"] = at
            variants.append(x)
        for x in variants:
            with self.subTest(receipts=x):
                try:
                    r = record(self.p, {}, x)
                except (TypeError, ValueError) as e:
                    self.fail(f"Malformed receipt must be rejected without {type(e).__name__}: {e}")
                self.assertEqual({h["sha"] for h in r["heads"]}, {B, C})

    def test_push_before_sha_and_unrelated_hydrated_objects_are_not_ancestry(self):
        self.rec = rec(pushes=[("feat", B, T0 + 5)])
        self.rec["pushes"][0]["before"] = A
        self.assertNotIn(A, [h["sha"] for h in record(self.p, {A: passing(T0 + 5)}, self.rec)["heads"]])

    def test_hydrated_metadata_controls_family_sync_and_work_classification(self):
        hyd = {A: {**commit(A, parents=2, headline="Merge branch 'main' into feat",
                           body="Co-Authored-By: Codex <codex@example.test>"), **passing(T0 + 5)},
               B: passing(T0 + 60, state="FAILURE"), C: passing(T0 + 120)}
        r = record(self.p, hyd, self.rec)
        self.assertIsNotNone(r["r0"])
        self.assertEqual((r["r0"]["family"], r["r0"]["sync"], r["r0"]["first_attempt"]["outcome"]),
                         ("codex", True, "pass"))
        self.assertEqual(r["first_decided"]["sha"], B)

    def test_receipt_head_needs_real_policy_complete_history_and_required_app(self):
        for hyd, rules in (({A: passing(T0 + 5)}, None),
                           ({A: hydrated(suite(T0 + 5, check("fast", "SUCCESS", T0, i=1), app="other"))}, RULES),
                           ({A: hydrated(more=True)}, RULES), ({}, RULES)):
            with self.subTest(hydrated=hyd, rules=rules):
                r = record(self.p, hyd, self.rec, rules=rules)
                self.assertIsNotNone(r["r0"])
                self.assertEqual(r["r0"]["first_attempt"]["outcome"], "unknown")
                self.assertTrue(r["incomplete"])


class OpenIntervalReceipts(unittest.TestCase):

    def setUp(self):
        self.p = pr([commit(C)], updated=T0 + 500, timeline=[self.event("ClosedEvent", 100), self.event("ReopenedEvent", 300)])
        self.p.update(isCrossRepository=False, fetched_at=T0 + 500)

    def event(self, kind, offset):
        return {"__typename": kind, "createdAt": iso(T0 + offset)}

    def result(self, at=200, p=None, opened=False):
        receipts = rec(pushes=[("feat", A, T0 + at)], opened={1: A} if opened else None)
        hyd = {A: {**commit(A, committed=T0 + at), **passing(T0 + at, state="FAILURE")}}
        return record(self.p if p is None else p, hyd, receipts)

    def assert_association(self, at, expected, p=None):
        r = self.result(at, p)
        self.assertEqual(A in {h["sha"] for h in r["heads"]}, expected)
        self.assertEqual(r["first_decided"]["sha"] if r["first_decided"] else None, A if expected else None)

    def test_closed_interval_push_cannot_create_head_or_first_decided_failure(self):
        self.assert_association(200, False)

    def test_open_pushes_and_exact_lifecycle_boundary_policy(self):
        for at, expected in ((-1, False), (0, True), (50, True), (99, True), (100, False),
                             (101, False), (299, False), (300, False), (301, True), (500, True), (501, False)):
            with self.subTest(at=at):
                self.assert_association(at, expected)

    def test_repeated_close_reopen_cycles(self):
        self.p["timelineItems"]["nodes"] = [self.event(k, t) for k, t in
            (("ClosedEvent", 100), ("ReopenedEvent", 200), ("ClosedEvent", 300), ("ReopenedEvent", 400))]
        for at, expected in ((50, True), (150, False), (200, False), (250, True),
                             (300, False), (350, False), (400, False), (450, True)):
            with self.subTest(at=at):
                self.assert_association(at, expected)

    def test_still_closed_and_merged_intervals_require_terminal_evidence(self):
        for kind, state in (("ClosedEvent", "CLOSED"), ("MergedEvent", "MERGED")):
            p = copy.deepcopy(self.p)
            p.update(state=state, closedAt=iso(T0 + 100), mergedAt=iso(T0 + 100) if state == "MERGED" else None)
            p["timelineItems"]["nodes"] = [self.event(kind, 100)]
            for at, expected in ((50, True), (100, False), (200, False)):
                with self.subTest(state=state, at=at):
                    self.assert_association(at, expected, p)

    def test_missing_partial_or_unreadable_history_withholds_unbound_push(self):
        variants = [None, {}, {"pageInfo": {"hasNextPage": False}},
                    {"pageInfo": {"hasNextPage": False}, "nodes": None},
                    {"pageInfo": {"hasNextPage": True}, "nodes": []},
                    {"nodes": []},
                    {"pageInfo": {"hasNextPage": False}, "nodes": [{}]}]
        for tl in variants:
            with self.subTest(timeline=tl):
                p = {**self.p, "timelineItems": tl}
                self.assert_association(50, False, p)
        p = copy.deepcopy(self.p)
        p["gaps"] = ["timelineItems not fully read"]
        self.assert_association(50, False, p)

    def test_reordered_equal_time_and_invalid_event_times_are_not_repaired(self):
        timelines = [[self.event("ReopenedEvent", 300), self.event("ClosedEvent", 100)],
                     [self.event("ClosedEvent", 100), self.event("ReopenedEvent", 100)],
                     [self.event("ClosedEvent", -1), self.event("ReopenedEvent", 300)],
                     [self.event("ClosedEvent", 100), self.event("ReopenedEvent", 501)],
                     [{"__typename": "ClosedEvent"}, self.event("ReopenedEvent", 300)]]
        for nodes in timelines:
            with self.subTest(nodes=nodes):
                self.assert_association(50, False, {**self.p, "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}})

    def test_contradictory_transitions_and_snapshot_state_withhold_push(self):
        timelines = [[self.event("ReopenedEvent", 300)], [self.event("ClosedEvent", 100)],
                     [self.event("ClosedEvent", 100), self.event("ClosedEvent", 200), self.event("ReopenedEvent", 300)],
                     [self.event("MergedEvent", 100), self.event("ReopenedEvent", 300)]]
        for nodes in timelines:
            with self.subTest(nodes=nodes):
                self.assert_association(50, False, {**self.p, "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}})

    def test_terminal_metadata_and_missing_snapshot_contradictions_withhold_push(self):
        for extra in ({"closedAt": iso(T0 + 101)}, {"closedAt": "bad"}, {"mergedAt": iso(T0 + 100)},
                      {"mergedAt": "bad"}, {"fetched_at": None, "updatedAt": None}, {"fetched_at": T0 - 1}):
            with self.subTest(extra=extra):
                self.assert_association(50, False, {**self.p, **extra})
        p = copy.deepcopy(self.p)
        p.update(state="CLOSED", closedAt=None)
        p["timelineItems"]["nodes"] = [self.event("ClosedEvent", 100)]
        self.assert_association(50, False, p)

    def test_complete_empty_lifecycle_proves_continuously_open(self):
        self.p["timelineItems"]["nodes"] = []
        self.assert_association(200, True)

    def test_independently_named_commits_force_heads_and_numbered_opening_survive(self):
        variants = []
        p = copy.deepcopy(self.p)
        p["commits"]["nodes"].insert(0, {"commit": commit(A)})
        variants.append(p)
        p = copy.deepcopy(self.p)
        p["timelineItems"]["nodes"].insert(1, {**self.event("HeadRefForcePushedEvent", 200), "beforeCommit": commit(A), "afterCommit": {"oid": C}})
        variants.append(p)
        p = copy.deepcopy(self.p)
        p["commits"]["nodes"] = [{"commit": commit(A)}]
        p["headRefOid"] = A
        p["timelineItems"]["pageInfo"]["hasNextPage"] = True
        variants.append(p)
        for p in variants:
            with self.subTest(p=p):
                self.assert_association(200, True, p)
                p["timelineItems"]["pageInfo"]["hasNextPage"] = True
                self.assert_association(200, True, p)
        r = self.result(opened=True)
        self.assertEqual((r["r0"]["sha"], r["r0"]["status"]), (A, "verified"))
        self.assertEqual(r["first_decided"]["sha"], A)
        self.p["timelineItems"]["pageInfo"]["hasNextPage"] = True
        r = self.result(opened=True)
        self.assertIn(A, {h["sha"] for h in r["heads"]})
        self.assertIsNone(r["r0"])


class Records(unittest.TestCase):
    def test_the_first_decided_work_head_skips_sync_heads(self):
        m = commit(M, parents=2, headline="Merge branch 'main' into feat")
        r = record(pr([commit(A), m]), {A: passing(T0, state="CANCELLED"), M: passing(T0 + 60)})
        self.assertIsNone(r["first_decided"])
        r = record(pr([commit(A), commit(B)]), {A: passing(T0, state="FAILURE"), B: passing(T0 + 60)})
        self.assertEqual((r["first_decided"]["sha"], r["first_decided"]["outcome"], r["first_decided"]["failed"]), (A, "fail", ["fast"]))

    def test_a_later_poll_leaves_a_pr_s_inputs_unchanged(self):
        p, hyd = pr([commit(A)]), {A: passing(T0 + 5)}
        a = record(p, hyd, rec(coverage=[(T0 - 100, T0 + 100)]))
        b = record(p, hyd, rec(coverage=[(T0 - 100, T0 + 900)]))
        self.assertEqual((a["inputs"], a), (b["inputs"], b))

    def test_a_new_receipt_or_rule_changes_the_inputs(self):
        p, hyd = pr([commit(A)]), {A: passing(T0 + 5)}
        a = record(p, hyd, rec())
        b = record(p, hyd, rec(opened={1: A}))
        self.assertNotEqual(a["inputs"], b["inputs"])
        self.assertNotEqual(a["inputs"], record(p, hyd, rec(), rules=RULES[:1])["inputs"])
        self.assertEqual(b["r0"]["status"], "verified")

    def test_complete_inputs_have_nothing_incomplete(self):
        r = record(pr([commit(A)]), {A: {**passing(T0 + 5), "_read": T0 + 60}})
        self.assertEqual((r["incomplete"], r["history_read"]), ([], [T0 + 60, T0 + 60]))

    def test_history_to_read_again_is_incomplete(self):
        p = pr([commit(A)])
        p["reread_heads"] = True
        self.assertEqual(record(p, {A: passing(T0 + 5)})["incomplete"], ["history to read again"])


def sample():

    trailer = "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    partial = pr([commit("g" * 40)], number=7, ref="seven")
    partial["gaps"] = ["timelineItems not fully read"]
    facts = {
        "backend#1": (pr([commit(A, body=trailer)], number=1), {A: passing(T0 + 5, branch="feat")}),
        "backend#2": (pr([commit(B, body=trailer)], number=2, ref="two"), {B: passing(T0 + 5, state="CANCELLED", branch="two")}),
        "backend#3": (pr([commit(C, body="\n\nCo-Authored-By: Codex <codex@openai.com>")], number=3, ref="three"),
                      {C: passing(T0 + 5, state="FAILURE", branch="three")}),
        "backend#7": (partial, {"g" * 40: passing(T0 + 5, branch="seven")}),
        "web#4": (pr([commit("d" * 40)], number=4, ref="four"), {"d" * 40: passing(T0 + 30, event="pull_request_target", branch="four")}),
        "web#5": (pr([commit("e" * 40)], number=5, ref="five", draft=True), {}),
        "ios#6": (pr([commit("f" * 40)], number=6, ref="six", created=T0 - 30 * 86400), {"f" * 40: passing(T0 - 30 * 86400, branch="six")}),
    }
    recs = {"backend": rec(opened={1: A, 2: B}, pushes=[("three", C, T0 - 30)], coverage=[(T0 - 600, T0 + 600)]),
            "web": rec(), "ios": rec()}
    sources = {"facts": {"discovered": 9, "discovery_complete": True, "unfetched": ["web#8"], "stale": ["backend#3"], "retry": {},
                         "waiting": 2, "last_clean_read": T0 + 3000, "due_heads": 0},
               "events": {"repos": {"backend": {"covered_since": T0 - 600, "polled": T0 + 3500, "gaps": 1,
                                                "gap_list": [{"after": T0 - 900, "before": T0 - 600, "detected": T0 - 300}]}}},
               "run": {"at": T0 + 3600, "errors": []}}
    return rc.derive(facts, recs, T0 + 3600, {"backend": RULES, "web": RULES, "ios": RULES}, sources)


class Summary(unittest.TestCase):
    def test_the_cohort_summary(self):
        s = rc.summary(sample()["prs"], T0 - 86400, T0 + 3600)
        self.assertEqual((s["prs"], s["readiness_unknown"]), (4, 1))
        self.assertEqual(s["r0"], {"verified": 2, "supported": 1, "candidate": 1, "unknown": 0})
        self.assertEqual(s["first_try"]["verified"], {"pass": 1, "fail": 0, "unknown": 1})
        self.assertEqual(s["unknown_reasons"], {"cancelled": 1})
        self.assertEqual(s["trailer_labels"], {"claude-opus": 2, "codex": 1, "none": 1})
        self.assertNotIn("families", s)

    def test_a_trailer_changes_only_the_label_count(self):
        prs = copy.deepcopy(sample()["prs"])
        for record in prs.values():
            if record.get("r0"):
                record["r0"]["family"] = "claude-sonnet"
        base, relabelled = rc.summary(sample()["prs"], T0 - 86400, T0 + 3600), rc.summary(prs, T0 - 86400, T0 + 3600)
        self.assertEqual(relabelled.pop("trailer_labels"), {"claude-sonnet": 4})
        base.pop("trailer_labels")
        self.assertEqual(relabelled, base)

    def test_the_shared_fixture_is_current(self):

        data = sample()
        with open(os.path.join(FIXTURES, "pr-facts.json")) as f:
            self.assertEqual(json.load(f), json.loads(json.dumps(data)))
        with open(os.path.join(FIXTURES, "pr-facts-summary.json")) as f:
            self.assertEqual(json.load(f), {"since": T0 - 86400, "now": T0 + 3600, **rc.summary(data["prs"], T0 - 86400, T0 + 3600)})


if __name__ == "__main__":
    import sys
    if "--write" in sys.argv:
        data = sample()
        with open(os.path.join(FIXTURES, "pr-facts.json"), "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        with open(os.path.join(FIXTURES, "pr-facts-summary.json"), "w") as f:
            json.dump({"since": T0 - 86400, "now": T0 + 3600, **rc.summary(data["prs"], T0 - 86400, T0 + 3600)}, f, indent=1, sort_keys=True)
    else:
        unittest.main()
