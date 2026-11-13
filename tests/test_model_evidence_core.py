import copy
import json
from pathlib import Path
import unittest

from collectors.model_evidence import ContractError, decode, encode, export_bundle
from collectors.model_evidence.ingest import accept_envelope
from collectors.model_evidence.usage import codex_turn_updates, normalize
from collectors.model_evidence.schema import JSON_SCHEMA

FIXTURES = Path(__file__).parent / 'fixtures' / 'model-evidence-v1'


def bundle(name='complete'):
    return json.loads((FIXTURES / (name + '.bundle.json')).read_text())


def evidence(at=900, line=1, source='fixture-native'):
    return {'source_id': source, 'at': at, 'line': line}


class Golden(unittest.TestCase):
    def test_complete_and_partial_goldens(self):
        for name in ('complete', 'partial'):
            with self.subTest(name=name):
                result = export_bundle(bundle(name))
                self.assertEqual(encode(result), (FIXTURES / (name+'.golden.json')).read_bytes())
                self.assertEqual(accept_envelope(result), result)
                self.assertFalse(result['health']['live_qualified'])

    def test_ordinary_fixture_usage_and_operation(self):
        out = export_bundle(bundle())
        self.assertEqual(out['requests'][0]['usage']['buckets'],
                         {'fresh_input':80,'cache_read':20,'cache_write':0,'output':30,'reasoning':5})
        self.assertEqual(out['operations'][0]['sha'], 'a'*40)
        self.assertEqual(out['health']['state'], 'complete')
        self.assertEqual(out['source_identity']['host'], None)

    def test_schema_document_matches_runtime_definition(self):
        self.assertEqual(json.loads(Path('collectors/model_evidence/agent-evidence.schema.json').read_text()), JSON_SCHEMA)

    def test_no_prompt_command_or_credential_text_is_published(self):
        b = bundle()
        b['prompt'] = 'SENSITIVE-PROMPT'
        b['runs'][0]['key'] = 'SENSITIVE-KEY'
        b['calls'][0]['command'] = 'git commit -m "SENSITIVE-MESSAGE"'
        b['completions'][0]['output'] = '[main aaaaaaa] SENSITIVE-MESSAGE\n'
        raw = encode(export_bundle(b))
        self.assertNotIn(b'SENSITIVE', raw)
        self.assertNotIn(b'command', raw)

    def test_schema_missing_and_old_fail(self):
        for old in (None,'agent-runs-export/1','pr-models/2'):
            b = bundle(); b['schema'] = old
            with self.assertRaises(ContractError): export_bundle(b)

    def test_live_origin_and_claimed_host_fail(self):
        b = bundle(); b['origin']['kind'] = 'live'
        with self.assertRaises(ContractError): export_bundle(b)
        e = export_bundle(bundle()); e['source_identity']['host'] = 'CI host'
        with self.assertRaises(ContractError): accept_envelope(e)

    def test_json_duplicates_nonfinite_oversize(self):
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b' '* (4*1024*1024+1)):
            with self.assertRaises(ContractError): decode(raw)

    def test_source_clock_bounds_not_mtime_or_generated(self):
        b = bundle(); b['sources'][0]['complete_through'] = None
        out = export_bundle(b)
        self.assertIn('coverage-unproven',out['health']['reasons'])
        b['sources'][0]['snapshot_at'] = 2000
        with self.assertRaises(ContractError): export_bundle(b)

    def test_invalid_neighbor_and_duplicate_run_invalidate_completeness(self):
        b = bundle(); b['runs'].append({'provider_namespace':'codex','native_run_id':None})
        out = export_bundle(b)
        self.assertEqual(len(out['runs']),1)
        self.assertEqual(out['health']['state'],'partial')
        b['runs'][-1] = copy.deepcopy(b['runs'][0])
        out = export_bundle(b)
        self.assertEqual(out['runs'],[])
        self.assertEqual(out['operations'],[])

    def test_invalid_native_id_and_unsupported_provider(self):
        for key, value in (('native_run_id','with spaces'),('provider_namespace','unverified-provider')):
            b=bundle(); b['runs'][0][key]=value
            self.assertEqual(export_bundle(b)['runs'],[])

    def test_requested_reported_distinct_and_switches_remain_per_turn(self):
        b = bundle(); r = b['runs'][0]
        turn = copy.deepcopy(r['turns'][0]); turn['native_turn_id']='turn-2'
        turn['reported']['model_id']['value']='different-actual'
        r['turns'].append(turn)
        out=export_bundle(b); t1,t2=out['runs'][0]['turns']
        values = {t['native_turn_id']:t for t in (t1,t2)}
        t = values['turn-r1']
        self.assertEqual(t['requested']['model_id']['value'],'requested-model')
        self.assertEqual(t['reported']['model_id']['value'],'reported-model')
        for field in ('provider','service_tier','effort','instance'):
            self.assertIsNone(t['reported'][field]['value'])
        self.assertEqual(values['turn-2']['reported']['model_id']['value'],'different-actual')

    def test_complete_main_does_not_complete_missing_child(self):
        out=export_bundle(bundle('partial'))
        self.assertIn('children-unreconciled',out['health']['reasons'])
        self.assertEqual(out['runs'][0]['turns'][0]['usage_status'],'complete')
        self.assertEqual(out['health']['state'],'partial')

    def test_current_metadata_shape_is_not_historical_settings_or_native_turns(self):
        metadata=json.loads((FIXTURES/'current-interface-shape.json').read_text())
        with self.assertRaises(ContractError): export_bundle(metadata)
        b=bundle(); b['current_metadata']=metadata
        b['runs'][0]['native_run_id']=metadata['thread']['recentRuns'][0]['runId']
        b['requests']=[]; b['calls']=[]; b['completions']=[]
        b['runs'][0]['turns'][0]['requested']={}; b['runs'][0]['turns'][0]['reported']={}
        out=export_bundle(b)
        self.assertTrue(out['runs'][0]['id'].startswith('codex:run:thread:mcp%3A'))
        for kind in ('requested','reported'):
            self.assertTrue(all(v['value'] is None for v in out['runs'][0]['turns'][0][kind].values()))
        self.assertNotIn(b'current-selection-model',encode(out))


    def test_shared_runs_cannot_be_exclusive_by_claim(self):
        b=bundle(); m=copy.deepcopy(b['runs'][0]['memberships'][0]); m['pr']=8
        b['runs'][0]['memberships'].append(m); b['runs'][0]['allocation']='exclusive'
        e=export_bundle(b)
        self.assertEqual(e['requests'][0]['allocation'],'shared')
        self.assertEqual(e['runs'][0]['allocation'],'shared')


class CanonicalLedger(unittest.TestCase):
    def with_source(self):
        b=bundle(); s=copy.deepcopy(b['sources'][0]); s['id']='copy'; s['digest']='2'*64
        b['sources'].append(s)
        return b

    def test_final_revision_replaces_missing_and_known_fields_as_a_whole(self):
        b=bundle(); final=copy.deepcopy(b['requests'][0]); final['revision']=2
        final['evidence']=evidence(910,2); final['usage'].pop('input_tokens')
        final['usage']['output_tokens']=9; final['usage']['reasoning_tokens']=1
        b['requests'].append(final)
        q=export_bundle(b)['requests'][0]
        self.assertEqual((q['copies'],q['revision'],q['usage']['output_total']),(2,2,9))
        self.assertIsNone(q['usage']['input_total'])
        self.assertFalse(q['conflict'])

    def test_equal_final_copies_refine_silence_without_using_obsolete_conflicts(self):
        b=self.with_source()
        old=copy.deepcopy(b['requests'][0]); old['usage']['output_tokens']=999
        b['requests']=[old]
        for source,line in (('fixture-native',2),('copy',5)):
            final=copy.deepcopy(old); final['revision']=2; final['evidence']=evidence(910,line,source)
            final['usage']['output_tokens']=9; final['usage']['reasoning_tokens']=1
            if source=='copy': final['usage'].pop('cache_creation_tokens')
            b['requests'].append(final)
        q=export_bundle(b)['requests'][0]
        self.assertEqual(q['usage']['output_total'],9)
        self.assertEqual(q['usage']['buckets']['cache_write'],0)
        self.assertFalse(q['conflict']); self.assertEqual(q['copies'],3)
        self.assertEqual(len(q['final_sources']),2)

    def test_equal_final_conflict_stays_unknown(self):
        b=self.with_source(); other=copy.deepcopy(b['requests'][0]); other['evidence']=evidence(source='copy')
        other['usage']['output_tokens']=31; b['requests'].append(other)
        out=export_bundle(b); q=out['requests'][0]
        self.assertTrue(q['conflict'])
        self.assertTrue(all(v is None for v in q['usage']['buckets'].values()))
        self.assertIn('request-conflict',out['health']['reasons'])

    def test_observation_order_does_not_change_final_or_owner(self):
        b=bundle(); later=copy.deepcopy(b['requests'][0]); later['revision']=2
        later['evidence']=evidence(910,4); b['requests'].append(later)
        a=export_bundle(b)['requests']; b['requests'].reverse()
        self.assertEqual(export_bundle(b)['requests'],a)
        self.assertEqual(a[0]['original_native_turn_id'],'turn-r1')

    def test_copied_request_with_conflicting_original_owner_cannot_allocate(self):
        b=bundle(); r=copy.deepcopy(b['runs'][0]); r['native_run_id']='r2'; r['native_session_id']='session-r2'
        r['turns'][0]['native_turn_id']='turn-r2'; b['runs'].append(r)
        q=copy.deepcopy(b['requests'][0]); q['original_native_run_id']='r2'; q['original_native_turn_id']='turn-r2'; b['requests'].append(q)
        out=export_bundle(b)['requests'][0]
        self.assertIsNone(out['original_run_id']); self.assertEqual(out['allocation'],'unknown')
        self.assertTrue(out['conflict'])

    def test_provider_scoped_same_native_request_is_not_a_global_collision(self):
        b=bundle('partial'); b['requests'][1]['native_request_id']='req-1'
        ids=[q['id'] for q in export_bundle(b)['requests']]
        self.assertEqual(ids,['claude:req-1','codex:req-1'])


class Usage(unittest.TestCase):
    def test_codex_optional_cache_write_missing_is_not_zero(self):
        raw={'input_tokens':100,'cached_input_tokens':20,'output_tokens':30,'reasoning_tokens':0}
        u=normalize('codex',raw)
        self.assertIsNone(u['buckets']['cache_write']); self.assertIsNone(u['buckets']['fresh_input'])
        self.assertEqual(u['buckets']['reasoning'],0); self.assertFalse(u['complete'])
        raw['cache_creation_tokens']=0
        self.assertEqual(normalize('codex',raw)['buckets']['fresh_input'],80)
        self.assertTrue(normalize('codex',raw)['complete'])

    def test_claude_split_input_and_reasoning_included_once(self):
        u=normalize('claude',{'input_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':5,
                              'output_tokens':9,'thinking_tokens':4})
        self.assertEqual((u['input_total'],u['output_total']),(35,9))
        self.assertEqual(u['buckets']['fresh_input'],10)

    def test_corrupt_counts_do_not_get_clamped_into_validity(self):
        for field,value in (('input_tokens',-1),('cached_input_tokens',101),('cache_creation_tokens',81),
                            ('output_tokens',True),('reasoning_tokens',31),('input_tokens',2**53)):
            raw=copy.deepcopy(bundle()['requests'][0]['usage']); raw[field]=value
            with self.subTest(field=field,value=value), self.assertRaises(ContractError): normalize('codex',raw)

    def test_turn_id_baseline_late_update_and_reset(self):
        def raw(i,o): return {'input_tokens':i,'cached_input_tokens':0,'output_tokens':o,'reasoning_tokens':0}
        events=[{'turn_id':'old','at':1,'latest':raw(10,2),'cumulative':raw(100,20)},
                {'turn_id':'new','at':2,'latest':raw(4,1),'cumulative':raw(104,21)},
                {'turn_id':'old','at':3,'latest':raw(12,3),'cumulative':raw(106,22)}]
        out=codex_turn_updates(events)
        self.assertEqual(out['new']['input_total'],4)
        self.assertEqual(out['old']['input_total'],12)
        events.append({'turn_id':'reset','at':4,'latest':raw(3,1),'cumulative':raw(3,1)})
        self.assertEqual(codex_turn_updates(events)['reset']['input_total'],3)
        self.assertIsNone(out['new']['buckets']['cache_write'])
        events.append({'turn_id':'new','at':5,'latest':raw(5,1),'cumulative':raw(111,23)})
        late=codex_turn_updates(events)['new']
        self.assertEqual(late['input_total'],5)
        self.assertEqual(late['derivation_scope'],'latest-response')
        self.assertFalse(late['turn_coverage_complete'])


if __name__ == '__main__': unittest.main()
