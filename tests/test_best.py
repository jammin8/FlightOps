import hashlib
import inspect
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tests"))
import best
import best_evidence as evidence
import best_support as support
from best_support import Origin
from model_analysis.contract import digest as frozen_digest
import models

HEAD = "a" * 40
OTHER = "b" * 40
THREAD = "mcp:stand-in"
KERNEL = ("collectors/model_analysis", "collectors/model_evidence")
KERNEL_PIN = {
    "collectors/model_analysis/__main__.py": "d818dbe52d7b6f6820034a898cbc66e32b450b997def04124bf44488db2bd495",
    "collectors/model_analysis/cohort.py": "9f3accf5ceb89c1ac44cb17a3128ff3f074495da08e7e9dc94cbab142919f861",
    "collectors/model_analysis/contract.py": "c84cd1139aff77c9dea757d9a0a80164428c8870dc25911e1004f68b90571587",
    "collectors/model_analysis/engine.py": "8399c04b001b76cb4a5826101209538b220c0c0ac647268fa2e6aeb47650df8b",
    "collectors/model_analysis/engine_v1.py": "919dad2ce4084f80b611508ca611c7073152050820a21392a235e2d95d0a7f49",
    "collectors/model_evidence/adapter.py": "d784ff19c2026626011126c9fb6960960899954a2c85e5181732aff0ba48b915",
    "collectors/model_evidence/joins_v2.py": "c30c767dd8c1836a4fbeaa754b39f0a43fcaa0203b25deae4dcd2f564c7679b9",
}

KERNEL_FROZEN = {
    "collectors/model_analysis/__init__.py": "d6fd9a0acdddf8a6611f862a26f7bf328167090b2e7e2f224364da6d50b52438",
    "collectors/model_evidence/__init__.py": "9a6f599bcab40b256f0345b568f58601aadd13697926c152496810a1107f5ff6",
    "collectors/model_evidence/__main__.py": "d507b9ee94184f3bf4c3ceb19fa6f22d0d57086a691ec45373f880121b8d39d4",
    "collectors/model_evidence/acquire_cli.py": "cceb8caba070a9cce29079495b094ed5a9e5a197832b6f21b97cb898f8c09daf",
    "collectors/model_evidence/acquisition.py": "506de89e6e9bfdf24330dc18d95b085aec4da9e8d64045fb558fe4c9100db98a",
    "collectors/model_evidence/agent-evidence-v2.schema.json": "1260a8208013397556c5ff20b5a2a6de23ab647d05ffd67bcf3c3ae2ce84c9bb",
    "collectors/model_evidence/agent-evidence.schema.json": "f5b92e8b2f8e1c54d3d5814af6d71316f3fc7af432cfff32d53c386895203e76",
    "collectors/model_evidence/capture_boundary.py": "97fd5f64dcc69090d292e951fac3c00f3ee30f508f755120aef904cf24ccc8d9",
    "collectors/model_evidence/capture_cli.py": "6ece3f99c4cff6e8d34004d91c5291554e72f0f376c7a22e400df2d80e8f8ea7",
    "collectors/model_evidence/capture_contract.py": "b826384bf38d5e0b3919f5ce002b652a6ebb324fd3511dd62d5b337b7afd663e",
    "collectors/model_evidence/capture_ledger.py": "e7793e9338b7676ab1b5932ff9fca62d7ecccb0288636453bedebed479587b7f",
    "collectors/model_evidence/capture_projection.py": "4fb711f4ebcbbe7458735d661ee6162b897167805448efc2fe76cfff2940e577",
    "collectors/model_evidence/contract.py": "595d8c1aa3e1ae68aa3299785b6dec3a91ba5bcaa3a02b630fc154865e9c71c0",
    "collectors/model_evidence/evidence-head-input-v1.schema.json": "b9592c426970ff21f671240feb910e973afdc7b1fc539eaa34bfa88d4eecc50e",
    "collectors/model_evidence/evidence-head-input-v2.schema.json": "58c5c944ef0e447fef28f207c2897ff11351eedd937cc5e4299d44f86ec3d57c",
    "collectors/model_evidence/evidence-head-output-v1.schema.json": "aac0cea832757883a7a0114b4802539d7759aba37364983ce548090fd1179fee",
    "collectors/model_evidence/evidence-head-output-v2.schema.json": "1541c9b8aebfe10e9bee1025fde8b7c24770b3d77c9e6fb0d519de869f807bee",
    "collectors/model_evidence/exporter.py": "eebf90924176b7adad34c3941f05ec35836345b5b332581472cfbfc1b1db1b0f",
    "collectors/model_evidence/ingest.py": "0a1222936b240be936e7135695b794eb13fa97194d600bc503bbbfc73347d3e8",
    "collectors/model_evidence/join_contract.py": "477c8a00b2932c1cd7d1f237d3e7fd018df784e132d0ba27628db5f865fefd36",
    "collectors/model_evidence/join_contract_v2.py": "41fa6e238c457f7402712d30186d9fbaae490df4221d5eb7d43f1b6b0c897124",
    "collectors/model_evidence/joins.py": "e1fc098b6cbc56bce8cca0e42051da94e4a560950f395ddb5dedfb03e9ea37f4",
    "collectors/model_evidence/native.py": "cf6968e76c789619622a9bdc65da17e33c8ed938d155972440aa9e1887a312f2",
    "collectors/model_evidence/observed_contract.py": "cb856bdaf57aba6336185c447527c49d4dd615b32f8fa2831aa2833fcd578796",
    "collectors/model_evidence/operations.py": "f431076cc75d0ff6616e802f9bcfee1b825e509237533bf1905fe72e05a4cb0e",
    "collectors/model_evidence/owner_lookup.py": "4823840f926003df4a96af9fab3f9cafc8bf0f0ca0f12da5f10b007736ef7ba1",
    "collectors/model_evidence/owner_lookup_cli.py": "d09b20150542a81699a6f0338b3d48f42d399272ab96875ca59233cc9b3f6a85",
    "collectors/model_evidence/owner_lookup_contract.py": "aa057966c03360e0bc43e32c91c1243a426b1a254d4e11b4d47c0e16f48d035b",
    "collectors/model_evidence/primary_owner_composition.py": "71ffac58a0c03dc77dd76a1a1975a113783634988a77daffae3d68e39a9abb40",
    "collectors/model_evidence/primary_owner_contract.py": "1c908e3ba0012eef3c64f2689648580f70ad1788032ea3b04feacbae46add9e5",
    "collectors/model_evidence/projection.py": "dc4f7f4a28868ec2d85aadb5402753d9867bb1e0cb556a20749460ace9537201",
    "collectors/model_evidence/replay.py": "04812a49579bfef2de61064a372b660e6a5d95afe6bf72c4d9afbdc703b29d14",
    "collectors/model_evidence/schema.py": "1e1c67116860e0544f87ddf121d613eca624a82eae5dc59fc8c89f05e1e8bd57",
    "collectors/model_evidence/selected-ci-normalized-v1.schema.json": "fe59714f4898c6f7061050fc31eb382e9a0d5209d6a9cc52c1386dc3ab536430",
    "collectors/model_evidence/selected-ci-source-v1.schema.json": "817b3839a7672b9a257e25f498bfa1a663d57db886a6d6eef90d032788c18f98",
    "collectors/model_evidence/selected_ci.py": "de36f009da542fb0874d9ddf051faeff9004995c3283d4aa135d3d7b1353b0be",
    "collectors/model_evidence/selected_ci_contract.py": "9c993d9ce8d6cf5db41a74ea75a96643e17d196bbb58b0b76355f68f5bf02f20",
    "collectors/model_evidence/shell.py": "84ae81105e0948486c3e7d9ab0e756435b14f42792e7e10006e920bdf7a874e2",
    "collectors/model_evidence/usage.py": "5f751e839ffcf602ca7fa5e3a67b6ade824fef3ca74c9cda994c96d1345dc9c8",
}


def stand_in_snapshot(**overrides):
    run = {"id": "run-1", "threadId": THREAD, "status": "completed", "activeAttemptId": "attempt-1", "userMessageId": "user-1", "completedAt": "2026-11-01T01:00:10Z",
           "modelSelection": {"instanceId": "codex", "model": "gpt-6.1-terra", "options": [{"id": "effort", "value": "low"}]}}
    brief = "Repo: example-org/example-ios (https://github.com/example-org/example-ios.git)\nPR: #7\nExpected head SHA (full): " + HEAD + "\nSetup, in this order.\nRepo: <owner/repo>"
    report = "Repo: example-org/example-ios\nPR: #7\nHead SHA: " + HEAD + "\nReviewer family: Codex\nVerdict: APPROVE\nTests run: x"
    projection = {"thread": {"id": THREAD}, "runs": [run], "attempts": [{"id": "attempt-1", "runId": "run-1", "status": "completed"}],
                  "providerTurns": [{"id": "turn-1", "runAttemptId": "attempt-1", "status": "completed", "startedAt": "2026-11-01T01:00:00Z", "completedAt": "2026-11-01T01:00:09Z", "nativeTurnRef": {"nativeId": "n"}}],
                  "turnItems": [{"id": "item-1", "type": "assistant_message", "runId": "run-1", "messageId": "said-1", "providerTurnId": "turn-1", "status": "completed"}],
                  "messages": [{"id": "user-1", "runId": "run-1", "role": "user", "streaming": False, "createdAt": "2026-11-01T01:00:00Z", "text": brief},
                               {"id": "said-1", "runId": "run-1", "role": "assistant", "streaming": False, "createdAt": "2026-11-01T01:00:08Z", "text": report}]}
    return {"projection": projection | overrides}


class Verdicts(unittest.TestCase):
    def test_a_finished_recipe_review_binds_repository_head_run_attempt_and_turn(self):
        record, why = evidence.verdict_of(stand_in_snapshot(), THREAD)
        self.assertIsNone(why)
        self.assertEqual((record["pr"], record["head"], record["run"], record["attempt"], record["turn"]), ("example-org/example-ios#7", HEAD, "run-1", "attempt-1", "turn-1"))

    def test_the_real_review_of_ios_3837_binds_to_its_exact_turn(self):
        data = support.capture()
        thread = "mcp:928f30d2-fa8a-40da-a0ee-2897ba438a4d"
        if thread not in data["snapshots"]:
            self.skipTest("this capture's cohort no longer holds ios#3837")
        record, why = evidence.verdict_of(data["snapshots"][thread], thread)
        self.assertIsNone(why)
        self.assertEqual(record["head"], "18b0b78a92ea3b7c9c2d744995fd172022906f9f")
        item = next(i for i in data["snapshots"][thread]["projection"]["turnItems"] if i["messageId"] == record["message"])
        self.assertEqual(item["providerTurnId"], record["turn"])

    def test_a_report_for_another_head_than_the_brief_is_refused(self):
        snapshot = stand_in_snapshot()
        said = snapshot["projection"]["messages"][1]
        said["text"] = said["text"].replace(HEAD, OTHER)
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "report_disagrees_with_brief"))

    def test_a_second_run_leaves_the_verdict_unbound(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["runs"].append(dict(snapshot["projection"]["runs"][0], id="run-2"))
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "not_one_completed_run"))

    def test_a_cut_history_is_not_a_verdict(self):
        self.assertEqual(evidence.verdict_of(stand_in_snapshot() | {"hasMoreHistory": True}, THREAD), (None, "history_partial"))

    def test_a_clock_overlap_with_another_native_turn_binds_nothing(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["providerTurns"][0]["nativeTurnRef"]["nativeId"] = "different-native-turn"
        del snapshot["projection"]["turnItems"]
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "turn_not_bound"))

    def test_a_verdict_claims_no_native_turn(self):
        record, _ = evidence.verdict_of(stand_in_snapshot(), THREAD)
        self.assertNotIn("native_turn", record)

    def test_a_turn_item_for_another_message_binds_nothing(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["turnItems"][0]["messageId"] = "said-2"
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "turn_not_bound"))

    def test_a_turn_item_without_a_provider_turn_binds_nothing(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["turnItems"][0]["providerTurnId"] = None
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "turn_not_bound"))

    def test_a_turn_of_another_attempt_binds_nothing(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["providerTurns"][0]["runAttemptId"] = "attempt-2"
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "turn_not_bound"))

    def test_two_turn_items_for_one_message_bind_nothing(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["turnItems"].append(dict(snapshot["projection"]["turnItems"][0], id="item-2"))
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "turn_not_bound"))

    def test_a_family_that_disagrees_with_the_requested_harness_is_refused(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["runs"][0]["modelSelection"]["instanceId"] = "claudeAgent"
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "family_disagrees_with_harness"))

    def test_a_title_alone_never_binds_anything(self):
        snapshot = stand_in_snapshot()
        snapshot["projection"]["messages"][1]["text"] = "Looks good."
        self.assertEqual(evidence.verdict_of(snapshot, THREAD), (None, "report_format"))


class Links(unittest.TestCase):
    def row(self, **link):
        good = {"host": "github.com", "repository": "example-org/example-ios", "number": 7, "url": "https://github.com/example-org/example-ios/pull/7", "source": "created", "linkedAt": "x", "snapshot": {}}
        return {"id": "t", "pullRequests": [good | link]}

    def test_an_exact_link_counts_and_its_thread_owns_one_pr(self):
        links, counts, refused = evidence.exact_links([self.row()])
        self.assertEqual((list(links), counts, refused), (["example-org/example-ios#7"], {"t": 1}, 0))

    def test_a_url_that_names_another_pr_is_refused(self):
        self.assertEqual(evidence.exact_links([self.row(url="https://github.com/example-org/example-ios/pull/8")])[2], 1)

    def test_another_host_is_refused(self):
        self.assertEqual(evidence.exact_links([self.row(host="example.com")])[2], 1)


class IndependenceKeys(unittest.TestCase):
    AS_OF = "2026-11-01T03:00:00Z"

    def packet(self, records):
        source = {"id": "github", "schema": "stand-in", "digest": "c" * 64, "snapshot_at": self.AS_OF,
                  "history_start": "2026-01-01T00:00:00Z", "complete_through": self.AS_OF, "complete": False, "gaps": ["synthetic incomplete source"]}
        sources = [source, source | {"id": "orch"}]
        return evidence.normalized(records, sources, evidence.inventory_of(sources), self.AS_OF, self.AS_OF, self.AS_OF)

    def test_a_long_source_identity_fits_the_contract_without_changing_eligibility(self):
        linked = record(links=[{"thread": "synthetic-" + "a" * 247}])
        packet = self.packet([linked])
        manifest = best.build_manifest(packet)
        self.assertEqual(manifest["rows"][0]["membership"], "potential")
        self.assertTrue(all(not proof["qualified"] for proof in packet["evidence"]))
        key = manifest["rows"][0]["independence_keys"][0]
        self.assertLessEqual(len(key.partition(":")[2]), 180)
        self.assertNotIn(linked["links"][0]["thread"], key)

    def test_a_shared_long_thread_still_marks_the_two_prs_correlated(self):
        thread = "synthetic-" + "a" * 247
        packet = self.packet([record(links=[{"thread": thread}]),
                              record(pr="example-org/example-ios#8", number=8, links=[{"thread": thread}])])
        rows = best.build_manifest(packet)["rows"]
        self.assertEqual(rows[0]["independence_keys"], rows[1]["independence_keys"])
        self.assertEqual(rows[0]["correlated_keys"], rows[0]["independence_keys"])
        self.assertEqual(rows[1]["correlated_keys"], rows[1]["independence_keys"])

    def test_distinct_source_ids_stay_distinct_and_repeatably_encoded(self):
        first, second = "a" * 257, "a" * 256 + "b"
        self.assertEqual(evidence.thread_independence_key(first), evidence.thread_independence_key(first))
        self.assertNotEqual(evidence.thread_independence_key(first), evidence.thread_independence_key(second))

    def test_short_keys_and_existing_freeze_inputs_stay_unchanged(self):
        self.assertEqual(evidence.thread_independence_key("synthetic-thread"), "cluster:thread:synthetic-thread")
        self.assertEqual(evidence.thread_independence_key("a" * 173), "cluster:thread:" + "a" * 173)
        self.assertTrue(evidence.thread_independence_key("a" * 174).startswith("cluster:thread-sha256:"))

    def test_whitespace_is_encoded_and_cannot_break_the_contract(self):
        for thread in ("synthetic thread", "synthetic\nthread", "synthetic\tthread"):
            key = evidence.thread_independence_key(thread)
            self.assertTrue(key.startswith("cluster:thread-sha256:"))
            self.assertNotIn(thread, key)
            self.assertTrue(best.build_manifest(self.packet([record(links=[{"thread": thread}])]))["rows"])

    def test_hashed_and_literal_source_id_namespaces_cannot_alias(self):
        hashed = evidence.thread_independence_key("a" * 257)
        self.assertNotEqual(hashed, evidence.thread_independence_key(hashed.removeprefix("cluster:")))


class Heads(unittest.TestCase):
    verdict = {"head": HEAD}

    def test_github_and_verdict_agree(self):
        self.assertEqual(evidence.head_state(HEAD, None, [self.verdict]), ("exact", HEAD))

    def test_a_verdict_on_an_older_head_is_not_exact(self):
        self.assertEqual(evidence.head_state(OTHER, None, [self.verdict]), ("verdicts_on_other_heads", OTHER))

    def test_a_verdict_alone_is_labelled_verdict_only(self):
        self.assertEqual(evidence.head_state(None, None, [self.verdict]), ("verdict_only", HEAD))

    def test_two_verdict_heads_without_github_pick_nothing(self):
        self.assertEqual(evidence.head_state(None, None, [self.verdict, {"head": OTHER}]), ("ambiguous", None))

    def test_a_merged_row_head_outranks_the_last_sighting(self):
        self.assertEqual(evidence.head_state(OTHER, HEAD, [self.verdict])[0], "verdicts_on_other_heads")

    def test_the_last_open_sighting_is_labelled_as_such(self):
        self.assertEqual(evidence.head_state(None, HEAD, [self.verdict]), ("exact_at_last_sighting", HEAD))


class Ledger(unittest.TestCase):
    def repos(self, at, head=HEAD, checks=()):
        return {"fetched": at, "prs": [{"repo": "ios", "number": 7, "head": head, "ci": "passed", "check_runs": list(checks), "check_runs_complete": True}]}

    def test_a_sighting_keeps_its_first_clock_and_advances_its_last(self):
        ledger = evidence.sighted(evidence.sighted({}, self.repos(10)), self.repos(20))
        seen = ledger["example-org/example-ios#7"][HEAD]
        self.assertEqual((seen["first_seen"], seen["last_seen"]), (10, 20))

    def test_a_new_head_does_not_rewrite_the_old_one(self):
        ledger = evidence.sighted(evidence.sighted({}, self.repos(10)), self.repos(20, OTHER))
        self.assertEqual(evidence.last_sighted(ledger, "example-org/example-ios#7"), OTHER)
        self.assertEqual(ledger["example-org/example-ios#7"][HEAD]["last_seen"], 10)

    def test_ci_on_the_head_counts_without_claiming_the_first_attempt(self):
        check = {"kind": "check_run", "name": "fast", "id": 1, "status": "COMPLETED", "conclusion": "FAILURE", "started": "s"}
        ledger = evidence.sighted({}, self.repos(10, checks=[check]))
        ci = evidence.ci_at(ledger, "example-org/example-ios#7", HEAD)
        self.assertEqual((ci["state"], ci["failed"], ci["reason"]), ("observed", 1, "first_attempt_unknown"))

    def test_ci_on_another_head_is_unknown(self):
        ledger = evidence.sighted({}, self.repos(10))
        self.assertEqual(evidence.ci_at(ledger, "example-org/example-ios#7", OTHER)["state"], "unknown")


def record(**fields):
    return {"pr": "example-org/example-ios#7", "repo": "example-org/example-ios", "number": 7, "state": "merged", "merged_at": "2026-11-01T02:00:00Z", "opened_at": "2026-10-31T02:00:00Z",
            "links": [], "verdicts": [], "head_state": "exact", "exact_head": HEAD, "github_head": HEAD, "ci": {"state": "unknown", "reason": "head_not_sighted_open"},
            "usage": {"runs": 1, "runs_with_usage": 1, "fresh_tokens_on_linked_threads": 9},
            "review_usage": {"runs": 0, "runs_with_usage": 0, "fresh_tokens_on_review_runs": None}, "thread_prs": 1, "merged_agrees": False, "witness": None} | fields


class Qualification(unittest.TestCase):
    AS_OF = "2026-11-01T03:00:00Z"

    def packet(self, complete=False):
        row = {"id": "github", "schema": "stand-in", "digest": "c" * 64, "snapshot_at": self.AS_OF, "history_start": "2026-01-01T00:00:00Z", "complete_through": self.AS_OF,
               "complete": complete, "gaps": [] if complete else ["stand-in gap"]}
        rows = [row, row | {"id": "orch"}]
        return evidence.normalized([record()], rows, evidence.inventory_of(rows), self.AS_OF, self.AS_OF, self.AS_OF)

    def test_no_proof_is_qualified_even_on_complete_sources(self):
        self.assertFalse(any(p["qualified"] for p in self.packet(complete=True)["evidence"]))

    def test_every_proof_says_why_it_is_unqualified(self):
        self.assertTrue(all(p["unqualified_reason"] for p in self.packet()["evidence"]))

    def test_a_witness_dictionary_has_no_way_in(self):
        with self.assertRaises(TypeError):
            evidence.proof(record(), "exact_producer", self.AS_OF, {"github", "orch", "usage"}, {"example-org/example-ios#7": {"exact_producer": {"head": HEAD, "source": "github", "at": "2026-10-31T02:00:00Z"}}})

    def test_no_entry_point_takes_witnesses(self):
        for function in (best.collect, best.compute, evidence.proof, evidence.observation, evidence.normalized):
            self.assertNotIn("witnesses", inspect.signature(function).parameters)

    def test_complete_sources_alone_earn_no_membership(self):
        self.assertEqual(best.build_manifest(self.packet(complete=True))["rows"][0]["membership"], "potential")

    def test_every_measure_needs_the_frozen_claims(self):
        for measure in evidence.MEASURES:
            self.assertLessEqual(best.build_manifest.__globals__["validate"].__globals__["CLAIMS"][measure["semantics"]], set(measure["required_claims"]))

    def test_a_shared_thread_is_a_shared_unit(self):
        row, _ = evidence.observation(record(thread_prs=3), self.AS_OF, self.AS_OF)
        self.assertIs(row["shared"], True)

    def test_a_pr_without_any_head_still_stays_in_the_denominator(self):
        row, proofs = evidence.observation(record(exact_head=None, head_state="unknown"), self.AS_OF, self.AS_OF)
        self.assertEqual((proofs, row["independence_keys"]), ([], ["stack:pr:example-org/example-ios#7"]))

    def test_settings_stay_requested_and_tier_unknown(self):
        self.assertEqual({(s["provider"]["basis"], s["tier"]["basis"]) for s in evidence.SETUPS}, {("requested", "unknown")})


class RealCapture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = support.capture()
        cls.out, cls.root, cls.orch = support.published(cls.data)
        cls.decision, cls.shown = json.loads(cls.out["decision"]), json.loads(cls.out["presentation"])

    def test_every_measure_is_insufficient_with_computed_gates(self):
        for measure in self.decision["measures"].values():
            self.assertFalse(measure["gates"]["two_exact_reported_setups"])
            self.assertFalse(measure["gates"]["inventory_complete"])
            self.assertEqual({g["status"] for g in measure["groups"].values()}, {"InsufficientEvidence"})

    def test_the_fixture_is_this_capture_recomputed(self):
        self.assertEqual(self.out, json.loads(support.PUBLICATION.read_text()))

    def test_the_real_merged_pr_is_traced_with_its_exact_identities(self):
        trace = next(t for t in self.shown["traces"] if t["pr"] == support.TARGET)
        stages = {s["name"]: s for s in trace["stages"]}
        self.assertEqual(trace["state"], "merged")
        self.assertEqual(trace["head"], "4cfaa6acbe03944bb583da55f3de9069dbadfc9a")
        self.assertIn("mcp:eebd6b7b-5dc0-486c-a076-e7bb74bd20e7", stages["Run"]["detail"])
        self.assertIn("turn item", stages["Run"]["detail"])
        self.assertEqual(stages["Setup"]["state"], "unknown")
        self.assertIn("thread_links_several_prs", " ".join(trace["reasons"]["fresh_tokens"]))

    def test_the_two_claude_family_is_labelled_diagnostic(self):
        self.assertEqual(self.shown["family"]["status"], "diagnostic")
        self.assertEqual(self.shown["family"]["setups"], self.decision["policy"]["family"])
        self.assertIn("not the production comparison", self.shown["family"]["note"])

    def run_stage(self, head, state):
        verdict = {"pr": "example-org/example-ios#7", "head": "b" * 40, "verdict": "APPROVE", "family": "Claude", "thread": "t", "run": "r", "attempt": "a", "turn": "u", "message": "m",
                   "requested": {"model": "claude-sonnet-5-5", "effort": "medium"}}
        trace = evidence.trace_of(record(verdicts=[verdict], github_head=head, exact_head=head, head_state=state), {"measures": {}})
        return next(s for s in trace["stages"] if s["name"] == "Run")["detail"]

    def test_a_review_of_another_head_shows_its_sha_beside_its_run(self):
        self.assertIn("run r, reviewed head " + "b" * 40, self.run_stage("a" * 40, "verdicts_on_other_heads"))

    def test_a_review_of_the_shown_head_repeats_no_sha(self):
        self.assertNotIn("reviewed head", self.run_stage("b" * 40, "exact"))

    def test_counts_are_real_rows_not_zeros(self):
        population = next(iter(self.decision["measures"]["fresh_tokens"]["populations"].values()))
        self.assertEqual(len(population["potential_denominator_ids"]), self.shown["counts"]["prs"])
        self.assertGreater(population["reasons"]["receipt_missing:exact_producer"], 0)

    def test_the_presentation_is_bound_to_this_exact_decision(self):
        self.assertEqual((self.shown["decision_hash"], self.shown["manifest_hash"], self.shown["input_hash"]), (self.decision["decision_hash"], self.decision["manifest_hash"], self.decision["input_hash"]))
        self.assertEqual(self.shown["presentation_hash"], evidence.digest({k: v for k, v in self.shown.items() if k != "presentation_hash"}))

    def test_the_retained_freeze_replays_to_the_published_decision(self):
        directory = os.path.join(self.root, "cache", "best-freeze")
        frozen = os.path.join(directory, self.decision["manifest_hash"] + ".json")
        replayed = os.path.join(self.root, "replayed.json")
        subprocess.run([sys.executable, "-m", "collectors.model_analysis", "replay", frozen, "--output", replayed], cwd=ROOT, check=True, capture_output=True)
        self.assertEqual(json.loads(pathlib.Path(replayed).read_text())["decision_hash"], self.decision["decision_hash"])

    def test_no_prompt_or_report_text_is_published(self):
        text = json.dumps(self.out)
        for phrase in ("independent code reviewer", "Tests run", "omitted from this capture", "git checkout"):
            self.assertNotIn(phrase, text)

    def test_finished_reviews_are_read_once(self):
        reads = [call for call in self.orch.calls if call != "shell"]
        self.assertEqual(len(set(reads)), len(reads))

    def test_the_stand_in_mark_is_in_the_frozen_manifest_and_the_presentation(self):
        frozen = json.loads(pathlib.Path(self.root, "cache", "best-freeze", self.decision["manifest_hash"] + ".json").read_text())
        marked = lambda rows: next(row["schema"] for row in rows if row["id"] == "orch")
        self.assertEqual(marked(frozen["manifest"]["sources"]), evidence.STAND_IN_SCHEMA)
        self.assertEqual(marked(self.shown["sources"]), evidence.STAND_IN_SCHEMA)

    def test_the_checked_in_publication_carries_the_mark(self):
        shown = json.loads(json.loads(support.PUBLICATION.read_text())["presentation"])
        self.assertEqual(next(row["schema"] for row in shown["sources"] if row["id"] == "orch"), evidence.STAND_IN_SCHEMA)


class Provenance(unittest.TestCase):
    def live(self, data):
        root, token = support.data_dir(data)
        return root, best.LiveOrch("http://127.0.0.1:3773", token)

    def test_a_recording_served_to_the_live_client_is_refused(self):
        data = support.capture()
        root, orch = self.live(data)
        with unittest.mock.patch.object(best.orch_api, "open_local", support.Transport(data)):
            with self.assertRaisesRegex(models.Failure, "stand-in recording"):
                best.collect(root, orch, clock=lambda: data["capture"]["made_at"])
        self.assertNotIn("best-reviews.json", os.listdir(os.path.join(root, "cache")))

    def test_a_source_that_declares_the_live_schema_stays_a_stand_in(self):
        class Claiming(support.StandInOrch):
            schema = evidence.ORCH_SCHEMA
        data = support.capture()
        out = best.collect(support.data_dir(data)[0], Claiming(data), clock=lambda: data["capture"]["made_at"])
        self.assertEqual(next(s["schema"] for s in json.loads(out["manifest"])["sources"] if s["id"] == "orch"), evidence.STAND_IN_SCHEMA)

    def test_a_subclass_of_the_live_client_stays_a_stand_in(self):
        class Measured(best.LiveOrch):
            pass
        self.assertEqual(best.source_schema(Measured()), evidence.STAND_IN_SCHEMA)
        self.assertEqual(best.source_schema(best.LiveOrch()), evidence.ORCH_SCHEMA)

    def test_the_publication_carries_the_manifest_its_decision_names(self):
        out, _, _ = support.published(support.capture())
        manifest, decision = json.loads(out["manifest"]), json.loads(out["decision"])
        self.assertEqual(manifest["manifest_hash"], decision["manifest_hash"])
        self.assertEqual(frozen_digest({k: v for k, v in manifest.items() if k != "manifest_hash"}), decision["manifest_hash"])

    def test_a_recording_without_its_mark_cannot_become_a_stand_in(self):
        data = support.capture()
        del data["shell"][evidence.STAND_IN_MARK]
        with self.assertRaises(ValueError):
            support.StandInOrch(data)

    def test_only_get_reaches_orch(self):
        data = support.capture()
        del data["shell"][evidence.STAND_IN_MARK]
        _, orch = self.live(data)
        transport = support.Transport(data)
        with unittest.mock.patch.object(best.orch_api, "open_local", transport):
            orch.shell()
            orch.thread(next(iter(data["snapshots"])))
        self.assertEqual(set(transport.calls), {"GET"})

    def test_the_collector_starts_the_live_client(self):
        self.assertIn('else ".", LiveOrch())', (ROOT / "collectors" / "best.py").read_text())


class Redirects(unittest.TestCase):
    def origin(self, answer):
        origin = Origin(answer)
        self.addCleanup(origin.close)
        return origin

    def test_a_redirected_read_never_carries_the_token(self):
        other = self.origin((200, {}, b"{}"))
        local = self.origin((302, {"Location": other.base + "/stolen"}, b""))
        _, token = support.data_dir(support.capture())
        orch = best.LiveOrch(local.base, token)
        with self.assertRaisesRegex(models.Failure, "Orchestrator answered 302"):
            best.shell_rows(orch)
        with self.assertRaises(best.orch_api.Refused):
            orch.thread("mcp:one")
        self.assertEqual(len(local.seen), 2)
        self.assertEqual(other.seen, [])

    def test_a_proxy_setting_never_sees_a_read(self):
        proxy = self.origin((200, {}, b"{}"))
        local = self.origin((200, {}, json.dumps({"projects": [], "threads": [], "archivedThreads": []}).encode()))
        _, token = support.data_dir(support.capture())
        with unittest.mock.patch.dict(os.environ, {"http_proxy": proxy.base, "HTTP_PROXY": proxy.base, "no_proxy": "", "NO_PROXY": ""}):
            best.LiveOrch(local.base, token).shell()
        self.assertEqual(proxy.seen, [])

    def test_no_read_can_use_the_default_opener(self):
        self.assertNotIn("urlopen", (ROOT / "collectors" / "best.py").read_text())


class Refusals(unittest.TestCase):
    def setUp(self):
        self.data = support.capture()
        self.root, self.token = support.data_dir(self.data)
        self.collect = lambda orch=None: best.collect(self.root, orch or support.StandInOrch(self.data), clock=lambda: self.data["capture"]["made_at"])

    def test_stale_github_produces_no_generation(self):
        self.data["repos"]["fetched"] -= 7200
        self.root, self.token = support.data_dir(self.data)
        with self.assertRaisesRegex(models.Failure, "GitHub collection is stale"):
            self.collect()

    def test_a_non_loopback_orch_is_refused(self):
        with self.assertRaisesRegex(models.Failure, "not local"):
            self.collect(best.LiveOrch("http://example.com:3773", self.token))


    def test_a_failed_pass_leaves_no_new_cache(self):
        self.data["models"]["generated"] -= 7200
        self.root, self.token = support.data_dir(self.data)
        with self.assertRaises(models.Failure):
            self.collect()
        self.assertNotIn("best-freeze", os.listdir(os.path.join(self.root, "cache")))

    def test_a_contract_refusal_preserves_the_last_good_publication_and_freeze(self):
        out = self.collect()
        publication = pathlib.Path(self.root, "raw", "best.json")
        publication.write_text(json.dumps(out))
        paths = [publication, *pathlib.Path(self.root, "cache", "best-freeze").glob("*.json")]
        before = {path: path.read_bytes() for path in paths}
        with unittest.mock.patch.object(best.resource, "setrlimit"), unittest.mock.patch.object(best, "build_manifest", side_effect=best.ContractError("independence keys required")):
            refused = best.run(self.root, support.StandInOrch(self.data), clock=lambda: self.data["capture"]["made_at"])
        self.assertEqual(refused["failure_code"], "invalid_independence_keys")
        self.assertNotIn("generated", refused)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_a_long_linked_thread_can_publish_with_the_original_source_clocks(self):
        linked = {"id": "synthetic-" + "a" * 247, "title": "", "status": "completed", "pullRequests": [{"host": "github.com",
                  "repository": "example-org/example-backend", "number": 6991, "url": "https://github.com/example-org/example-backend/pull/6991"}]}
        self.data["shell"]["threads"].append(linked)
        out = self.collect()
        manifest = json.loads(out["manifest"])
        source = next(row for row in manifest["sources"] if row["id"] == "github")
        self.assertEqual(source["snapshot_at"], evidence.stamp(self.data["repos"]["fetched"]))
        row = next(row for row in manifest["rows"] if row["pr_id"] == support.TARGET)
        self.assertIn(evidence.thread_independence_key(linked["id"]), row["independence_keys"])
        self.assertEqual(row["membership"], "potential")
        self.assertEqual(out["generated"], self.data["capture"]["made_at"])


class ReviewUsage(unittest.TestCase):
    TOKENS = {"input": 5, "cache_read": 0, "cache_write": 0, "output": 0}
    VERDICT = {"thread": "t", "run": "r1"}

    def test_usage_is_the_named_run_alone(self):
        runs = {"r1": {"thread": "t", "tokens": self.TOKENS}, "r2": {"thread": "t", "tokens": self.TOKENS | {"input": 100}}}
        self.assertEqual(evidence.review_usage_of([self.VERDICT], runs)["fresh_tokens_on_review_runs"], 5)

    def test_a_named_run_missing_from_the_report_stays_unknown(self):
        runs = {"r2": {"thread": "t", "tokens": self.TOKENS}}
        self.assertIsNone(evidence.review_usage_of([self.VERDICT], runs)["fresh_tokens_on_review_runs"])

    def test_a_run_on_another_thread_stays_unknown(self):
        runs = {"r1": {"thread": "other", "tokens": self.TOKENS}}
        self.assertIsNone(evidence.review_usage_of([self.VERDICT], runs)["fresh_tokens_on_review_runs"])

    def test_a_run_without_tokens_stays_unknown(self):
        self.assertIsNone(evidence.review_usage_of([self.VERDICT], {"r1": {"thread": "t", "tokens": None}})["fresh_tokens_on_review_runs"])

    def test_one_unknown_run_of_two_leaves_the_total_unknown(self):
        verdicts = [self.VERDICT, {"thread": "t", "run": "r2"}]
        self.assertIsNone(evidence.review_usage_of(verdicts, {"r1": {"thread": "t", "tokens": self.TOKENS}})["fresh_tokens_on_review_runs"])

    def test_two_verdicts_on_one_run_count_it_once(self):
        runs = {"r1": {"thread": "t", "tokens": self.TOKENS}}
        self.assertEqual(evidence.review_usage_of([self.VERDICT, self.VERDICT], runs)["fresh_tokens_on_review_runs"], 5)


class Clocks(unittest.TestCase):
    def setUp(self):
        self.data = support.capture()
        self.at = self.data["capture"]["made_at"]

    def shown(self, out):
        return {row["id"]: row for row in json.loads(out["presentation"])["sources"]}

    def test_a_cached_verdict_keeps_its_first_observation_time(self):
        root, _ = support.data_dir(self.data)
        orch = support.StandInOrch(self.data)
        best.collect(root, orch, clock=lambda: self.at)
        later = best.collect(root, orch, clock=lambda: self.at + 100)
        self.assertEqual(self.shown(later)["orch"]["snapshot_at"], evidence.stamp(self.at))
        self.assertEqual(json.loads(later["decision"])["as_of"], evidence.stamp(self.at + 100))

    def test_a_cache_entry_with_no_observation_time_is_read_again(self):
        row = next(r for r in self.data["shell"]["threads"] if r["id"] in self.data["snapshots"]) | {"status": "completed"}
        orch = support.StandInOrch(self.data)
        best.read_reviews([row], {row["id"]: {"updated": row["updatedAt"], "record": None, "refused": "report_format"}}, orch, lambda: self.at)
        self.assertEqual(orch.calls, [row["id"]])

    def test_a_github_clock_inside_the_tolerance_is_published_as_fetched(self):
        self.data["repos"]["fetched"] = self.at + 4
        root, _ = support.data_dir(self.data)
        out = best.collect(root, support.StandInOrch(self.data), clock=lambda: self.at)
        self.assertEqual(self.shown(out)["github"]["snapshot_at"], evidence.stamp(self.at + 4))

    def test_the_freeze_reaches_past_a_later_github_clock_and_as_of_stays(self):
        self.data["repos"]["fetched"] = self.at + 4
        root, _ = support.data_dir(self.data)
        out = best.collect(root, support.StandInOrch(self.data), clock=lambda: self.at)
        decision = json.loads(out["decision"])
        frozen = json.loads(pathlib.Path(root, "cache", "best-freeze", decision["manifest_hash"] + ".json").read_text())
        self.assertEqual((frozen["manifest"]["as_of"], frozen["manifest"]["frozen_at"]), (evidence.stamp(self.at), evidence.stamp(self.at + 4)))

    def test_a_fractional_github_clock_is_not_truncated(self):
        self.assertEqual(evidence.stamp(1000.25), "1970-01-01T00:16:40.250000Z")


class ModelsSource(unittest.TestCase):
    def raw(self, report, error=None):
        directory = tempfile.mkdtemp()
        with open(os.path.join(directory, "models.json"), "w") as f:
            json.dump(report, f)
        if error:
            with open(os.path.join(directory, "models.error"), "w") as f:
                f.write(error)
        return directory

    def test_a_failed_refresh_stops_the_pass_even_with_recent_bytes(self):
        with self.assertRaisesRegex(models.Failure, "Models collection failed: Orchestrator unreachable"):
            best.usage(self.raw({"schema": models.SCHEMA, "generated": 1000, "runs": {}}, "Orchestrator unreachable"), 1010)

    def test_a_report_of_another_schema_is_unreadable(self):
        with self.assertRaisesRegex(models.Failure, "Models report unreadable"):
            best.usage(self.raw({"schema": "other", "generated": 1000, "runs": {}}), 1010)

    def test_a_current_report_with_no_error_is_used(self):
        self.assertEqual(best.usage(self.raw({"schema": models.SCHEMA, "generated": 1000, "runs": {}}), 1010)[0]["generated"], 1000)


class GitHubCoverage(unittest.TestCase):
    def test_a_partial_inventory_stops_the_pass(self):
        data = support.capture()
        data["repos"]["repos"][0]["truncated"] = True
        root, _ = support.data_dir(data)
        with self.assertRaisesRegex(models.Failure, "GitHub collection is partial"):
            best.github(os.path.join(root, "raw"), data["repos"]["fetched"])


class Reviews(unittest.TestCase):
    def test_an_unfinished_review_counts_as_unread_and_is_never_read(self):
        data = support.capture()
        row = data["shell"]["threads"][0] | {"status": "running"}
        orch = support.StandInOrch(data)
        _, _, unread, entries = best.read_reviews([row], {}, orch, lambda: 0)
        self.assertEqual((unread, entries, orch.calls), (1, {}, []))

    def test_the_read_budget_leaves_the_rest_unread(self):
        data = support.capture()
        rows = [r for r in data["shell"]["threads"] if r["id"] in data["snapshots"]]
        _, _, unread, entries = best.read_reviews(rows, {}, support.StandInOrch(data), lambda: 0)
        self.assertEqual((len(entries), unread), (best.READS, len(rows) - best.READS))

    def test_the_cache_holds_no_text(self):
        data = support.capture()
        rows = [r for r in data["shell"]["threads"] if r["id"] in data["snapshots"]]
        _, _, _, entries = best.read_reviews(rows, {}, support.StandInOrch(data), lambda: 0)
        self.assertNotIn("omitted from this capture", json.dumps(entries))


class Budget(unittest.TestCase):
    def setUp(self):
        self.data = support.capture()
        self.root, self.token = support.data_dir(self.data)

    def oversized(self, *path):
        with open(os.path.join(self.root, *path), "wb") as f:
            f.truncate(best.SOURCE_BYTES + 1)

    def main(self):
        answer = subprocess.run([sys.executable, "-I", str(ROOT / "collectors" / "best.py"), self.root], capture_output=True, text=True, check=True)
        return json.loads(answer.stdout)["error"]

    def test_the_ledger_caps_stay_at_the_measured_size(self):
        self.assertLessEqual(evidence.LEDGER_PRS * evidence.LEDGER_HEADS * evidence.LEDGER_ROWS, 108000)


    def test_an_oversized_github_file_is_refused_before_it_is_parsed(self):
        self.oversized("raw", "repos.json")
        self.assertEqual(self.main(), "repos.json exceeds 32 MiB")

    def test_an_oversized_models_file_is_refused_before_it_is_read(self):
        self.oversized("raw", "models.json")
        with self.assertRaisesRegex(models.Failure, "models.json exceeds 32 MiB"):
            best.usage(os.path.join(self.root, "raw"), self.data["capture"]["made_at"])

    def test_an_oversized_shell_is_refused(self):
        orch = best.LiveOrch("http://127.0.0.1:3773", self.token)
        with unittest.mock.patch.object(best, "SOURCE_BYTES", 10), unittest.mock.patch.object(best.orch_api, "open_local", support.Transport(self.data)):
            with self.assertRaisesRegex(models.Failure, "Orchestrator shell exceeds"):
                best.shell_rows(orch)

    def test_an_oversized_review_thread_is_refused_by_reason_and_not_read_again(self):
        row = next(r for r in self.data["shell"]["threads"] if r["id"] in self.data["snapshots"]) | {"status": "completed"}
        orch = unittest.mock.Mock()
        orch.thread.side_effect = best.orch_api.TooLarge()
        _, refused, unread, entries = best.read_reviews([row], {}, orch, lambda: 1)
        self.assertEqual((refused, unread), ({"thread_too_large": 1}, 0))
        best.read_reviews([row], entries, orch, lambda: 2)
        self.assertEqual(orch.thread.call_count, 1)

    def test_a_pass_past_the_memory_ceiling_refuses_and_leaves_the_caches_alone(self):
        script = f"""
import json, sys
sys.path.insert(0, {str(ROOT / 'collectors')!r})
import best
class Huge:
    def shell(self):
        return bytearray(best.MEMORY_BYTES)
print(json.dumps(best.run({self.root!r}, Huge(), clock=lambda: {self.data['capture']['made_at']})))
"""
        answer = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(answer.stdout)["error"], "input exceeds the 290 MiB memory budget")
        self.assertNotIn("best-reviews.json", os.listdir(os.path.join(self.root, "cache")))


def ledger_row(**fields):
    return {"binding": "bound", "head_at_open": HEAD, "head_at_open_status": "exact", "head_at_open_basis": "activity-chain", "head_at_open_event": 9, "head_at_open_candidates": None,
            "first_ready_at": "2026-10-31T02:00:00Z", "first_ready_at_basis": "ready-for-review-event", "first_ready_head": HEAD, "first_ready_head_status": "exact", "first_ready_head_basis": "activity-chain",
            "first_ready_head_event": 9, "first_ready_head_candidates": None, "final_head_check": "in-incarnation"} | fields


class Ledger(unittest.TestCase):
    AS_OF = "2026-11-01T03:00:00Z"

    def setUp(self):
        self.data = support.capture()
        self.at = self.data["capture"]["made_at"]

    def published(self, data=None):
        data = data or self.data
        root, _ = support.data_dir(data)
        out = best.collect(root, support.StandInOrch(data), clock=lambda: self.at)
        return out, json.loads(out["manifest"]), json.loads(out["presentation"])

    def test_real_ledger_rows_reach_the_manifest_and_the_trace_with_their_basis(self):
        out, manifest, shown = self.published()
        row = next(r for r in self.data["witness"]["rows"] if (r["repo"], r["number"]) == ("backend", 6991))
        self.assertEqual(next(r for r in manifest["rows"] if r["pr_id"] == support.TARGET)["first_ready_at"], row["first_ready_at"])
        stage = next(s for t in shown["traces"] for s in t["stages"] if s["name"] == "First ready" and "evidence ledger" in s["detail"])
        self.assertIn("unqualified", stage["detail"])
        self.assertGreater(shown["counts"]["first_ready_seen"], 0)

    def test_the_source_row_names_the_ledger_and_is_never_complete(self):
        _, manifest, _ = self.published()
        source = next(s for s in manifest["sources"] if s["id"] == evidence.LEDGER_SOURCE)
        self.assertEqual((source["schema"], source["complete"]), (evidence.LEDGER_SCHEMA, False))
        self.assertIn("no source-specific rule qualifies this evidence", source["gaps"])

    def test_the_trace_gives_first_ready_its_provenance_and_stays_unqualified(self):
        stages = {s["name"]: s for s in evidence.ledger_stages(record(witness=evidence.witness_of(ledger_row(), ["timeline-truncated"])))}
        self.assertEqual(stages["First ready"]["state"], "observed")
        for piece in ("evidence ledger", "2026-10-31T02:00:00Z", "ready-for-review-event", "timeline-truncated", "unqualified"):
            self.assertIn(piece, stages["First ready"]["detail"])
        self.assertEqual(stages["Head at open"]["state"], "exact")

    def test_a_tied_head_names_no_sha_and_keeps_its_candidates(self):
        tied = evidence.witness_of(ledger_row(head_at_open=None, head_at_open_status="tied", head_at_open_candidates=json.dumps(["a" * 40, "b" * 40])), [])
        self.assertEqual(tied["head_at_open"]["candidates"], ["a" * 40, "b" * 40])
        stage = evidence.ledger_stages(record(witness=tied))[0]
        self.assertEqual(stage["state"], "unknown")
        self.assertIn("2 candidates", stage["detail"])

    def test_an_exact_ledger_row_leaves_every_proof_unqualified(self):
        row, proofs = evidence.observation(record(witness=evidence.witness_of(ledger_row(), [])), self.AS_OF, self.AS_OF)
        reasons = {p["claim"]: (p["qualified"], p["unqualified_reason"]) for p in proofs}
        self.assertEqual(reasons["first_ready"], (False, "no_source_rule_for_first_ready"))
        self.assertEqual(reasons["verified_anchor"], (False, "no_source_rule_for_anchor"))
        self.assertFalse(any(p["qualified"] for p in proofs))
        self.assertEqual((row["anchor"]["kind"], row["anchor"]["r0_sha"]), ("unknown", None))

    def test_the_ledger_changes_no_call(self):
        calls = lambda out: {m: {g: fact["status"] for g, fact in measure["groups"].items()} for m, measure in json.loads(out["decision"])["measures"].items()}
        with_ledger = self.published()[0]
        without = dict(self.data, witness=None)
        self.assertEqual(calls(with_ledger), calls(self.published(without)[0]))

    def test_a_row_with_no_first_ready_gives_its_basis_as_the_reason(self):
        witness = evidence.witness_of(ledger_row(first_ready_at=None, first_ready_at_basis="timeline-incomplete", first_ready_head=None, first_ready_head_status="pending"), [])
        self.assertEqual(evidence.reason_for("first_ready", record(witness=witness)), "first_ready_timeline_incomplete")
        self.assertEqual(evidence.reason_for("verified_anchor", record(witness=witness)), "first_ready_head_pending")

    def test_with_no_ledger_draft_history_stays_a_gap(self):
        _, manifest, _ = self.published(dict(self.data, witness=None))
        sources = {s["id"]: s for s in manifest["sources"]}
        self.assertNotIn(evidence.LEDGER_SOURCE, sources)
        self.assertIn("draft history not collected", sources["github"]["gaps"])

    def test_a_ledger_of_another_schema_is_a_gap_and_stays_unchanged(self):
        root, _ = support.data_dir(self.data)
        path = os.path.join(root, best.ledger_store.FILE)
        db = sqlite3.connect(path)
        db.execute("PRAGMA user_version=9")
        db.close()
        before = open(path, "rb").read()
        out = best.collect(root, support.StandInOrch(self.data), clock=lambda: self.at)
        github = next(s for s in json.loads(out["manifest"])["sources"] if s["id"] == "github")
        self.assertIn("evidence ledger is another schema", github["gaps"])
        self.assertEqual(open(path, "rb").read(), before)

    def test_a_ledger_ahead_of_this_pass_is_not_read(self):
        self.at = evidence.instant(self.data["witness"]["passes"][-1]["finished_at"]) - 1
        self.data["repos"]["fetched"] = self.data["models"]["generated"] = int(self.at)
        out, manifest, _ = self.published()
        github = next(s for s in manifest["sources"] if s["id"] == "github")
        self.assertIn("evidence ledger clock is ahead of this pass", github["gaps"])


class OverLimit(unittest.TestCase):
    def setUp(self):
        self.data = support.capture()
        self.at = self.data["capture"]["made_at"]
        self.root, _ = support.data_dir(self.data)
        self.path = os.path.join(self.root, "cache", "best-ledger.json")

    def collect(self):
        return best.collect(self.root, support.StandInOrch(self.data), clock=lambda: self.at)

    def gaps(self, out, source="github"):
        return next(s["gaps"] for s in json.loads(out["manifest"])["sources"] if s["id"] == source)

    def held(self, path=None):
        path = path or self.path
        return sorted(name for name in os.listdir(os.path.dirname(path)) if name.startswith(os.path.basename(path) + ".held-"))

    def spoil(self, body):
        with open(self.path, "wb") as f:
            f.write(body)

    def test_a_ledger_past_its_byte_limit_keeps_its_bytes_and_a_gap_and_the_next_pass_runs(self):
        self.spoil(b"kept")
        with open(self.path, "r+b") as f:
            f.truncate(best.SOURCE_BYTES + 1)
        out = self.collect()
        self.assertEqual(len(self.held()), 1)
        with open(os.path.join(self.root, "cache", self.held()[0]), "rb") as f:
            self.assertEqual(f.read(4), b"kept")
        self.assertTrue(any(f"held aside, {best.SOURCE_BYTES + 1} bytes" in gap for gap in self.gaps(out)))
        self.assertTrue(evidence.ledger_shape(json.load(open(self.path))))
        self.collect()
        self.assertEqual(len(self.held()), 1)

    def test_an_unreadable_ledger_is_held_without_stopping_the_pass(self):
        self.spoil(b"{not json")
        self.assertTrue(any("held aside" in gap for gap in self.gaps(self.collect())))
        self.assertEqual(len(self.held()), 1)

    def test_a_ledger_of_the_wrong_shape_is_held_without_stopping_the_pass(self):
        self.spoil(b'{"a#1": {"head": {"last_seen": "soon"}}}')
        self.collect()
        self.assertEqual(len(self.held()), 1)

    def test_with_every_hold_slot_used_the_bytes_stay_and_the_pass_still_publishes(self):
        for n in range(best.HELD_FILES):
            open(f"{self.path}.held-{n}", "w").close()
        self.spoil(b"{not json")
        out = self.collect()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"{not json")
        self.assertTrue(any("saves none" in gap for gap in self.gaps(out)))

    def test_an_over_limit_review_cache_is_held_and_reread(self):
        path = os.path.join(self.root, "cache", "best-reviews.json")
        with open(path, "w") as f:
            f.write("[]")
        out = self.collect()
        self.assertEqual(len(self.held(path)), 1)
        self.assertTrue(any("held aside" in gap for gap in self.gaps(out, "orch")))

    def ledger(self):
        check = {"kind": "check_run", "name": "n", "id": 1, "status": "COMPLETED", "conclusion": "SUCCESS", "started": "s"}
        seen = lambda at: {"first_seen": at, "last_seen": at, "ci": "passed", "check_runs": {str(n): check for n in range(40)}, "complete": True}
        return {"old#1": {"a" * 40: seen(10)}, "new#2": {"b" * 40: seen(20)}}

    def test_compaction_drops_the_oldest_check_rows_first_and_marks_them(self):
        ledger = self.ledger()
        limit = len(evidence.canonical_text(ledger["new#2"])) + 200
        with unittest.mock.patch.object(evidence, "LEDGER_BYTES", limit):
            evidence.compacted(ledger)
        old, new = ledger["old#1"]["a" * 40], ledger["new#2"]["b" * 40]
        self.assertEqual((old["check_runs"], old["complete"], old["compacted"], old["first_seen"]), ({}, False, True, 10))
        self.assertEqual((len(new["check_runs"]), new["complete"], "compacted" in new), (40, True, False))
        self.assertEqual(evidence.compaction_gaps(ledger), ["1 sighted heads lost check rows to compaction"])

    def test_a_check_evicted_by_the_row_cap_marks_its_head_incomplete(self):
        check = lambda n: {"kind": "check_run", "name": f"check-{n:03}", "id": n, "status": "COMPLETED", "conclusion": "FAILURE" if n == 0 else "SUCCESS", "started": "s"}
        key = "example-org/example-backend#1"
        pass_with = lambda at, numbers: {"fetched": at, "prs": [{"repo": "backend", "number": 1, "head": "a" * 40, "ci": "passed", "check_runs": [check(n) for n in numbers], "check_runs_complete": True}]}
        ledger = evidence.sighted({}, pass_with(1, range(60)))
        self.assertTrue(evidence.ci_at(ledger, key, "a" * 40)["check_list_complete"])
        evidence.sighted(ledger, pass_with(2, range(1, 61)))
        seen = evidence.ci_at(ledger, key, "a" * 40)
        self.assertEqual((seen["checks"], seen["failed"], seen["check_list_complete"], seen["checks_compacted"]), (60, 0, False, True))
        self.assertEqual(evidence.compaction_gaps(ledger), ["1 sighted heads lost check rows to compaction"])
        self.assertIn("compacted away", evidence.ci_words({"ci": seen}))
        evidence.sighted(ledger, pass_with(3, range(1, 61)))
        self.assertFalse(evidence.ci_at(ledger, key, "a" * 40)["check_list_complete"])

    def test_a_compacted_head_never_reads_complete_again(self):
        ledger = {"example-org/example-web#1": {"a" * 40: {"first_seen": 1, "last_seen": 1, "ci": None, "check_runs": {}, "complete": False, "compacted": True}}}
        evidence.sighted(ledger, {"fetched": 2, "prs": [{"repo": "web", "number": 1, "head": "a" * 40, "ci": "passed", "check_runs": [], "check_runs_complete": True}]})
        self.assertFalse(ledger["example-org/example-web#1"]["a" * 40]["complete"])

    def test_a_ledger_under_its_limit_is_left_alone(self):
        ledger = self.ledger()
        self.assertEqual(evidence.compacted(json.loads(json.dumps(ledger))), ledger)


class Packaging(unittest.TestCase):
    def test_the_collector_runs_isolated_and_reports_why_it_stopped(self):
        answer = subprocess.run([sys.executable, "-I", str(ROOT / "collectors" / "best.py"), "/nonexistent"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(answer.stdout), {"schema": "flightops-best/1", "error": "GitHub collection is missing"})


class FailureDiagnostics(unittest.TestCase):
    def refused(self, failure):
        with unittest.mock.patch.object(best.resource, "setrlimit"), unittest.mock.patch.object(best, "collect", side_effect=failure):
            return best.run("unused", None)

    def test_the_known_independence_refusal_has_a_fixed_diagnostic_code(self):
        out = self.refused(best.ContractError("independence keys required"))
        self.assertEqual(out["failure_code"], "invalid_independence_keys")
        self.assertIn("invalid independence keys", out["error"])

    def test_unknown_contract_details_cannot_disclose_source_content(self):
        private = "synthetic-private-source-id"
        out = self.refused(best.ContractError("conflicting id: " + private))
        self.assertEqual(out["failure_code"], "comparison_contract_refused")
        self.assertNotIn(private, json.dumps(out))

    def test_storage_errors_cannot_disclose_paths(self):
        out = self.refused(PermissionError(13, "denied", "/synthetic-private/source.json"))
        self.assertEqual(out["failure_code"], "storage_unavailable")
        self.assertNotIn("synthetic-private", json.dumps(out))

    def test_malformed_input_cannot_disclose_source_values(self):
        out = self.refused(KeyError("synthetic-private-key"))
        self.assertEqual(out["failure_code"], "malformed_input")
        self.assertNotIn("synthetic-private", json.dumps(out))


class Boundaries(unittest.TestCase):
    SOURCE = (ROOT / "collectors" / "best.py").read_text() + (ROOT / "collectors" / "best_evidence.py").read_text()

    def test_no_write_verb_or_github_or_ssh(self):
        for word in ("POST", "PUT", "PATCH", "DELETE", "gh ", "ssh", "subprocess"):
            self.assertNotIn(word, self.SOURCE)

    def test_no_commit_trailer_reaches_a_decision(self):
        for word in ("trailer", "co-authored", "receipts", "pr-facts", "prfacts"):
            self.assertNotIn(word, self.SOURCE.lower())

    def test_no_fan_sensors(self):
        for word in ("rig_smm", "rig_ddv", "hwmon"):
            self.assertNotIn(word, self.SOURCE)

    def git_lines(self, *args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()

    def test_the_kernel_differs_from_the_frozen_base_only_by_the_pinned_content(self):
        tracked = self.git_lines("ls-files", "--", *KERNEL)
        added = self.git_lines("ls-files", "--others", "--exclude-standard", "--", *KERNEL)
        present = set(tracked + added)
        self.assertEqual(present, set(KERNEL_FROZEN) | set(KERNEL_PIN))
        for path, pin in {**KERNEL_FROZEN, **KERNEL_PIN}.items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), pin, path)

    def test_a_removed_kernel_file_fails_the_guard(self):
        expected = set(KERNEL_FROZEN) | set(KERNEL_PIN)
        for path in expected:
            with self.subTest(path=path), unittest.mock.patch.object(self, "git_lines", side_effect=[sorted(expected - {path}), []]):
                with self.assertRaises(AssertionError):
                    self.test_the_kernel_differs_from_the_frozen_base_only_by_the_pinned_content()

    def test_an_edit_inside_a_pinned_file_fails_the_guard(self):
        original = pathlib.Path.read_bytes
        with unittest.mock.patch.object(pathlib.Path, "read_bytes", lambda path: original(path) + b"\n"):
            with self.assertRaises(AssertionError):
                self.test_the_kernel_differs_from_the_frozen_base_only_by_the_pinned_content()

    def test_a_reported_diff_alone_cannot_satisfy_the_guard(self):
        reported = "\n".join(KERNEL_PIN) + "\n"
        original = pathlib.Path.read_bytes
        answer = unittest.mock.Mock(stdout=reported)
        with unittest.mock.patch.object(subprocess, "run", return_value=answer), unittest.mock.patch.object(pathlib.Path, "read_bytes", lambda path: original(path)[1:]):
            with self.assertRaises(AssertionError):
                self.test_the_kernel_differs_from_the_frozen_base_only_by_the_pinned_content()

    def test_an_unpinned_kernel_file_fails_the_guard(self):
        changed = "\n".join([*KERNEL_PIN, "collectors/model_analysis/never_pinned.py"]) + "\n"
        with unittest.mock.patch.object(self, "git_lines", side_effect=[changed.split(), []]):
            with self.assertRaises(AssertionError):
                self.test_the_kernel_differs_from_the_frozen_base_only_by_the_pinned_content()


if __name__ == "__main__":
    unittest.main()
