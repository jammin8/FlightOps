import copy
import unittest

from collectors.model_evidence.adapter import derive_observed
from collectors.model_evidence.native import timestamp
from test_model_evidence_native import shape, replay, projection, native_rows, PROJECT


class NativeSettingOrder(unittest.TestCase):
    def equal_time_shape(self, line):
        s = shape()
        start = next(r for r in s['correction']['records'] if r.get('type') == 'task_started')
        event = next(r for r in s['correction']['records'] if r['line'] == 605)
        event.update(line=line, timestamp=start['timestamp'])
        event['thread_settings']['service_tier'] = 'priority'
        return s

    def assert_usage_and_context(self, result, *, copies=1):
        self.assertEqual(len(result['requests']), 15)
        self.assertEqual(result['reconciliation'][0]['state'], 'match')
        self.assertTrue(all(v is True for v in result['reconciliation'][0]['matches'].values()))
        self.assertEqual(result['reconciliation'][0]['observed']['buckets'],
                         {'fresh_input':63247,'cache_read':4312576,'cache_write':0,'output':28370,'reasoning':13749})
        self.assertEqual(result['operations'], [])
        self.assertFalse(result['health']['live_qualified'])
        for q in result['requests']:
            self.assertEqual(q['reported']['model_id']['value'], 'gpt-6.1-sol')
            self.assertEqual(q['reported']['model_id']['basis'], 'native-turn-context')
            self.assertEqual(q['reported']['effort']['value'], 'xhigh')
            self.assertEqual(q['copies'], copies)
            self.assertFalse(q['conflict'])
            self.assertEqual(q['original_run_id'], result['runs'][0]['id'])
            self.assertEqual(q['original_native_turn_id'], result['runs'][0]['turns'][0]['native_turn_id'])

    def assert_tier(self, result, value):
        self.assertEqual({q['reported']['service_tier']['value'] for q in result['requests']}, {value})
        self.assertEqual(result['runs'][0]['turns'][0]['reported']['service_tier']['value'], value)
        if value is None:
            self.assertTrue(all(q['reported']['service_tier']['basis'] == 'unknown' and
                                q['reported']['service_tier']['evidence'] is None for q in result['requests']))

    def derive_copies(self, s, rows_a, rows_b, *, reverse=False):
        session = s['fixture']['attempts'][0]['nativeThreadId']
        at = timestamp(s['fixture']['observed_at'])
        snapshots = [{'records':rows_a,'digest':'a'*64,'gaps':[]}, {'records':rows_b,'digest':'b'*64,'gaps':[]}]
        if reverse:
            snapshots.reverse()
        return derive_observed(projection(s), {session:snapshots}, projects=[PROJECT],
                               window={'start':at-90*86400,'end':at}, as_of=at, acquired_at=at,
                               origin='synthetic-replay')

    def shifted_copy(self, rows, delta=1000):
        rows = copy.deepcopy(rows)
        for row in rows:
            row['line'] += delta
        return rows

    def test_same_timestamp_line605_before606_remains_known(self):
        result = replay(self.equal_time_shape(605))
        self.assert_tier(result, 'priority')
        self.assert_usage_and_context(result)

    def test_same_timestamp_unused_line608_after606_cannot_backfill(self):
        result = replay(self.equal_time_shape(608))
        self.assert_tier(result, None)
        self.assert_usage_and_context(result)

    def test_one_source_before_default_is_not_displaced_by_same_time_after_priority(self):
        s = self.equal_time_shape(605)
        before = next(r for r in s['correction']['records'] if r['line'] == 605)
        after = copy.deepcopy(before)
        before['thread_settings']['service_tier'] = 'default'
        after['line'] = 608
        s['correction']['records'].append(after)
        result = replay(s)
        self.assert_tier(result, 'default')
        self.assert_usage_and_context(result)

    def test_shared_start_copies_each_prove_local_precedence_despite_shifted_lines(self):
        s = self.equal_time_shape(605)
        rows = native_rows(s)
        result = self.derive_copies(s, rows, self.shifted_copy(rows))
        self.assert_tier(result, 'priority')
        self.assert_usage_and_context(result, copies=2)

    def test_same_setting_and_start_copies_disagree_on_precedence_withhold_tier(self):
        s = self.equal_time_shape(605)
        rows_a = native_rows(s)
        rows_b = self.shifted_copy(rows_a)
        next(r for r in rows_b if r['line'] == 1605)['line'] = 1608
        result = self.derive_copies(s, rows_a, rows_b)
        self.assert_tier(result, None)
        self.assert_usage_and_context(result, copies=2)

    def test_unknown_cross_source_order_does_not_compare_unrelated_line_numbers(self):
        s = self.equal_time_shape(605)
        rows = native_rows(s)
        a = [r for r in rows if r['line'] != 605]
        for unrelated_line in (5, 5000):
            b = copy.deepcopy([r for r in rows if r['line'] != 606])
            next(r for r in b if r['line'] == 605)['line'] = unrelated_line
            with self.subTest(unrelated_line=unrelated_line):
                result = self.derive_copies(s, a, b)
                self.assert_tier(result, None)
                self.assert_usage_and_context(result, copies=2)

    def test_ambiguous_copy_cannot_be_ignored_in_favor_of_a_preceding_copy(self):
        s = self.equal_time_shape(605)
        rows = native_rows(s)
        missing_start = self.shifted_copy([r for r in rows if r['line'] != 606])
        result = self.derive_copies(s, rows, missing_start)
        self.assert_tier(result, None)
        self.assert_usage_and_context(result, copies=2)

    def test_disputed_start_occurrence_is_not_collapsed_to_minimum_timestamp(self):
        s = self.equal_time_shape(605)
        a = native_rows(s)
        b = self.shifted_copy(a)
        next(r for r in b if r['line'] == 1606)['payload']['started_at'] += 1
        result = self.derive_copies(s, a, b)
        self.assert_tier(result, None)
        self.assert_usage_and_context(result, copies=2)

    def test_disputed_copy_order_is_independent_of_snapshot_iteration_order(self):
        s = self.equal_time_shape(605)
        a = native_rows(s)
        b = self.shifted_copy(a)
        next(r for r in b if r['line'] == 1605)['line'] = 1608
        for reverse in (False, True):
            result = self.derive_copies(s, a, b, reverse=reverse)
            self.assert_tier(result, None)
            self.assert_usage_and_context(result, copies=2)

    def test_genuine_correction_line605_strictly_earlier_than606_is_unchanged(self):
        result = replay(shape())
        self.assert_tier(result, 'default')
        self.assertEqual(result['requests'][0]['reported']['service_tier']['evidence']['line'], 605)
        self.assert_usage_and_context(result)

    def test_original_late755_is_observed_without_backfilling_the_prior_turn(self):
        from collectors.model_evidence.replay import replay_owned
        s = shape()
        result = replay_owned(s['fixture'], s['thread'], origin='synthetic-replay')
        self.assert_tier(result, None)
        self.assertTrue(any(o['evidence']['line'] == 755 and o['settings']['service_tier']['value'] == 'default'
                            for o in result['settings_observations']))
        self.assert_usage_and_context(result)

    def test_late_response_revision_keeps_original_owner_tier_and_five_usage_buckets(self):
        s = shape()
        original = next(r for r in s['fixture']['native']['records'] if r['type'] == 'token_usage_record')
        later = copy.deepcopy(original)
        later.update(line=760, timestamp='2026-10-29T22:59:00Z')
        s['fixture']['native']['records'].append(later)
        result = replay(s)
        self.assert_tier(result, 'default')
        self.assertEqual(len(result['requests']), 15)
        self.assertEqual(result['reconciliation'][0]['state'], 'match')
        q = next(q for q in result['requests'] if q['native_request_id'] == original['payload']['response_id'])
        self.assertEqual(q['copies'], 2)
        self.assertEqual(q['revision'], int(timestamp(later['timestamp'])*1000))
        self.assertEqual(q['original_native_turn_id'], original['payload']['turn_id'])
        self.assertEqual(q['original_run_id'], result['runs'][0]['id'])

    def equal_clock_context_snapshots(self):
        s = shape()
        start = next(r for r in s['correction']['records'] if r.get('type') == 'task_started')
        next(r for r in s['correction']['records'] if r['line'] == 605)['timestamp'] = start['timestamp']
        next(r for r in s['fixture']['native']['records'] if r['line'] == 607)['timestamp'] = start['timestamp']
        first = native_rows(s)
        second = self.shifted_copy(first)
        next(r for r in second if r['line'] == 1605)['line'] = 1608
        return s, first, second

    def assert_usage_ownership_revisions(self, result, baseline, *, copies=2):
        self.assertEqual(len(result['requests']), 15)
        self.assertEqual(result['reconciliation'][0]['state'], 'match')
        self.assertTrue(all(v is True for v in result['reconciliation'][0]['matches'].values()))
        self.assertEqual(result['reconciliation'][0]['observed']['buckets'],
                         {'fresh_input':63247,'cache_read':4312576,'cache_write':0,'output':28370,'reasoning':13749})
        before = {q['id']:q for q in baseline['requests']}
        self.assertEqual({q['id'] for q in result['requests']}, set(before))
        for q in result['requests']:
            original = before[q['id']]
            for key in ('original_run_id','original_native_turn_id','native_request_id','revision','usage'):
                self.assertEqual(q[key], original[key])
            self.assertEqual(q['copies'], original['copies']*copies)
            self.assertFalse(q['conflict'])
        self.assertEqual(result['operations'], [])
        self.assertFalse(result['health']['live_qualified'])

    def assert_independent_context(self, result, key, value):
        for q in result['requests']:
            fact = q['reported'][key]
            self.assertEqual(fact['value'], value)
            self.assertEqual(fact['basis'], 'native-turn-context')
            ev = fact['evidence']
            self.assertIn(ev['line'], (607,1607))
            self.assertEqual(ev['source_digest'], ('a' if ev['line']==607 else 'b')*64)
            self.assertEqual(ev['at'], timestamp('2026-10-29T22:28:34.594Z'))
            self.assertTrue(any(o['native_turn_id']==q['original_native_turn_id'] and o['settings'][key]==fact
                                for o in result['settings_observations']))
        self.assertEqual(result['runs'][0]['turns'][0]['reported'][key]['value'], value)

    def test_equal_clock_single_snapshot_keeps_agreeing_model_and_effort(self):
        s, first, _ = self.equal_clock_context_snapshots()
        result = replay(s)
        self.assertEqual({q['reported']['model_id']['value'] for q in result['requests']}, {'gpt-6.1-sol'})
        self.assertEqual({q['reported']['effort']['value'] for q in result['requests']}, {'xhigh'})
        self.assert_tier(result, 'default')
        self.assert_usage_ownership_revisions(result, replay(s), copies=1)

    def test_equal_clock_ambiguous_session_copies_keep_independent_context(self):
        s, first, second = self.equal_clock_context_snapshots()
        result = self.derive_copies(s, first, second)
        self.assert_independent_context(result, 'model_id', 'gpt-6.1-sol')
        self.assert_independent_context(result, 'effort', 'xhigh')
        self.assert_tier(result, None)
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_equal_clock_context_survives_reversed_snapshot_order(self):
        s, first, second = self.equal_clock_context_snapshots()
        result = self.derive_copies(s, first, second, reverse=True)
        self.assert_independent_context(result, 'model_id', 'gpt-6.1-sol')
        self.assert_independent_context(result, 'effort', 'xhigh')
        self.assert_tier(result, None)
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_equal_clock_conflicting_context_models_remain_unknown_per_dimension(self):
        s, first, second = self.equal_clock_context_snapshots()
        next(r for r in second if r['line']==1607)['payload']['model'] = 'other-model'
        result = self.derive_copies(s, first, second)
        self.assertEqual({q['reported']['model_id']['value'] for q in result['requests']}, {None})
        self.assert_independent_context(result, 'effort', 'xhigh')
        self.assertIn('native-settings-conflict', result['inventory']['gaps'])
        self.assert_tier(result, None)
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_equal_clock_conflicting_context_efforts_remain_unknown_per_dimension(self):
        s, first, second = self.equal_clock_context_snapshots()
        next(r for r in second if r['line']==1607)['payload']['effort'] = 'low'
        result = self.derive_copies(s, first, second)
        self.assertEqual({q['reported']['effort']['value'] for q in result['requests']}, {None})
        self.assert_independent_context(result, 'model_id', 'gpt-6.1-sol')
        self.assertIn('native-settings-conflict', result['inventory']['gaps'])
        self.assert_tier(result, None)
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_equal_clock_both_context_dimensions_conflict_without_erasing_usage(self):
        s, first, second = self.equal_clock_context_snapshots()
        next(r for r in second if r['line']==1607)['payload'].update(model='other-model', effort='low')
        result = self.derive_copies(s, first, second)
        for key in ('model_id','effort'):
            self.assertEqual({q['reported'][key]['value'] for q in result['requests']}, {None})
        self.assertIn('native-settings-conflict', result['inventory']['gaps'])
        self.assert_tier(result, None)
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_uncertain_contradicting_session_value_is_not_blanket_discarded(self):
        s, first, second = self.equal_clock_context_snapshots()
        for rows in (first,second):
            next(r for r in rows if r['payload'].get('type')=='thread_settings_applied' and r['timestamp']=='2026-10-29T22:28:34.594Z')['payload']['thread_settings']['model'] = 'other-model'
        result = self.derive_copies(s, first, second)
        self.assertEqual({q['reported']['model_id']['value'] for q in result['requests']}, {None})
        self.assert_independent_context(result, 'effort', 'xhigh')
        self.assertIn('native-settings-conflict', result['inventory']['gaps'])
        self.assert_tier(result, None)
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_definitely_applicable_session_and_context_disagreement_stays_unknown(self):
        s, _, _ = self.equal_clock_context_snapshots()
        next(r for r in s['fixture']['native']['records'] if r['line']==607)['payload']['model'] = 'other-model'
        result = replay(s)
        self.assertEqual({q['reported']['model_id']['value'] for q in result['requests']}, {None})
        self.assertEqual({q['reported']['effort']['value'] for q in result['requests']}, {'xhigh'})
        self.assertIn('native-settings-conflict', result['inventory']['gaps'])
        self.assert_usage_ownership_revisions(result, replay(s), copies=1)

    def test_absent_context_cannot_rescue_uncertain_values_from_requested_defaults(self):
        s, first, second = self.equal_clock_context_snapshots()
        first = [r for r in first if r['type']!='turn_context']
        second = [r for r in second if r['type']!='turn_context']
        result = self.derive_copies(s, first, second)
        for key in ('model_id','effort','service_tier'):
            self.assertEqual({q['reported'][key]['value'] for q in result['requests']}, {None})
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_older_context_does_not_bypass_a_later_session_uncertainty_barrier(self):
        s, first, second = self.equal_clock_context_snapshots()
        for rows in (first,second):
            next(r for r in rows if r['type']=='turn_context')['timestamp'] = '2026-10-29T22:28:34.593Z'
        result = self.derive_copies(s, first, second)
        for key in ('model_id','effort','service_tier'):
            self.assertEqual({q['reported'][key]['value'] for q in result['requests']}, {None})
        self.assert_usage_ownership_revisions(result, replay(s))

    def test_equal_clock_context_and_late_revision_keep_original_ownership(self):
        s, _, _ = self.equal_clock_context_snapshots()
        original = next(r for r in s['fixture']['native']['records'] if r['type']=='token_usage_record')
        late = copy.deepcopy(original)
        late.update(line=760, timestamp='2026-10-29T22:59:00Z')
        s['fixture']['native']['records'].append(late)
        first = native_rows(s)
        second = self.shifted_copy(first)
        next(r for r in second if r['line']==1605)['line'] = 1608
        result = self.derive_copies(s, first, second)
        self.assert_independent_context(result, 'model_id', 'gpt-6.1-sol')
        self.assert_independent_context(result, 'effort', 'xhigh')
        self.assert_tier(result, None)
        self.assert_usage_ownership_revisions(result, replay(s))
        q = next(q for q in result['requests'] if q['native_request_id']==original['payload']['response_id'])
        self.assertEqual(q['copies'], 4)
        self.assertEqual(q['revision'], int(timestamp(late['timestamp'])*1000))


if __name__ == '__main__':
    unittest.main()
