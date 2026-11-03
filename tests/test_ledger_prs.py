import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import ledger_activity
import ledger_github
import ledger_prs
import ledger_store
from ledger_fakes import FakeGitHub
from ledger_store import ZERO

NOW = ledger_store.epoch("2026-11-01T12:00:00Z")
REF = "refs/heads/b"


def sha(n):
    return f"{n:040x}"


def node(number=1, **changes):
    base = {"number": number, "state": "OPEN", "isDraft": False, "createdAt": "2026-10-01T10:00:00Z", "updatedAt": "2026-10-01T12:00:00Z", "mergedAt": None, "closedAt": None,
            "headRefName": "b", "headRefOid": sha(2), "isCrossRepository": False, "baseRefName": "main", "headRepository": {"nameWithOwner": "example-org/example-web"},
            "mergeCommit": None, "author": {"login": "jammin8"}, "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": []}}
    return dict(base, **changes)


def answer(nodes, more=False, cursor=None, repos=("web",)):
    return {"data": {"rateLimit": {"cost": 1, "remaining": 4000}, **{repo: {"pullRequests": {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes if repo == "web" else []}}
                                                                     for repo in ("backend", "web", "ios")}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ledger_store.Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.store.begin_pass(NOW)
        with self.store.tx():
            self.store.set_cursor("activity:web", {"covered_from": "2026-09-01T00:00:00Z", "covered_to": "2026-11-01T11:00:00Z", "feed_start": True, "walk": None})

    def event(self, n, at, before, after, ref=REF):
        with self.store.tx():
            self.store.db.execute("INSERT INTO activity_events VALUES ('web', ?, 'push', ?, ?, ?, ?, 'jammin8', 1, 1)", (n, ref, before, after, at))

    def inventory(self, *nodes, **kwargs):
        fake = FakeGitHub(graphql=lambda q: answer(list(nodes), **kwargs))
        client = ledger_github.Client(runner=fake)
        client.start()
        changed = ledger_prs.read_inventory(self.store, client, NOW)
        ledger_prs.resolve(self.store, changed, set(), NOW)
        return fake, client

    def witness(self, number=1):
        return self.store.db.execute("SELECT * FROM pr_witness WHERE repo = 'web' AND number = ?", (number,)).fetchone()

    def branch(self):
        self.event(1, "2026-10-01T09:00:00Z", ZERO, sha(1))
        self.event(2, "2026-10-01T11:00:00Z", sha(1), sha(2))


class Inventory(Base):
    def test_a_page_is_one_graphql_query_with_its_cost_counted(self):
        fake, client = self.inventory(node())
        self.assertEqual((len(fake.queries), client.graphql_points), (1, 1))

    def test_inventory_is_read_only(self):
        fake, _ = self.inventory(node())
        self.assertNotIn("mutation", fake.queries[0])

    def test_a_missing_repository_is_a_gap_not_an_empty_list(self):
        fake = FakeGitHub(graphql=lambda q: {"data": {"web": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}})
        client = ledger_github.Client(runner=fake)
        client.start()
        ledger_prs.read_inventory(self.store, client, NOW)
        kinds = {tuple(r) for r in self.store.db.execute("SELECT repo, kind FROM gaps WHERE closed_pass IS NULL")}
        self.assertEqual(kinds, {("backend", "read-failed"), ("ios", "read-failed")})

    def test_a_backfill_resumes_from_its_cursor_across_passes(self):
        pages = iter([answer([node(n)], more=True, cursor=f"c{n}") for n in range(1, ledger_prs.PAGES_PER_PASS + 1)])
        fake = FakeGitHub(graphql=lambda q: next(pages))
        client = ledger_github.Client(runner=fake)
        client.start()
        ledger_prs.read_inventory(self.store, client, NOW)
        self.assertEqual(ledger_prs.state_of(self.store, "web")["walk"]["after"], f"c{ledger_prs.PAGES_PER_PASS}")
        self.assertIn(f'after: "c{ledger_prs.PAGES_PER_PASS - 1}"', fake.queries[-1])

    def test_a_truncated_timeline_is_explicit(self):
        self.inventory(node(timelineItems={"pageInfo": {"hasNextPage": True}, "nodes": []}))
        self.assertEqual(self.store.db.execute("SELECT kind FROM gaps WHERE source = 'prs'").fetchone()[0], "timeline-truncated")


class Witness(Base):
    def test_head_at_open_comes_from_the_chain_with_provenance(self):
        self.branch()
        self.inventory(node())
        w = self.witness()
        self.assertEqual((w["head_at_open"], w["head_at_open_status"], w["head_at_open_basis"], w["head_at_open_event"]), (sha(1), "exact", "activity-chain", 1))

    def test_a_push_after_open_is_not_head_at_open(self):
        self.event(1, "2026-10-01T09:00:00Z", ZERO, sha(1))
        self.event(2, "2026-10-01T10:00:01Z", sha(1), sha(2))
        self.inventory(node())
        self.assertEqual(self.witness()["head_at_open"], sha(1))

    def test_first_ready_is_a_separate_field_from_head_at_open(self):
        self.event(1, "2026-10-01T09:00:00Z", ZERO, sha(1))
        self.event(2, "2026-10-01T11:00:00Z", sha(1), sha(2))
        self.inventory(node(isDraft=False, timelineItems={"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "ReadyForReviewEvent", "createdAt": "2026-10-01T11:30:00Z"}]}))
        w = self.witness()
        self.assertEqual((w["head_at_open"], w["first_ready_head"], w["first_ready_at"], w["first_ready_at_basis"]), (sha(1), sha(2), "2026-10-01T11:30:00Z", "ready-for-review-event"))

    def test_opened_ready_says_so(self):
        self.branch()
        self.inventory(node())
        w = self.witness()
        self.assertEqual((w["first_ready_at"], w["first_ready_at_basis"]), ("2026-10-01T10:00:00Z", "opened-ready"))

    def test_a_ready_pr_later_converted_opened_ready(self):
        self.branch()
        self.inventory(node(timelineItems={"pageInfo": {"hasNextPage": False}, "nodes": [{"__typename": "ConvertToDraftEvent", "createdAt": "2026-10-01T10:30:00Z"}]}))
        self.assertEqual(self.witness()["first_ready_at_basis"], "opened-ready-before-draft-event")

    def test_an_incomplete_timeline_is_not_proof_of_opening_ready(self):
        pr = {"created_at": "2026-01-01T00:00:00Z", "is_draft": 0, "state": "OPEN", "timeline_complete": 0}
        self.assertEqual(ledger_prs.first_ready(pr, []), (None, "timeline-incomplete"))

    def test_an_incomplete_timeline_stays_pending_with_a_gap(self):
        self.branch()
        self.inventory(node(timelineItems={"pageInfo": {"hasNextPage": True}, "nodes": [{"__typename": "ClosedEvent", "createdAt": "2026-10-01T10:30:00Z"}]}))
        w = self.witness()
        self.assertEqual((w["first_ready_at"], w["first_ready_head"], w["first_ready_head_status"], w["first_ready_at_basis"]), (None, None, "pending", "timeline-incomplete"))
        self.assertEqual(self.store.db.execute("SELECT kind FROM gaps WHERE source = 'prs' AND closed_pass IS NULL").fetchone()[0], "timeline-truncated")

    def test_a_ready_event_inside_a_truncated_timeline_still_counts(self):
        self.branch()
        self.inventory(node(timelineItems={"pageInfo": {"hasNextPage": True}, "nodes": [{"__typename": "ReadyForReviewEvent", "createdAt": "2026-10-01T11:30:00Z"}]}))
        self.assertEqual((self.witness()["first_ready_at"], self.witness()["first_ready_head_status"]), ("2026-10-01T11:30:00Z", "exact"))

    def test_a_timeline_read_whole_later_resolves_first_ready(self):
        self.branch()
        self.inventory(node(timelineItems={"pageInfo": {"hasNextPage": True}, "nodes": []}))
        self.inventory(node())
        self.assertEqual((self.witness()["first_ready_at_basis"], self.witness()["first_ready_head_status"]), ("opened-ready", "exact"))

    def test_a_draft_has_no_first_ready(self):
        self.branch()
        self.inventory(node(isDraft=True))
        w = self.witness()
        self.assertEqual((w["first_ready_at"], w["first_ready_head"], w["first_ready_head_status"], w["first_ready_at_basis"]), (None, None, "not-applicable", "still-draft"))

    def test_a_fork_with_the_same_branch_name_never_binds_to_local_pushes(self):
        self.branch()
        self.inventory(node(isCrossRepository=True, headRepository={"nameWithOwner": "someone/example-web"}))
        w = self.witness()
        self.assertEqual((w["binding"], w["head_at_open"], w["head_at_open_status"], w["final_head_check"]), ("head-repository-differs", None, "unknown", "not-checked"))

    def test_a_deleted_head_repository_is_unknown(self):
        self.branch()
        self.inventory(node(headRepository=None))
        self.assertEqual(self.witness()["binding"], "head-repository-unknown")

    def test_a_reused_branch_name_binds_to_the_incarnation_at_open(self):
        self.event(1, "2026-09-10T09:00:00Z", ZERO, sha(7))
        self.event(2, "2026-09-11T09:00:00Z", sha(7), ZERO)
        self.branch_again()
        self.inventory(node())
        w = self.witness()
        self.assertEqual((w["incarnation_seq"], w["head_at_open"], w["final_head_check"]), (2, sha(2), "in-incarnation"))

    def branch_again(self):
        self.event(3, "2026-10-01T09:00:00Z", ZERO, sha(1))
        self.event(4, "2026-10-01T09:30:00Z", sha(1), sha(2))

    def test_a_pr_opened_between_incarnations_is_unbound(self):
        self.event(1, "2026-09-10T09:00:00Z", ZERO, sha(7))
        self.event(2, "2026-09-11T09:00:00Z", sha(7), ZERO)
        self.inventory(node())
        self.assertEqual((self.witness()["binding"], self.witness()["head_at_open_status"]), ("no-incarnation-at-open", "unknown"))

    def test_a_final_head_outside_the_chain_is_a_failed_check_not_a_lost_head_at_open(self):
        self.branch()
        self.inventory(node(headRefOid=sha(9)))
        w = self.witness()
        self.assertEqual((w["final_head_check"], w["head_at_open_status"]), ("absent", "exact"))

    def test_a_final_head_match_does_not_make_a_gap_exact(self):
        self.event(1, "2026-10-01T09:00:00Z", ZERO, sha(1))
        self.event(2, "2026-10-01T11:00:00Z", sha(9), sha(2))
        self.inventory(node())
        w = self.witness()
        self.assertEqual((w["final_head_check"], w["head_at_open_status"], w["head_at_open"]), ("in-incarnation", "gap", None))

    def test_activity_not_yet_read_leaves_the_field_pending_then_resolves(self):
        self.branch()
        with self.store.tx():
            self.store.set_cursor("activity:web", ledger_activity.blank())
        self.inventory(node())
        self.assertEqual(self.witness()["head_at_open_status"], "pending")
        with self.store.tx():
            self.store.set_cursor("activity:web", {"covered_from": "2026-09-01T00:00:00Z", "covered_to": "2026-11-01T11:00:00Z", "feed_start": True, "walk": None})
        ledger_prs.resolve(self.store, set(), set(), NOW)
        self.assertEqual(self.witness()["head_at_open_status"], "exact")

    def test_a_head_queued_before_activity_gains_its_push_time(self):
        self.branch()
        with self.store.tx():
            self.store.set_cursor("activity:web", ledger_activity.blank())
        self.inventory(node())
        self.assertIsNone(self.store.db.execute("SELECT pushed_at FROM ci_heads WHERE sha = ?", (sha(2),)).fetchone()[0])
        with self.store.tx():
            self.store.set_cursor("activity:web", {"covered_from": "2026-09-01T00:00:00Z", "covered_to": "2026-11-01T11:00:00Z", "feed_start": True, "walk": None})
        ledger_prs.resolve(self.store, set(), set(), NOW)
        self.assertEqual(self.store.db.execute("SELECT pushed_at FROM ci_heads WHERE sha = ?", (sha(2),)).fetchone()[0], "2026-10-01T11:00:00Z")
        self.assertEqual(self.store.db.execute("SELECT pushed_at FROM pr_heads WHERE sha = ? AND role = 'observed-head'", (sha(2),)).fetchone()[0], "2026-10-01T11:00:00Z")

    def test_a_pr_older_than_the_feed_stays_unknown(self):
        with self.store.tx():
            self.store.set_cursor("activity:web", {"covered_from": "2026-10-15T00:00:00Z", "covered_to": "2026-11-01T11:00:00Z", "feed_start": True, "walk": None})
        self.inventory(node())
        self.assertEqual((self.witness()["head_at_open_status"], self.witness()["head_at_open_basis"]), ("unknown", "before-activity-feed"))

    def test_every_head_during_the_pr_is_queued_for_ci(self):
        self.event(1, "2026-10-01T09:00:00Z", ZERO, sha(1))
        self.event(2, "2026-10-01T11:00:00Z", sha(1), sha(2))
        self.event(3, "2026-10-01T13:00:00Z", sha(2), sha(3))
        self.inventory(node(headRefOid=sha(3)))
        heads = {r[0] for r in self.store.db.execute("SELECT sha FROM ci_heads")}
        self.assertEqual(heads, {sha(1), sha(2), sha(3)})

    def test_a_push_after_merge_is_not_a_pr_head(self):
        self.branch()
        self.event(3, "2026-10-02T13:00:00Z", sha(2), sha(3))
        self.inventory(node(state="MERGED", mergedAt="2026-10-01T12:00:00Z", closedAt="2026-10-01T12:00:00Z"))
        heads = {r[0] for r in self.store.db.execute("SELECT sha FROM ci_heads")}
        self.assertEqual(heads, {sha(1), sha(2)})


RULES = {"branchProtectionRules": {"pageInfo": {"hasNextPage": False}, "nodes": []},
         "rulesets": {"pageInfo": {"hasNextPage": False}, "nodes": [
             {"name": "main", "enforcement": "ACTIVE", "target": "BRANCH", "conditions": {"refName": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
              "rules": {"pageInfo": {"hasNextPage": False}, "nodes": [{"type": "REQUIRED_STATUS_CHECKS", "parameters": {"requiredStatusChecks": [{"context": "quality", "integrationId": 15368}]}}]}}]}}


class Policy(Base):
    def read(self, now=NOW, rules=RULES):
        fake = FakeGitHub(graphql=lambda q: {"data": {"web": rules, "backend": rules, "ios": rules}})
        client = ledger_github.Client(runner=fake)
        client.start()
        ledger_prs.read_policy(self.store, client, now)
        return fake

    def test_required_checks_stay_with_the_head(self):
        self.branch()
        self.inventory(node())
        self.read()
        ledger_prs.attach_policy(self.store)
        row = self.store.db.execute("SELECT required, base_ref, basis FROM head_policy WHERE sha = ?", (sha(2),)).fetchone()
        self.assertEqual((json.loads(row[0]), row[1], row[2]), ([[None, "quality", 15368]], "main", "read-after-head"))

    def test_policy_waits_until_the_head_time_can_be_known(self):
        self.branch()
        with self.store.tx():
            self.store.set_cursor("activity:web", ledger_activity.blank())
        self.inventory(node())
        self.read()
        self.assertEqual(ledger_prs.attach_policy(self.store), 0)

    def test_a_later_policy_never_rewrites_a_heads_policy(self):
        self.branch()
        self.inventory(node())
        self.read()
        ledger_prs.attach_policy(self.store)
        changed = json.loads(json.dumps(RULES).replace("quality", "other"))
        self.read(NOW + 90000, changed)
        ledger_prs.attach_policy(self.store)
        self.assertIn("quality", self.store.db.execute("SELECT required FROM head_policy WHERE sha = ?", (sha(2),)).fetchone()[0])

    def test_policy_is_read_at_most_daily(self):
        self.assertEqual(len(self.read().queries), 1)
        self.assertEqual(len(self.read(NOW + 3600).queries), 0)

    def test_no_policy_read_leaves_no_invented_policy(self):
        self.branch()
        self.inventory(node())
        self.assertEqual(ledger_prs.attach_policy(self.store), 0)


if __name__ == "__main__":
    unittest.main()
