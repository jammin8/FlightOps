from itertools import permutations
import math
import unittest
from unittest.mock import patch

from collectors.model_analysis import build_manifest, decide, ContractError
from collectors.model_analysis.contract import HISTORICAL_POLICY_VERSION, digest
from collectors.model_analysis import engine
from test_model_analysis_support import light_cell, packet, missing, clone, rebind, six_cells


def run(p):
    rebind(p)
    return decide(build_manifest(p))['measures']['metric']


def no_calls(result):
    return all(not g['calls'] for g in result['groups'].values())


class Decisions(unittest.TestCase):
    def test_large_seeded_rate_effect_calls_best_and_attention_with_real_calibration(self):
        r = run(packet(40, effect=True))
        self.assertEqual(r['groups']['a']['calls'], ['Best'])
        self.assertEqual(r['groups']['b']['calls'], ['NeedsAttention'])
        self.assertTrue(all(r['groups']['a']['gates'].values()))
        self.assertEqual(r['calibration']['status'], 'computed')
        self.assertEqual([x['simulations'] for x in r['calibration']['scenarios']], [120]*4)
        for s in r['calibration']['scenarios']:
            self.assertLessEqual(s['upper_95_simultaneous'], .1)
            self.assertEqual(s['false_flags'], len(s['failure_cases']))

    def test_seeded_nondegenerate_large_effect(self):
        import random
        p = packet(100)
        rng = random.Random(3813)
        for row in p['observations']:
            row['measures']['metric']['value'] = int(rng.random() < (.95 if row['setup_id'] == 'a' else .05))
        r = run(p)
        self.assertEqual(r['groups']['a']['calls'], ['Best'])
        self.assertEqual(r['groups']['b']['calls'], ['NeedsAttention'])
        self.assertTrue(r['calibration']['pass'])

    def test_positive_amount_effect_and_unbounded_missingness(self):
        p = packet(40, kind='amount', direction='lower', effect=True)
        r = run(p)
        self.assertEqual(r['groups']['a']['calls'], ['Best'])
        self.assertEqual(r['groups']['b']['calls'], ['NeedsAttention'])
        self.assertTrue(r['calibration']['pass'])
        for row in p['observations']:
            if row['number'] % 4 == 0: missing(row)
        r = run(p)
        self.assertTrue(no_calls(r))
        self.assertIsNone(r['groups']['a']['worst_case_interval'][1])
        self.assertFalse(r['coverage_threshold_pass'])

    def test_null_equal_and_boundary_rates_no_clear_difference(self):
        for value in (None, 0, 1):
            p = packet(40)
            if value is not None:
                for row in p['observations']: row['measures']['metric']['value'] = value
            r = run(p)
            self.assertTrue(no_calls(r))
            self.assertEqual(r['groups']['a']['status'], 'NoClearDifference')
            self.assertTrue(r['calibration']['pass'])

    def test_wrong_direction_changes_winner_and_attention_is_against_fixed_reference(self):
        p = packet(40, effect=True, direction='lower')
        r = run(p)
        self.assertEqual(r['groups']['b']['calls'], ['Best'])
        self.assertEqual(r['groups']['a']['calls'], [])
        self.assertEqual(r['reference_setup'], 'a')

    def test_twenty_is_not_sufficient_and_below_twenty_has_no_bootstrap(self):
        r = run(packet(19))
        self.assertFalse(r['gates']['independent_prs_at_least_20_each'])
        self.assertEqual(r['calibration']['status'], 'not_computed')
        self.assertNotIn('bootstrap', r)
        r = run(packet(20))
        self.assertTrue(r['gates']['independent_prs_at_least_20_each'])
        self.assertFalse(r['gates']['precision'])
        self.assertTrue(no_calls(r))

    def test_all_missing_and_immature_keep_denominators(self):
        p = packet(30)
        for row in p['observations']: missing(row)
        r = run(p)
        self.assertTrue(no_calls(r))
        self.assertEqual(r['coverage']['a']['known_denominator'], 30)
        self.assertEqual(r['coverage']['a']['minimum_fraction'], 0)
        self.assertEqual(len(r['populations']['a']['missing_ids']), 30)

    def test_severe_missingness_and_unknown_allocations_bound_against_winner(self):
        p = packet(60, effect=True)
        for row in p['observations']:
            if row['number'] % 3: missing(row)

        r = run(p)
        self.assertEqual(r['independent_counts'], {'a': 20, 'b': 20})
        self.assertTrue(no_calls(r))
        p = packet(40, effect=True)
        for row in p['observations'][:20]:
            row['setup_id'] = None; row['possible_setup_ids'] = []
        r = run(p)
        self.assertEqual(r['coverage']['b']['possible_extra'], 20)
        self.assertLess(r['coverage']['b']['minimum_fraction'], .9)
        self.assertTrue(no_calls(r))

    def test_directional_rate_bounds_can_pass_below_ninety_percent(self):
        p = packet(60, effect=True)
        for row in p['observations']:
            if row['number'] % 6 == 0: missing(row)
        r = run(p)
        self.assertFalse(r['coverage_threshold_pass'])
        self.assertEqual(r['groups']['a']['calls'], ['Best'])
        self.assertEqual(r['groups']['b']['calls'], ['NeedsAttention'])
        self.assertTrue(all(r['groups']['a']['gates'].values()))
        self.assertEqual(r['groups']['a']['coverage_route'], 'directional_worst_case')
        self.assertEqual(r['groups']['a']['unmet_gates'], [])

    def test_joint_support_not_marginal_counts_and_unknown_anchored_size(self):
        p = packet(40)
        p['policy']['reference_mix'] = [
            {'repo':'org/repo','task':task,'size':size,'weight':.25}
            for task in ('feature','bug') for size in ('s','m')]
        for i, row in enumerate(p['observations']):

            task = 'feature' if i % 2 == 0 else 'bug'
            small = (task == 'feature') == (row['setup_id'] == 'a')
            row['task'] = task
            row['anchored_size']['changed_lines'] = 50 if small else 300
        r = run(p)
        self.assertEqual(r['common_support_fraction'], 0)
        self.assertEqual(len(r['uncovered_strata']), 4)
        self.assertTrue(no_calls(r))
        p = packet(40, effect=True)
        for row in p['observations'][40:]: row['anchored_size'] = None
        r = run(p)
        self.assertFalse(r['gates']['common_support_at_least_70_percent'])

    def test_prs_outside_common_support_cannot_waive_its_missingness(self):
        p = packet(400, kind='amount', direction='lower', effect=True)
        for i, row in enumerate(p['observations']):
            if i % 400 >= 80: row['task'] = 'outside-reference'
            elif i % 400 >= 40: missing(row)
        rebind(p)
        manifest = build_manifest(clone(p))
        diluted = decide(manifest)['measures']['metric']
        p['observations'] = [r for r in p['observations'] if r['task'] != 'outside-reference']
        control = run(p)
        self.assertEqual(len(manifest['populations']['metric']['a']['included_ids']), 360)
        self.assertEqual(len(manifest['populations']['metric']['a']['denominator_ids']), 400)
        for g in ('a', 'b'):
            self.assertEqual(diluted['coverage'][g], {'included': 40, 'known_denominator': 80, 'possible_extra': 0,
                                                      'minimum_fraction': .5, 'threshold_pass': False})
            self.assertEqual(diluted['coverage'][g], control['coverage'][g])
            self.assertEqual(diluted['groups'][g]['calls'], [])
            self.assertEqual(diluted['groups'][g]['status'], 'InsufficientEvidence')
            self.assertEqual(diluted['groups'][g]['estimate'], control['groups'][g]['estimate'])
            self.assertEqual(diluted['groups'][g]['worst_case_interval'], control['groups'][g]['worst_case_interval'])
            self.assertIsNone(diluted['groups'][g]['worst_case_interval'][1])
            pop = diluted['populations'][g]
            outside = [e['pr_id'] for e in pop['excluded'] if e['reason'] == 'outside_common_support']
            self.assertEqual(len(outside), 320)
            self.assertFalse(set(outside) & set(pop['eligibility_ids']))
            self.assertEqual(len(pop['included_ids']), diluted['independent_counts'][g])
        self.assertFalse(diluted['coverage_threshold_pass'])

    def test_support_scoping_keeps_unknown_cells_and_possible_setups_against_every_group(self):
        p = packet(60, effect=True)
        a = p['observations'][:60]
        b = p['observations'][60:]
        for row in a[:10]: row['task'] = 'outside-reference'
        for row in a[10:12]: row['anchored_size'] = None
        for row in a[12:15]: row['setup_id'] = None; row['possible_setup_ids'] = []
        for row in a[15:17]: row['setup_id'] = None; row['possible_setup_ids'] = []; row['task'] = 'outside-reference'
        for row in b[:4]: missing(row)
        r = run(p)
        self.assertEqual(r['coverage']['a'], {'included': 43, 'known_denominator': 45, 'possible_extra': 3,
                                              'minimum_fraction': 43/48, 'threshold_pass': False})
        self.assertEqual(r['coverage']['b'], {'included': 56, 'known_denominator': 60, 'possible_extra': 3,
                                              'minimum_fraction': 56/63, 'threshold_pass': False})
        self.assertEqual(r['populations']['a']['verified_numerator'], 43)
        self.assertEqual(r['populations']['a']['reasons'], {'actual_setup_unknown': 3, 'anchored_size_unknown': 2})
        self.assertEqual(r['populations']['b']['reasons'], {'actual_setup_unknown': 3, 'missing_value': 4})
        self.assertEqual(r['calibration']['missing_shape'], {'a': {'org/repo/feature/s': 5}, 'b': {'org/repo/feature/s': 7}})

    def test_every_pr_on_common_support_keeps_the_manifest_population_unchanged(self):
        p = packet(40, effect=True)
        for row in p['observations'][:5]: missing(row)
        rebind(p)
        manifest = build_manifest(p)
        self.assertEqual(decide(manifest)['measures']['metric']['populations'], manifest['populations']['metric'])

    def test_a_measured_cell_without_support_cannot_raise_coverage_either(self):
        p = packet(60, effect=True)
        p['policy']['reference_mix'] = [{'repo':'org/repo','task':'feature','size':'s','weight':.8},
                                        {'repo':'org/repo','task':'bug','size':'s','weight':.2}]
        for i, row in enumerate(p['observations']):
            if i % 60 < 30: missing(row)
            elif i % 60 < 40: row['task'] = 'bug'
        for row in p['observations'][60:]:
            if row['task'] == 'bug': missing(row)
        r = run(p)
        self.assertEqual(r['common_support_fraction'], .8)
        self.assertEqual(r['coverage']['a']['included'], 20)
        self.assertEqual(r['coverage']['a']['known_denominator'], 50)
        self.assertEqual(sum(e['reason'] == 'outside_common_support' for e in r['populations']['a']['excluded']), 10)
        self.assertEqual(sum(e['reason'] == 'outside_common_support' for e in r['populations']['b']['excluded']), 10)

    def test_no_common_support_keeps_the_cohort_denominators(self):
        p = packet(40, effect=True)
        for row in p['observations'][40:]: row['task'] = 'outside-reference'
        rebind(p)
        manifest = build_manifest(p)
        r = decide(manifest)['measures']['metric']
        self.assertEqual(r['common_support_fraction'], 0)
        self.assertEqual(r['populations'], manifest['populations']['metric'])
        self.assertEqual(r['coverage']['b']['known_denominator'], 40)
        self.assertTrue(no_calls(r))

    def standardized(self, p, kind='rate'):
        rebind(p)
        manifest = build_manifest(p)
        values, weights, _, _, _ = engine._shape(manifest, 'metric')
        means = engine._summary(values, weights, kind == 'amount')[0]
        return {g: math.exp(v) if kind == 'amount' else v for g, v in means.items()}

    def test_a_light_cell_cannot_waive_missing_prs_in_a_heavy_cell_for_a_rate(self):
        p = light_cell(240, 24)
        observed = self.standardized(p)
        self.assertAlmostEqual(observed['a'], .85)
        self.assertAlmostEqual(observed['b'], .15)
        r = run(p)
        for g in ('a', 'b'):
            self.assertEqual(r['coverage'][g]['included'], 216)
            self.assertEqual(r['coverage'][g]['known_denominator'], 240)
            self.assertEqual(r['coverage'][g]['minimum_fraction'], .9)
            self.assertFalse(r['coverage'][g]['threshold_pass'])
            self.assertEqual(r['groups'][g]['calls'], [])
            self.assertEqual(r['groups'][g]['status'], 'InsufficientEvidence')
            self.assertEqual(r['groups'][g]['coverage_route'], 'directional_worst_case')
        self.assertFalse(r['coverage_threshold_pass'])
        self.assertTrue(r['calibration']['pass'])
        completed = clone(p)
        for row in completed['observations']:
            if row['measures']['metric']['value'] is None:
                row['measures']['metric']['value'] = 0 if row['setup_id'] == 'a' else 1
        reversed_rates = self.standardized(completed)
        self.assertAlmostEqual(reversed_rates['a'], .475)
        self.assertAlmostEqual(reversed_rates['b'], .525)

    def test_a_light_cell_cannot_waive_missing_prs_in_a_heavy_cell_for_an_amount(self):
        p = light_cell(200, 20, kind='amount', direction='lower')
        r = run(p)
        for g in ('a', 'b'):
            self.assertEqual(r['coverage'][g]['minimum_fraction'], .9)
            self.assertFalse(r['coverage'][g]['threshold_pass'])
            self.assertEqual(r['groups'][g]['calls'], [])
            self.assertIsNone(r['groups'][g]['worst_case_interval'][1])
        self.assertTrue(r['calibration']['pass'])
        completed = clone(p)
        for row in completed['observations']:
            if row['measures']['metric']['value'] is None:
                row['measures']['metric']['value'] = 100000 if row['setup_id'] == 'a' else 100
        reversed_means = self.standardized(completed, 'amount')
        self.assertGreater(reversed_means['a'], reversed_means['b'])

    def test_the_historical_policy_keeps_those_calls_under_its_own_identity(self):
        for p in (light_cell(240, 24, version=HISTORICAL_POLICY_VERSION),
                  light_cell(200, 20, kind='amount', direction='lower', version=HISTORICAL_POLICY_VERSION)):
            rebind(p)
            r = decide(build_manifest(p, replaying=True))['measures']['metric']
            self.assertEqual(r['groups']['a']['calls'], ['Best'])
            self.assertEqual(r['groups']['b']['calls'], ['NeedsAttention'])
            self.assertTrue(r['coverage_threshold_pass'])

    def two_cells(self, n=200, small=20):
        p = packet(n, effect=True)
        p['policy']['reference_mix'] = [{'repo':'org/repo','task':t,'size':'s','weight':.5} for t in ('small', 'large')]
        for i, row in enumerate(p['observations']):
            row['task'] = 'small' if i % n < small else 'large'
        return p

    def test_exactly_ninety_percent_in_every_cell_passes_and_earns_its_calls(self):
        p = self.two_cells()
        rows = p['observations']
        for offset in (0, 200):
            for row in rows[offset:offset+2] + rows[offset+20:offset+38]: missing(row)
        r = run(p)
        self.assertEqual(r['coverage']['a']['minimum_fraction'], .9)
        self.assertTrue(r['coverage_threshold_pass'])
        self.assertEqual(r['groups']['a']['coverage_route'], 'at_least_90_percent_each')
        self.assertEqual(r['groups']['a']['calls'], ['Best'])
        self.assertEqual(r['groups']['b']['calls'], ['NeedsAttention'])

    def test_one_cell_below_ninety_percent_fails_even_when_raw_coverage_passes(self):
        p = self.two_cells()
        for row in p['observations'][:3]: missing(row)
        r = run(p)
        self.assertEqual(r['coverage']['a']['minimum_fraction'], 197/200)
        self.assertFalse(r['coverage']['a']['threshold_pass'])
        self.assertTrue(r['coverage']['b']['threshold_pass'])
        self.assertFalse(r['coverage_threshold_pass'])

    def test_an_unknown_cell_counts_against_every_supported_cell(self):
        p = self.two_cells()
        for row in p['observations'][20:23]: row['anchored_size'] = None
        r = run(p)
        self.assertEqual(r['calibration']['missing_shape']['a'], {'org/repo/small/s': 3, 'org/repo/large/s': 3})
        self.assertEqual(r['coverage']['a']['minimum_fraction'], 197/200)
        self.assertFalse(r['coverage']['a']['threshold_pass'])

    def test_a_possible_setup_counts_against_every_group_it_might_join(self):
        p = self.two_cells()
        for row in p['observations'][:3]:
            row['setup_id'] = None
            row['possible_setup_ids'] = []
        r = run(p)
        self.assertEqual(r['coverage']['b']['possible_extra'], 3)
        self.assertEqual(r['coverage']['b']['included'], 200)
        self.assertEqual(r['coverage']['b']['minimum_fraction'], 200/203)
        self.assertFalse(r['coverage']['b']['threshold_pass'])

    def test_no_supported_cell_passes_no_threshold(self):
        p = packet(40, effect=True)
        for row in p['observations'][40:]: row['task'] = 'outside-reference'
        r = run(p)
        self.assertFalse(r['coverage']['a']['threshold_pass'])
        self.assertFalse(r['coverage_threshold_pass'])

    def test_partial_common_support_reports_uncovered_mix(self):
        p = packet(40, effect=True)
        p['policy']['reference_mix'] = [
            {'repo':'org/repo','task':'feature','size':'s','weight':.7},
            {'repo':'org/repo','task':'bug','size':'m','weight':.3}]
        r = run(p)
        self.assertAlmostEqual(r['common_support_fraction'], .7)
        self.assertEqual(r['uncovered_strata'], [['org/repo', 'bug', 'm']])
        self.assertEqual(r['groups']['a']['calls'], ['Best'])

    def test_direct_standardization_removes_imbalance_not_pooled_marginals(self):
        p = packet(100)
        p['policy']['reference_mix'] = [
            {'repo':'org/repo','task':'feature','size':'s','weight':.5},
            {'repo':'org/repo','task':'bug','size':'s','weight':.5}]
        for i, row in enumerate(p['observations']):
            high = (i % 100 >= (80 if i < 100 else 20))
            row['task'] = 'bug' if high else 'feature'
            row['measures']['metric']['value'] = int(high)
        r = run(p)
        self.assertTrue(no_calls(r))
        self.assertAlmostEqual(r['groups']['a']['estimate'], .5)
        self.assertAlmostEqual(r['groups']['b']['estimate'], .5)
        self.assertEqual(r['common_support_fraction'], 1)

    def assert_rate_fractions(self, r):
        self.assertLessEqual(r['common_support_fraction'], 1)
        for group in r['groups'].values():
            low, high = group['simultaneous_95_interval']
            self.assertLessEqual(0, low)
            self.assertLessEqual(low, group['estimate'])
            self.assertLessEqual(group['estimate'], high)
            self.assertLessEqual(high, 1)
            self.assertLessEqual(0, group['worst_case_interval'][0])
            self.assertLessEqual(group['worst_case_interval'][0], group['worst_case_interval'][1])
            self.assertLessEqual(group['worst_case_interval'][1], 1)

    def weighted(self, weights, n=80):
        p = packet(n, effect=True)
        p['policy']['reference_mix'] = [{'repo':'org/repo','task':str(i),'size':'s','weight':w} for i, w in enumerate(weights)]
        for i, row in enumerate(p['observations']): row['task'] = str(i % len(weights))
        return p

    def test_weighted_all_success_rate_stays_inside_the_rate_domain(self):
        r = run(self.weighted([0.266411259804338, 0.1419783183553549, 0.5022241992749249, 0.08938622256538212]))
        self.assertEqual(r['groups']['a']['estimate'], 1.0)
        self.assertEqual(r['groups']['b']['estimate'], 0.0)
        self.assertEqual(r['groups']['a']['calls'], ['Best'])
        self.assertEqual(r['groups']['b']['calls'], ['NeedsAttention'])
        for bound, simultaneous in zip(r['groups']['a']['worst_case_interval'], r['groups']['a']['simultaneous_95_interval']):
            self.assertAlmostEqual(bound, simultaneous, places=12)
        self.assert_rate_fractions(r)

    def test_support_mass_inside_the_frozen_sum_tolerance_stays_a_fraction(self):
        r = run(self.weighted([.25 + 2e-10, .25 + 2e-10, .25 + 2e-10, .25 + 2e-10]))
        self.assertEqual(r['common_support_fraction'], 1.0)
        self.assertTrue(r['gates']['common_support_at_least_70_percent'])
        self.assert_rate_fractions(r)

    def test_exactly_seventy_percent_support_passes_whatever_the_reference_order(self):
        rebind(original := six_cells())
        first = decide(build_manifest(original))
        self.assertTrue(first['measures']['metric']['gates']['common_support_at_least_70_percent'])
        self.assertEqual(first['measures']['metric']['groups']['a']['calls'], ['Best'])
        self.assertEqual(first['measures']['metric']['groups']['b']['calls'], ['NeedsAttention'])
        for order in ((5, 4, 3, 2, 1, 0), (2, 5, 0, 4, 1, 3)):
            rebind(reordered := six_cells(order=order))
            self.assertEqual(decide(build_manifest(reordered))['measures'], first['measures'], order)

    def test_every_reference_order_gives_one_support_shape(self):
        rebind(p := six_cells())
        manifest = build_manifest(p)
        mix = list(manifest['policy']['reference_mix'])
        shapes = set()
        for order in permutations(mix):
            manifest['policy']['reference_mix'] = list(order)
            _, weights, _, mass, uncovered = engine._shape(manifest, 'metric')
            shapes.add((tuple(weights.items()), mass, tuple(map(tuple, uncovered))))
        self.assertEqual(len(shapes), 1)
        self.assertGreaterEqual(shapes.pop()[1], engine.SUPPORT_FLOOR)

    def test_support_below_the_gate_stays_below_it(self):
        for weights in ([0.428, 0.052, 0.065, 0.074, 0.071, 0.31],
                        [0.438 - 1e-11, 0.052, 0.065, 0.074, 0.071, 0.3 + 1e-11]):
            r = run(six_cells(weights))
            self.assertFalse(r['gates']['common_support_at_least_70_percent'], weights)
            self.assertTrue(no_calls(r))

    def test_support_above_the_gate_passes(self):
        r = run(six_cells([0.448, 0.052, 0.065, 0.074, 0.071, 0.29]))
        self.assertTrue(r['gates']['common_support_at_least_70_percent'])
        self.assertEqual(r['groups']['a']['calls'], ['Best'])

    def test_amount_bounds_without_missing_prs_equal_the_simultaneous_interval(self):
        p = packet(80, kind='amount', direction='lower', effect=True)
        p['policy']['reference_mix'] = [{'repo':'org/repo','task':t,'size':'s','weight':w} for t, w in (('a', .1), ('b', .2), ('c', .7))]
        for i, row in enumerate(p['observations']): row['task'] = 'abc'[i % 3]
        r = run(p)
        for group in r['groups'].values():
            for bound, simultaneous in zip(group['worst_case_interval'], group['simultaneous_95_interval']):
                self.assertAlmostEqual(bound, simultaneous, places=12)

    def test_multiple_comparisons_best_must_separate_from_every_other(self):
        p = packet(40, groups=('a','b','c'), effect=True)

        for row in p['observations'][40:80]: row['measures']['metric']['value'] = 1
        r = run(p)
        self.assertNotIn('Best', r['groups']['a']['calls'])
        self.assertNotIn('Best', r['groups']['b']['calls'])
        self.assertEqual(r['groups']['c']['calls'], ['NeedsAttention'])
        self.assertEqual(len(r['comparisons']), 3)
        self.assertEqual(r['calibration']['family_size'], 3)

    def test_frozen_measure_family_also_adjusts_multiplicity(self):
        from collectors.model_analysis.contract import digest
        single = run(packet(40, effect=True))
        p = packet(40, effect=True)
        second = clone(p['measures'][0]); second['id'] = 'second'
        p['measures'].append(second)
        for row in p['observations']:
            fact = clone(row['measures']['metric'])
            ids = []
            for ident in fact['evidence_ids']:
                ev = clone(next(e for e in p['evidence'] if e['id'] == ident))
                ev['id'] += ':second'; ev['measure_id'] = 'second'
                ev['value_hash'] = digest({'measure_id':'second','value':fact['value'],'effective_at':fact['effective_at']})
                ids.append(ev['id']); p['evidence'].append(ev)
            fact['evidence_ids'] = ids
            row['measures']['second'] = fact
        results = decide(build_manifest(p))['measures']
        self.assertEqual(set(results), {'metric','second'})
        self.assertGreaterEqual(results['metric']['groups']['a']['simultaneous_95_interval'][1] -
                                results['metric']['groups']['a']['simultaneous_95_interval'][0],
                                single['groups']['a']['simultaneous_95_interval'][1] -
                                single['groups']['a']['simultaneous_95_interval'][0])
        self.assertTrue(results['second']['calibration']['pass'])

    def test_exhausted_computation_withholds_calibration_and_all_calls(self):
        with patch.object(engine, 'MAX_WORK', 10):
            r = run(packet(40, effect=True))
        self.assertTrue(no_calls(r))
        self.assertEqual(r['calibration']['reason'], 'resampling_work_budget_exhausted')
        with patch.object(engine, 'MAX_SECONDS', -1):
            r = run(packet(40, effect=True))
        self.assertTrue(no_calls(r))
        self.assertEqual(r['calibration']['reason'], 'wall_budget_exhausted')

    def test_actual_calibration_detects_false_flags_and_records_failures(self):


        def bad_null(values, weights, kind, scenario, rng):
            return {g:{c:[float(g == 'a')]*len(xs) for c,xs in cells.items() if c in weights}
                    for g,cells in values.items()}
        with patch.object(engine, '_null_values', bad_null):
            r = run(packet(40, effect=True))
        self.assertTrue(no_calls(r))
        self.assertFalse(r['calibration']['pass'])
        self.assertEqual(r['calibration']['scenarios'][0]['false_flags'], 120)
        self.assertEqual(r['calibration']['scenarios'][0]['failure_cases'][0]['simulation'], 0)

    def test_manifest_tampering_refuses_and_deterministic_decision_matches_frozen_hash(self):
        p = packet(40, effect=True)
        p['policy']['version'] = HISTORICAL_POLICY_VERSION
        m = build_manifest(p, replaying=True); d = decide(m)

        from pathlib import Path
        import json
        golden = json.loads((Path(__file__).parent/'fixtures/model-analysis-kernel/decision-expected.json').read_text())
        self.assertEqual(d['decision_hash'], golden['decision_hash'])
        self.assertEqual(d['manifest_hash'], golden['manifest_hash'])
        m['rows'][0]['measures']['metric']['value'] = 0
        with self.assertRaisesRegex(ContractError, 'manifest hash mismatch'): decide(m)
