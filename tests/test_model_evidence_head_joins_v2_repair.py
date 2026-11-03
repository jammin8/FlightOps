from copy import deepcopy
import unittest

from collectors.model_evidence.joins import derive as derive_v1
from collectors.model_evidence.joins_v2 import derive, from_v1, unique
from collectors.model_evidence.observed_contract import accept_observed
from test_model_evidence_head_joins import fixture as v1_fixture
from test_model_evidence_head_joins_v2 import fixture, events, kind, rechain, assignment, add_assignment


def clean_assignment(b, identity, **kwargs):
    rows = add_assignment(b, identity, **kwargs)
    rows[0]['payload'].update(revision=0, supersedes_assignment_id=None, requested_owner_binding_id=None)
    return rows


def requested_other_assignment():
    b = fixture()
    copied = deepcopy(kind(b, 'owner_bound'))
    copied.update(event_id='owner:SYNTHETIC-other', assignment_id='assignment:SYNTHETIC-other',
        owner_binding_id='owner:SYNTHETIC-other')
    events(b).append(copied)
    kind(b, 'assignment_intent')['payload']['requested_owner_binding_id'] = copied['event_id']
    return rechain(b)


class ReusedOpeningReceipt(unittest.TestCase):
    def case(self, change=lambda heads: None):
        b = v1_fixture()
        other = deepcopy(b['heads'][0])
        other.update(id='head:2', pr=2)
        b['heads'].append(other)
        change(b['heads'])
        v1 = derive_v1(b)
        b['agent_evidence'] = accept_observed(b['agent_evidence'])
        b['operations'] = []
        b['assignments'] = []
        return v1, derive(from_v1(b))

    def test_two_prs_claiming_one_opening_receipt_lose_first_try_eligibility(self):
        v1, v2 = self.case()
        self.assertEqual([c['verified_r0_ci_eligible'] for c in v2['ci']], [False, False])
        self.assertTrue(all('r0-receipt-conflict' in c['reasons'] for c in v2['ci']))
        self.assertEqual({(c['head_id'], c['verified_r0_ci_eligible'], tuple(c['reasons'])) for c in v1['ci']},
                         {(c['head_id'], c['verified_r0_ci_eligible'], tuple(c['reasons'])) for c in v2['ci']})

    def test_an_unrelated_receipt_keeps_its_head_eligible(self):
        def change(heads):
            heads[1]['r0_receipt'] = 'opened:2'
        _, v2 = self.case(change)
        self.assertEqual([c['verified_r0_ci_eligible'] for c in v2['ci']], [True, True])
        self.assertFalse(any('r0-receipt-conflict' in c['reasons'] for c in v2['ci']))

    def test_the_same_receipt_in_another_repository_is_unrelated(self):
        def change(heads):
            heads[1]['repository'] = 'other/repo'
        _, v2 = self.case(change)
        self.assertFalse(any('r0-receipt-conflict' in c['reasons'] for c in v2['ci']))

    def test_an_unverified_claim_does_not_conflict_with_a_verified_receipt(self):
        def change(heads):
            heads[1]['r0_status'] = 'unknown'
        v1, v2 = self.case(change)
        self.assertEqual([c['verified_r0_ci_eligible'] for c in v2['ci']], [c['verified_r0_ci_eligible'] for c in v1['ci']])
        self.assertFalse(any('r0-receipt-conflict' in c['reasons'] for c in v2['ci']))

    def test_a_superseded_revision_still_counts_as_a_claim(self):
        b = v1_fixture()
        other = deepcopy(b['heads'][0])
        other.update(id='head:2', pr=2)
        b['heads'].append(other)
        b['agent_evidence'] = accept_observed(b['agent_evidence'])
        b['operations'] = []
        b['assignments'] = []
        value = from_v1(b)
        later = deepcopy(next(h for h in value['heads'] if h['id'] == 'head:2'))
        later.update(revision=1, predecessor_revision=0, r0_receipt='opened:2')
        value['heads'].append(later)
        out = derive(value)
        self.assertFalse(next(c for c in out['ci'] if c['head_id'] == 'head:1')['verified_r0_ci_eligible'])


class V2SelectionRepair(unittest.TestCase):
    def retained(self, b, out):
        self.assertEqual(out['input']['boundary_snapshots'][0]['events'], unique(events(b)))
        self.assertEqual(out['input']['boundary_snapshots'][0]['as_of'], b['boundary_snapshots'][0]['as_of'])
        self.assertEqual(out['denominators']['selected_events'], len(unique(events(b))))
        self.assertEqual(out['authentication'], 'not-performed')
        self.assertFalse(out['merge_authority'])
        self.assertEqual(out['normalized_rows'], [])
        original = assignment(out)
        self.assertEqual(original['intents'], [kind(b, 'assignment_intent')['payload']])
        self.assertEqual(original['owners'], unique([e['payload'] for e in events(b)
            if e['kind'] == 'owner_bound' and e['assignment_id'] == original['id']]))
        self.assertEqual(original['resolutions'], unique([e['payload'] for e in events(b)
            if e['kind'] == 'assignment_resolution' and e['assignment_id'] == original['id']]))

    def test_v21_scope_mismatch_withholds_selected_owner(self):
        b = requested_other_assignment()
        out = derive(b)
        self.retained(b, out)
        a = assignment(out)
        self.assertIn('requested-owner-mismatch', a['reasons'])
        self.assertFalse(a['consistent'])
        self.assertIsNone(a['decision'])
        self.assertIsNone(a['owner'])

    def test_v21_scope_mismatch_withholds_native_knownness(self):
        out = derive(requested_other_assignment())
        self.assertFalse(assignment(out)['known']['native_owner'])

    def test_v21_scope_mismatch_withholds_mapping_denominator(self):
        out = derive(requested_other_assignment())
        self.assertEqual(out['denominators']['native_owner_mappings'], 0)

    def test_v21_matching_and_unrequested_owners_remain_selected(self):
        for requested in (False, True):
            with self.subTest(requested=requested):
                b = fixture()
                if requested:
                    kind(b, 'assignment_intent')['payload']['requested_owner_binding_id'] = kind(b, 'owner_bound')['event_id']
                out = derive(rechain(b))
                self.retained(b, out)
                a = assignment(out)
                self.assertTrue(a['consistent'])
                self.assertEqual(a['owner'], kind(b, 'owner_bound')['payload'])
                self.assertTrue(a['known']['native_owner'])
                self.assertEqual(out['denominators']['native_owner_mappings'], 1)

    def test_v21_missing_wrong_kind_payload_and_epoch_keep_raw_owners_unselected(self):
        for mode in ('missing', 'kind', 'payload', 'epoch'):
            with self.subTest(mode=mode):
                b = fixture()
                a, owner = kind(b, 'assignment_intent'), kind(b, 'owner_bound')
                if mode == 'missing':
                    a['payload']['requested_owner_binding_id'] = 'owner:SYNTHETIC-missing'
                elif mode == 'kind':
                    a['payload']['requested_owner_binding_id'] = kind(b, 'dispatch_observed')['event_id']
                elif mode == 'payload':
                    other = deepcopy(owner)
                    other.update(event_id='owner:SYNTHETIC-payload', owner_binding_id='owner:SYNTHETIC-payload')
                    other['payload']['actor_binding_id'] = 'actor:SYNTHETIC-conflict'
                    events(b).append(other)
                    a['payload']['requested_owner_binding_id'] = other['event_id']
                else:
                    owner['recorder_epoch'] = 'epoch:SYNTHETIC-other'
                out = derive(rechain(b))
                self.retained(b, out)
                self.assertIsNone(assignment(out)['owner'])
                self.assertFalse(assignment(out)['known']['native_owner'])
                self.assertEqual(out['denominators']['native_owner_mappings'], 0)

    def test_v21_absent_owner_keeps_independent_decision_and_unrelated_scope_survives(self):
        b = fixture()
        events(b)[:] = [e for e in events(b) if e['kind'] != 'owner_bound']
        out = derive(rechain(b))
        self.retained(b, out)
        self.assertTrue(assignment(out)['consistent'])
        self.assertIsNotNone(assignment(out)['decision'])
        self.assertIsNone(assignment(out)['owner'])
        b = requested_other_assignment()
        clean_assignment(b, 'assignment:SYNTHETIC-independent', at=200)
        out = derive(rechain(b))
        independent = assignment(out, 'assignment:SYNTHETIC-independent')
        self.assertTrue(independent['consistent'])
        self.assertIsNotNone(independent['decision'])
        self.assertEqual(independent['intents'][0]['requested_owner_binding_id'], None)
        self.retained(b, out)
        shuffled = deepcopy(b)
        events(shuffled).reverse()
        self.assertEqual(derive(shuffled), out)

    def test_v22_missing_revision_predecessor_withholds_consistency(self):
        for revision in (1, 7):
            with self.subTest(revision=revision):
                b = fixture()
                kind(b, 'assignment_intent')['payload']['revision'] = revision
                out = derive(rechain(b))
                self.retained(b, out)
                self.assertFalse(assignment(out)['consistent'])

    def test_v22_missing_revision_predecessor_withholds_selected_decision(self):
        for revision in (1, 7):
            with self.subTest(revision=revision):
                b = fixture()
                kind(b, 'assignment_intent')['payload']['revision'] = revision
                out = derive(rechain(b))
                self.retained(b, out)
                self.assertIsNone(assignment(out)['decision'])
                self.assertIn('intent-revision-predecessor-unresolved', assignment(out)['reasons'])
                self.assertIsNotNone(assignment(out)['owner'])

    def test_v22_invalid_review_revision_propagates_to_rework_but_not_independent_scope(self):
        b = fixture()
        kind(b, 'assignment_intent')['payload'].update(role='review', pr=1, reviewed_sha='a'*40, revision=7)
        kind(b, 'assignment_resolution')['payload'].update(reviewed_sha='a'*40, review_outcome='changes_requested')
        child = clean_assignment(b, 'assignment:SYNTHETIC-rework', role='rework', at=200)
        child[0]['payload'].update(trigger_kind='review', trigger_id='assignment:one')
        child[2]['payload']['trigger_id'] = 'assignment:one'
        clean_assignment(b, 'assignment:SYNTHETIC-independent', at=210)
        out = derive(rechain(b))
        self.retained(b, out)
        independent = assignment(out, 'assignment:SYNTHETIC-independent')
        self.assertTrue(independent['consistent'])
        self.assertIsNotNone(independent['decision'])
        for field in ('consistent', 'decision'):
            with self.subTest(field=field):
                dependent = assignment(out, 'assignment:SYNTHETIC-rework')
                if field == 'consistent':
                    self.assertFalse(dependent[field])
                else:
                    self.assertIsNone(dependent[field])
                self.assertIn('dependency-unqualified', dependent['reasons'])

    def test_v22_synthetic_two_event_revision1_unknown_pr_owner_retains_inventory_and_clocks(self):
        b = fixture()
        b.update(heads=[], sources=[])
        events(b)[:] = [e for e in events(b) if e['kind'] in ('assignment_intent', 'dispatch_observed')]
        a, d = kind(b, 'assignment_intent'), kind(b, 'dispatch_observed')
        a['payload'].update(revision=1, supersedes_assignment_id=None, pr=None, actor_binding_id=None,
            trigger_kind='initial', trigger_id=None)
        d['payload'].update(task_id_from_transport=None, orchestration_run_id='run:SYNTHETIC-returned', status='starting', delivery_state='started')
        rechain(b)
        b['generated_at'] = b['boundary_snapshots'][0]['as_of']
        out = derive(b)
        self.retained(b, out)
        a = assignment(out)
        self.assertEqual(out['denominators']['selected_events'], 2)
        self.assertEqual(out['denominators']['selected_assignments'], 1)
        self.assertEqual(out['denominators']['native_owner_mappings'], 0)
        self.assertEqual(a['intents'][0]['revision'], 1)
        self.assertIsNone(a['pr'])
        self.assertIsNone(a['owner'])
        self.assertFalse(a['consistent'])

    def test_v22_revision0_and_valid_linked_revision1_keep_lifecycles(self):
        b = fixture()
        kind(b, 'assignment_intent')['payload']['pr'] = 1
        valid = clean_assignment(b, 'assignment:SYNTHETIC-valid', at=200)
        valid[0]['payload'].update(revision=1, supersedes_assignment_id='assignment:one')
        out = derive(rechain(b))
        self.retained(b, out)
        self.assertTrue(assignment(out)['consistent'])
        self.assertIsNotNone(assignment(out)['decision'])
        self.assertIn('assignment-superseded', assignment(out)['reasons'])
        self.assertTrue(assignment(out, 'assignment:SYNTHETIC-valid')['consistent'])
        self.assertIsNotNone(assignment(out, 'assignment:SYNTHETIC-valid')['decision'])

    def test_v22_wrong_task_dangling_cycles_and_borrowing_stay_scoped(self):
        for mode in ('wrong-task', 'dangling', 'cycle'):
            with self.subTest(mode=mode):
                b = fixture()
                kind(b, 'assignment_intent')['payload']['pr'] = 1
                valid = clean_assignment(b, 'assignment:SYNTHETIC-valid', at=200)
                valid[0]['payload'].update(revision=1, supersedes_assignment_id='assignment:one')
                bad = clean_assignment(b, 'assignment:SYNTHETIC-bad', at=210)
                bad[0]['payload'].update(revision=1, supersedes_assignment_id='assignment:one')
                if mode == 'wrong-task':
                    bad[0]['payload']['task_id'] = 'work:SYNTHETIC-other'
                elif mode == 'dangling':
                    bad[0]['payload']['supersedes_assignment_id'] = 'assignment:SYNTHETIC-missing'
                else:
                    bad[0]['payload']['supersedes_assignment_id'] = 'assignment:SYNTHETIC-bad'
                out = derive(rechain(b))
                self.assertFalse(assignment(out, 'assignment:SYNTHETIC-bad')['consistent'])
                self.assertTrue(assignment(out, 'assignment:SYNTHETIC-valid')['consistent'])
                self.assertTrue(assignment(out)['consistent'])
                self.retained(b, out)


if __name__ == '__main__':
    unittest.main()
