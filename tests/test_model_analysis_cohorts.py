import json
from pathlib import Path
import unittest

from collectors.model_analysis import ContractError, build_manifest
from collectors.model_analysis.contract import digest
from test_model_analysis_support import AS_OF, packet, clone, missing

FIXTURES = Path(__file__).parent / 'fixtures' / 'model-analysis-kernel'


class Cohorts(unittest.TestCase):
    def test_frozen_membership_golden_independent_ids_and_counts(self):
        m = build_manifest(json.loads((FIXTURES/'membership-input.json').read_text()), replaying=True)
        expected = json.loads((FIXTURES/'membership-expected.json').read_text())
        self.assertEqual([r['pr_id'] for r in m['rows']], expected['cohort_ids'])
        self.assertEqual([r['pr_id'] for r in m['rows'] if r['description_28d']], expected['description_28d_ids'])
        self.assertEqual(m['populations']['metric'], expected['populations'])
        self.assertEqual(m['excluded'], expected['excluded'])

    def test_identical_opening_rerun_reopen_transport_copies_count_once(self):
        p = packet(3)
        p['observations'] += [clone(p['observations'][0]) for _ in range(3)]
        p['evidence'] += [clone(p['evidence'][0])]
        m = build_manifest(p)
        self.assertEqual(len(m['rows']), 6)
        self.assertEqual(len(m['populations']['metric']['a']['denominator_ids']), 3)

    def test_conflicting_pr_revisions_refuse_not_last_writer_wins(self):
        for field, value in [('revision', 'rerun-2'), ('first_ready_at', '2026-10-02T00:00:00Z')]:
            p = packet(2)
            other = clone(p['observations'][0]); other[field] = value
            p['observations'].append(other)
            with self.assertRaisesRegex(ContractError, 'conflicting pr_id'): build_manifest(p)

    def test_conflicting_sources_or_receipts_refuse(self):
        for section in ('sources', 'evidence'):
            p = packet(2); other = clone(p[section][0]); other['digest'] = 'd'*64
            p[section].append(other)
            with self.assertRaisesRegex(ContractError, 'conflicting id'): build_manifest(p)

    def test_unknown_settings_and_membership_remain_possible_denominators(self):
        p = packet(3); r = p['observations'][0]
        r['setup_id'] = None; r['first_ready_at'] = None
        m = build_manifest(p)
        for g in ('a', 'b'):
            self.assertEqual(m['populations']['metric'][g]['potential_denominator_ids'], ['org/repo#1'])
        self.assertEqual(m['rows'][0]['membership'], 'potential')

    def test_requested_setting_is_not_reported_and_does_not_fill_unknown(self):
        p = packet(3); p['setups'][0]['tier']['basis'] = 'requested'
        m = build_manifest(p)
        self.assertFalse(m['setups']['a']['exact'])
        self.assertEqual(len(m['populations']['metric']['b']['potential_denominator_ids']), 3)

    def test_unverified_old_membership_date_is_not_exclusion(self):
        p = packet(2); r = p['observations'][0]
        r['first_ready_at'] = '2020-01-01T00:00:00Z'; r['membership_evidence_ids'] = []
        m = build_manifest(p)
        self.assertEqual(len(m['rows']), 4)
        self.assertEqual(m['rows'][0]['membership'], 'potential')

    def test_anchor_size_preserved_and_current_rework_size_cannot_substitute(self):
        p = packet(2)
        p['observations'][0]['current_size'] = 100000
        m = build_manifest(p)
        self.assertEqual(m['rows'][0]['stratum'], ['org/repo', 'feature', 's'])
        p['observations'][0]['anchored_size']['r0_sha'] = 'f'*40
        self.assertIsNone(build_manifest(p)['rows'][0]['stratum'])

    def test_supported_push_anchor_not_verified(self):
        p = packet(2); p['observations'][0]['anchor']['kind'] = 'push_reconstruction'
        m = build_manifest(p)
        self.assertIn('anchored_size_unknown', m['rows'][0]['measures']['metric']['reasons'])

    def test_missing_shared_stacked_and_correlated_never_inflate_independence(self):
        p = packet(3)
        p['observations'][0]['shared'] = True
        p['observations'][1]['independence_keys'] += ['stack:one']
        p['observations'][3]['independence_keys'] += ['stack:one']
        missing(p['observations'][2])
        m = build_manifest(p)
        self.assertEqual(m['populations']['metric']['a']['included_ids'], [])
        self.assertEqual(len(m['populations']['metric']['a']['denominator_ids']), 3)
        self.assertEqual(m['populations']['metric']['b']['included_ids'], ['org/repo#5', 'org/repo#6'])

    def test_cross_window_reused_request_is_still_correlated(self):
        p = packet(2)
        p['observations'][2]['independence_keys'] = p['observations'][0]['independence_keys'][:]
        p['observations'][2]['first_ready_at'] = '2026-01-01T00:00:00Z'
        p['evidence'][12]['effective_at'] = '2026-01-01T00:00:00Z'
        m = build_manifest(p)
        self.assertEqual(len(m['excluded']), 1)
        self.assertIn('correlated_unit', m['rows'][0]['measures']['metric']['reasons'])

    def test_zero_missing_invalid_overflow_underflow_distinct(self):
        p = packet(3, kind='amount')
        values = [0, None, 'bad', 10**101, -1, 1e-110]
        for r, v in zip(p['observations'], values): r['measures']['metric']['value'] = v
        m = build_manifest(p)
        reasons = [r['measures']['metric']['reasons'] for r in m['rows']]
        for reason, actual in zip(['zero_amount','missing_value','invalid_value','overflow_value','negative_amount','underflow_amount'], reasons):
            self.assertIn(reason, actual)
        rate = packet(2)
        self.assertEqual(build_manifest(rate)['populations']['metric']['a']['included_ids'], ['org/repo#1','org/repo#2'])

    def test_unqualified_wrong_identity_schema_coverage_and_future_receipts(self):
        for mutate in (lambda p: p['evidence'][4].update(qualified=False),
                       lambda p: p['evidence'][4].update(pr_id='org/other#1'),
                       lambda p: p['evidence'][4].update(anchor_sha='f'*40),
                       lambda p: p['evidence'][4].update(effective_at='2026-10-30T00:00:00Z'),
                       lambda p: p['sources'][0].update(complete=False),
                       lambda p: p['sources'][0].update(gaps=['retention'])):
            p = packet(2); mutate(p)
            m = build_manifest(p)
            self.assertNotEqual(m['rows'][0]['measures']['metric']['status'], 'included')
        p = packet(2); p['evidence'][4]['schema'] = ''
        with self.assertRaises(ContractError): build_manifest(p)

    def test_late_evidence_is_new_manifest_old_frozen_object_unchanged(self):
        p = packet(2); missing(p['observations'][0])
        old = build_manifest(p); frozen_bytes = json.dumps(old, sort_keys=True)
        p['previous_manifest_hash'] = old['manifest_hash']; p['snapshot_id'] = 'late-receipt'
        p['observations'][0]['measures']['metric']['value'] = 1
        new = build_manifest(p)
        self.assertNotEqual(old['manifest_hash'], new['manifest_hash'])
        self.assertEqual(new['previous_manifest_hash'], old['manifest_hash'])
        self.assertEqual(frozen_bytes, json.dumps(old, sort_keys=True))

    def test_complete_empty_distinct_from_unavailable_inventory(self):
        p = packet(2); p['observations'] = []; p['evidence'] = []
        self.assertTrue(build_manifest(p)['inventory_complete'])
        p['inventory']['status'] = 'unavailable'
        self.assertFalse(build_manifest(p)['inventory_complete'])

    def test_full_canonical_repo_and_pr_id_required(self):
        p = packet(2); p['observations'][0]['repo'] = 'repo'
        with self.assertRaises(ContractError): build_manifest(p)

    def test_measure_specific_required_claims_cannot_be_weakened(self):
        for semantics in ('first_try_ci', 'usage', 'rework'):
            p = packet(2); p['measures'][0]['semantics'] = semantics
            with self.assertRaisesRegex(ContractError, 'insufficient receipt'): build_manifest(p)

    def test_each_measure_semantics_requires_its_own_bound_receipts(self):
        from collectors.model_analysis.contract import CLAIMS
        from test_model_analysis_support import rebind
        for semantics in ('first_try_ci', 'rework', 'usage'):
            p = packet(2, kind='amount' if semantics == 'usage' else 'rate')
            p['measures'][0]['semantics'] = semantics
            p['measures'][0]['required_claims'] = sorted(CLAIMS[semantics])
            for row in p['observations']:


                for claim in CLAIMS[semantics]:
                    template = next(e for e in p['evidence'] if e['pr_id'] == row['pr_id'] and
                                    e['claim'] == ('verified_anchor' if claim == 'verified_anchor' else 'measure_verified'))
                    ev = clone(template); ev['id'] += ':metric:'+claim; ev['claim'] = claim
                    ev['measure_id'] = 'metric'
                    fact = row['measures']['metric']
                    ev['value_hash'] = digest({'measure_id':'metric','value':fact['value'],'effective_at':fact['effective_at']})
                    p['evidence'].append(ev)
                    fact['evidence_ids'].append(ev['id'])
            m = build_manifest(p)
            self.assertEqual(len(m['populations']['metric']['a']['included_ids']), 2)

            claim = next(c for c in sorted(CLAIMS[semantics]) if c != 'exact_producer')
            for ev in p['evidence']:
                if ev['pr_id'] == 'org/repo#1' and ev['claim'] == claim: ev['qualified'] = False
            self.assertEqual(build_manifest(p)['rows'][0]['measures']['metric']['status'], 'missing')

    def test_manifest_hash_covers_sources_policy_and_values(self):
        m = build_manifest(packet(2))
        self.assertEqual(m['manifest_hash'], digest({k:v for k,v in m.items() if k != 'manifest_hash'}))
