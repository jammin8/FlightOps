from copy import deepcopy
import ast
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from collectors.model_evidence import primary_owner_contract as contract
from collectors.model_evidence import primary_owner_composition as core
from collectors.model_evidence import owner_lookup
from collectors.model_evidence.contract import ContractError,digest,encode

FIXTURE=Path(__file__).parent/'fixtures/model-evidence-primary-owner-synthetic.json'
GOLDEN=Path(__file__).parent/'fixtures/model-evidence-primary-owner-synthetic-output.json'


def fixture():return json.loads(FIXTURE.read_bytes())


def relookup(b,index):
    o=b['lookup_observations'][index]
    o=owner_lookup.derive_owner_lookup(o['selector'],o['rows'],o['source']['observations'])
    b['lookup_observations'][index]=o
    return o


def reseal(snapshot):
    events=snapshot['events'];previous=None
    for i,e in enumerate(events,1):e['sequence']=i;e['previous_event_digest']=previous;previous=digest(e)
    raw=b''.join(encode(e) for e in events)
    import hashlib
    sha=hashlib.sha256(raw).hexdigest();m=snapshot['manifest']
    m['segments']=[{'name':'seg-'+sha+'.jsonl','digest':sha,'previous_segment_digest':None,'first_sequence':1,'last_sequence':len(events),'event_count':len(events)}]
    m.update(last_sequence=len(events),last_event_digest=previous,last_source_at=max(e['recorded_at'] for e in events))
    snapshot['as_of']=m['last_source_at']


def refresh_calls(b):
    for c in b['caller_transcripts']:
        for field in ('request_ref','result_ref'):
            r=c[field]
            if r is None:continue
            if r['kind']=='event':
                rows=[e for s in b['primary_snapshots'] for e in s['events'] if e['event_id']==r['event_id'] and e['recorder_id']==r['recorder_id']]
                if len(rows)==1:c[field]=core.reference(rows[0],'event')
            else:
                rows=[o for o in b['lookup_observations'] if o['selector']['selection_id']==r['selection_id']]
                if len(rows)==1:c[field]=core.reference(rows[0],r['kind'])


class CompositionTests(unittest.TestCase):
    def setUp(self):self.b=fixture()
    def beta(self,out):return next(a for a in out['assignments'] if a['key']['assignment_id']=='SYNTHETIC-assignment-beta')
    def beta_edges(self,out):return [r for r in out['relations'] if r['key']['assignment']['assignment_id']=='SYNTHETIC-assignment-beta']
    def assert_unverified(self,out):
        self.assertEqual(out['authentication'],'not-performed');self.assertIs(out['merge_authority'],False)
        self.assertEqual(out['normalized_rows'],[]);self.assertEqual(out['denominators']['authenticated_rows'],0)
    def test_positive_golden_complete_contract_and_original_inventory(self):
        out=core.compose(self.b);self.assertEqual(out,json.loads(GOLDEN.read_bytes()));self.assertEqual(contract.accept_output(encode(out)),out)
        self.assertEqual(out['input'],self.b);self.assertEqual(out['denominators']['edges'],9)
        self.assertEqual(out['denominators']['matched_edges'],7);self.assertEqual(len(out['protocol_results']),3)
        self.assertTrue(all(p['state']=='consistent-supplied' for p in out['protocol_results']));self.assert_unverified(out)
    def test_revision_gap_keeps_independent_matched_relation(self):
        out=core.compose(self.b);a=next(a for a in out['assignments'] if a['key']['assignment_id'].endswith('alpha'))
        self.assertIn('revision-predecessor-unresolved',a['lifecycle_dispositions']);self.assertEqual(a['decision_state'],'unknown')
        self.assertTrue(any(r['relation_state']=='structurally-matched' for r in out['relations'] if r['key']['assignment']==a['key']))
    def test_all_attempts_turns_null_candidate_and_shared_unallocated(self):
        out=core.compose(self.b);self.assertEqual(out['denominators']['shared_run_groups'],1)
        self.assertEqual(out['denominators']['multi_owner_assignments'],2)
        self.assertEqual(sum(r['key']['provider_turn_id'] is None for r in out['relations']),2)
        self.assertTrue(all(r['attribution_state']=='unallocated' for r in out['relations']))
        self.assertIn('retention-unproven',out['gaps']);self.assertIn('child-inventory-unknown',out['gaps'])
    def test_exact_copies_increase_occurrences_without_changing_graph(self):
        before=core.compose(self.b)
        for field in ('primary_snapshots','lookup_observations','caller_transcripts'):self.b[field]+=deepcopy(self.b[field])
        out=core.compose(self.b)
        for field in ('assignments','relations','protocol_results','dispositions'):self.assertEqual(out[field],before[field])
        self.assertEqual(out['denominators']['event_occurrences'],12);self.assertEqual(out['denominators']['event_unique'],6)
        self.assertEqual(out['denominators']['lookup_occurrences'],6);self.assertEqual(out['input'],self.b)
    def test_outer_permutation_graph_and_raw_occurrence_order(self):
        before=core.compose(self.b)
        for field in ('primary_snapshots','lookup_observations','caller_transcripts'):self.b[field].reverse()
        out=core.compose(self.b)
        for field in ('assignments','relations','protocol_results','dispositions','denominators','gaps'):self.assertEqual(out[field],before[field])
        self.assertEqual(out['input'],self.b);self.assertNotEqual(out['input_digest'],before['input_digest'])
    def test_conflicting_lookup_copy_and_unrelated_survival(self):
        changed=deepcopy(self.b['lookup_observations'][0]);changed['rows']['run_attempts'][0]['payload']['nativeThreadId']='SYNTHETIC-wrong'
        changed=owner_lookup.derive_owner_lookup(changed['selector'],changed['rows'],changed['source']['observations']);self.b['lookup_observations'].append(changed)
        out=core.compose(self.b);self.assertEqual(self.beta_edges(out)[0]['relation_state'],'structurally-matched')
        self.assertTrue(all('lookup-selection-conflict' in r['reasons'] for r in out['relations'] if r['key']['selection_id'].endswith('alpha')))
        self.assertEqual(out['input']['lookup_observations'][-1],changed)
    def test_wrong_selector_scope_even_recomputed(self):
        for field,value in [('assignment_id','SYNTHETIC-other'),('recorder_id','SYNTHETIC-other'),('recorder_epoch','SYNTHETIC-other'),
                ('dispatch_event_id','SYNTHETIC-other'),('target_thread_id','SYNTHETIC-other'),('orchestration_run_id','SYNTHETIC-other'),('dispatch_response_observed_at',99994.6)]:
            with self.subTest(field=field):
                b=fixture();b['lookup_observations'][1]['selector'][field]=value;relookup(b,1);refresh_calls(b);out=core.compose(b)
                edges=[r for r in out['relations'] if r['key']['selection_id'].endswith('beta')]
                self.assertTrue(all(r['relation_state']!='structurally-matched' for r in edges));self.assertEqual(out['input'],b)
    def test_project_is_typed_lookup_relation_not_repository_name(self):
        self.b['lookup_observations'][1]['selector']['project_id']='15422c94-4afe-4fff-b056-f3b240bc63ce'
        relookup(self.b,1);refresh_calls(self.b);out=core.compose(self.b)
        self.assertTrue(all(r['relation_state']!='structurally-matched' for r in self.beta_edges(out)))
        self.assertIn('primary-project-unavailable',self.beta(out)['lifecycle_dispositions'])
    def test_missing_lookup_and_dangling_lookup_preserved(self):
        b=fixture();b['primary_snapshots']=[];out=core.compose(b)
        self.assertEqual(out['denominators']['lookup_occurrences'],3);self.assertEqual(out['denominators']['assignments'],3)
        self.assertTrue(all(r['relation_state']!='structurally-matched' for r in out['relations']))
        b=fixture();b['lookup_observations']=[];b['caller_transcripts']=[];out=core.compose(b)
        self.assertEqual(out['denominators']['assignments'],3);self.assertEqual(out['relations'],[])
        self.assertTrue(all('lookup-unavailable' in a['lifecycle_dispositions'] for a in out['assignments']))
    def test_requested_owner_wrong_kind_scope_payload_and_cross_epoch(self):
        for mode in ('missing','kind','scope','epoch','payload','matching'):
            with self.subTest(mode=mode):
                b=fixture();s=b['primary_snapshots'][1];a=s['events'][0];c=b['lookup_observations'][1]['candidates'][0]
                owner=deepcopy(s['events'][1]);owner.update(event_id='SYNTHETIC-owner',kind='owner_bound',recorded_at=99999,dependencies=[])
                owner['payload']={k:c[k] for k in ('task_id_from_transport','child_thread_id','orchestration_run_id','attempt_id','attempt_run_id','turn_attempt_id','turn_session_id','provider_namespace','native_session_id','native_turn_id','actor_binding_id')};owner['payload']['original_source_refs']=[]
                a['payload']['requested_owner_binding_id']=owner['event_id']
                if mode=='kind':a['payload']['requested_owner_binding_id']=s['events'][1]['event_id']
                elif mode=='scope':owner['assignment_id']='SYNTHETIC-other'
                elif mode=='epoch':owner['recorder_epoch']='SYNTHETIC-other'
                elif mode=='payload':owner['payload']['native_turn_id']='SYNTHETIC-wrong'
                if mode not in ('kind','missing'):s['events'].append(owner)
                reseal(s);refresh_calls(b);out=core.compose(b)
                matched=all(r['relation_state']=='structurally-matched' for r in self.beta_edges(out))
                self.assertEqual(matched,mode=='matching')
    def test_manifest_segments_chain_counts_digest_and_missing_history(self):
        for field,value in [('last_event_digest','f'*64),('last_sequence',9),('last_source_at',99996)]:
            b=fixture();b['primary_snapshots'][1]['manifest'][field]=value;out=core.compose(b)
            self.assertIn('snapshot-incomplete',self.beta(out)['lifecycle_dispositions']);self.assertTrue(self.beta_edges(out))
        for field,value in [('previous_segment_digest','f'*64),('event_count',3),('last_sequence',3),('digest','f'*64)]:
            b=fixture();segment=b['primary_snapshots'][1]['manifest']['segments'][0];segment[field]=value
            if field=='digest':segment['name']='seg-'+value+'.jsonl'
            out=core.compose(b);self.assertIn('snapshot-incomplete',self.beta(out)['lifecycle_dispositions'])
        b=fixture();b['primary_snapshots'][1]['manifest']=None;out=core.compose(b);self.assertIn('manifest-unavailable',out['gaps'])
    def test_event_fork_old_terminal_conflicts_are_not_latest_wins(self):
        b=fixture();s=deepcopy(b['primary_snapshots'][0]);s['events'][0]['payload']['task_id']='SYNTHETIC-conflict';reseal(s);b['primary_snapshots'].append(s)
        out=core.compose(b);self.assertEqual(self.beta_edges(out)[0]['relation_state'],'structurally-matched')
        self.assertTrue(all(r['relation_state']!='structurally-matched' for r in out['relations'] if r['key']['assignment']['assignment_id'].endswith('alpha')))
    def test_protocol_positive_root_rig_nonce_is_one_chain(self):
        result=core.validate_protocol(self.b);self.assertTrue(all(r['state']=='consistent-supplied' for r in result))
        self.assertEqual(self.b['caller_transcripts'][1]['request_nonce'],self.b['caller_transcripts'][2]['request_nonce'])
    def test_protocol_one_field_corruptions_are_scoped_refusals(self):
        for field,value in [('parent_invocation_id','SYNTHETIC-missing'),('producer_version','SYNTHETIC-version'),('host','rig'),
                ('route','guarded-orch'),('exit_code',1),('exit_code',None),('terminal','unknown'),('outcome','ambiguous'),
                ('failure_code','publication-ambiguous'),('result_nonce','SYNTHETIC-wrong'),('elapsed_ms',15001),('observed_at',99999)]:
            with self.subTest(field=field,value=value):
                b=fixture();b['caller_transcripts'][1][field]=value;out=core.compose(b)
                row=next(r for r in out['protocol_results'] if r['invocation_id']=='SYNTHETIC-call-root')
                self.assertEqual(row['state'],'refused');self.assertEqual(out['input'],b)
                self.assertEqual(self.beta_edges(out)[0]['relation_state'],'structurally-matched')
    def test_protocol_missing_and_swapped_results_refs_and_unknowns(self):
        for replacement in (None,core.reference(self.b['lookup_observations'][0],'lookup'),core.reference(self.b['primary_snapshots'][0]['events'][0],'event')):
            b=fixture();b['caller_transcripts'][1]['result_ref']=replacement
            self.assertTrue(any(p['state']=='refused' for p in core.validate_protocol(b)))
        b=fixture();b['caller_transcripts']=[];out=core.compose(b);self.assertEqual(out['protocol_results'],[])
        self.assertIn('caller-protocol-unavailable',self.beta(out)['lifecycle_dispositions'])
    def test_rig_parent_must_be_exact_root_chain_and_not_dispatch(self):
        for parent in ('SYNTHETIC-call-dispatch','SYNTHETIC-missing'):
            b=fixture();b['caller_transcripts'][2]['parent_invocation_id']=parent
            self.assertEqual(next(x for x in core.validate_protocol(b) if x['invocation_id']=='SYNTHETIC-call-rig')['state'],'refused')
    def test_source_binding_version_digest_and_changed_identity(self):
        for mode in ('version','digest','changed','missing','duplicate'):
            b=fixture();c=b['caller_transcripts'][1];binding=c['source_bindings'][0]
            if mode=='version':binding['version']='SYNTHETIC-other'
            elif mode=='digest':binding['digest']='f'*64
            elif mode=='changed':binding['after_identity']['inode']+=1
            elif mode=='missing':c['source_bindings']=[]
            else:c['source_bindings'].append(deepcopy(binding))
            row=next(x for x in core.validate_protocol(b) if x['invocation_id']=='SYNTHETIC-call-root');self.assertEqual(row['state'],'refused')
    def test_second_lookup_reused_nonce_and_invocation_conflicts(self):
        for mode in ('second','nonce','invocation'):
            b=fixture()
            if mode=='second':
                c=deepcopy(b['caller_transcripts'][1]);c['invocation_id']='SYNTHETIC-root-second';c['request_nonce']=c['result_nonce']='SYNTHETIC-second';b['caller_transcripts'].append(c)
            elif mode=='nonce':b['caller_transcripts'][0]['request_nonce']=b['caller_transcripts'][0]['result_nonce']=b['caller_transcripts'][1]['request_nonce']
            else:
                c=deepcopy(b['caller_transcripts'][1]);c['exit_code']=1;b['caller_transcripts'].append(c)
            self.assertTrue(any(x['state']=='refused' for x in core.validate_protocol(b)))
    def test_clocks_async_worker_and_unsynchronized_rig_preserved(self):
        b=fixture();o=b['lookup_observations'][1]
        for kind in ('runs','run_attempts','provider_turns'):
            for row in o['rows'][kind]:row['payload']['startedAt']=99996;row['payload']['completedAt']=99999
        relookup(b,1);refresh_calls(b);out=core.compose(b);self.assertEqual(self.beta_edges(out)[0]['relation_state'],'structurally-matched')
        b=fixture();o=b['lookup_observations'][1];o['source']['observations']['acquisition_started_at']=200000;o['source']['observations']['acquisition_completed_at']=200001;relookup(b,1);refresh_calls(b)
        b['caller_transcripts'][2].update(started_at=199999,observed_at=200002)
        self.assertTrue(all(p['state']=='consistent-supplied' for p in core.validate_protocol(b)))
        self.assertEqual(core.compose(b)['input'],b)
    def test_source_null_future_clocks_partial_asof_and_inventory(self):
        b=fixture();o=b['lookup_observations'][1];o['rows']['runs'][0]['payload']['startedAt']=None;o['rows']['provider_turns'][0]['payload']['completedAt']=100001
        relookup(b,1);refresh_calls(b);out=core.compose(b);self.assertIn('source-after-as-of',out['gaps']);self.assertEqual(out['input'],b)
        b=fixture();b['composed_at']=99990;out=core.compose(b);self.assertIn('primary-after-composed-at',out['gaps'])
        b=fixture();o=b['lookup_observations'][1];o['source']['observations']['row_limit_hit']=True;o['source']['observations']['table_states']['provider_turns']='limited';relookup(b,1)
        out=core.compose(b);self.assertEqual(out['denominators']['capped_lookup_occurrences'],1);self.assertEqual(out['denominators']['unread_lookup_tables'],1)
    def test_duplicate_rig_calls_and_child_failure_refuse_root_chain(self):
        for mode in ('duplicate','failed','wrong-reference'):
            b=fixture()
            if mode=='duplicate':
                c=deepcopy(b['caller_transcripts'][2]);c['invocation_id']='SYNTHETIC-rig-second';b['caller_transcripts'].append(c)
            elif mode=='failed':b['caller_transcripts'][2]['exit_code']=1
            else:b['caller_transcripts'][2]['result_ref']=core.reference(b['lookup_observations'][0],'lookup')
            result=core.validate_protocol(b)
            self.assertEqual(next(x for x in result if x['invocation_id']=='SYNTHETIC-call-root')['state'],'refused')
            self.assertEqual(next(x for x in result if x['invocation_id']=='SYNTHETIC-call-dispatch')['state'],'consistent-supplied')
    def test_wrong_reference_kinds_are_refusals_not_harness_errors(self):
        refs=[core.reference(self.b['primary_snapshots'][1]['events'][0],'event'),core.reference(self.b['lookup_observations'][1],'selector'),core.reference(self.b['lookup_observations'][1],'lookup')]
        for i in range(3):
            for field in ('request_ref','result_ref'):
                for ref in refs:
                    b=fixture();b['caller_transcripts'][i][field]=deepcopy(ref)
                    result=core.validate_protocol(b);self.assertEqual(len(result),3)
    def revision_fixture(self):
        b=fixture();s=b['primary_snapshots'][1];a,d=s['events'];prior='SYNTHETIC-predecessor'
        old=deepcopy(a);old.update(event_id='SYNTHETIC-prior-intent',assignment_id=prior,recorded_at=99980,dependencies=[])
        old_dispatch=deepcopy(d);old_dispatch.update(event_id='SYNTHETIC-prior-dispatch',assignment_id=prior,recorded_at=99981,dependencies=[old['event_id']])
        old_dispatch['payload'].update(client_request_id=prior,request_observed_at=99980.5,response_observed_at=99981)
        resolution=deepcopy(old_dispatch);resolution.update(event_id='SYNTHETIC-prior-resolution',kind='assignment_resolution',recorded_at=99990,dependencies=[])
        resolution['payload']={'revision':0,'terminal_run_id':d['payload']['orchestration_run_id'],'worker_terminal_state':'completed',
            'worker_started_at':99982,'worker_completed_at':99988,'result_repository':'local/flightops','result_pr':None,'result_sha':'b'*40,
            'reviewed_sha':None,'review_outcome':None,'resolution':'completed','trigger_id':None,'resolved_at':99989,'observed_at':99990,'result_source_refs':[]}
        a['payload'].update(revision=1,supersedes_assignment_id=prior)
        s['events']=[old,old_dispatch,resolution,a,d];reseal(s);refresh_calls(b)
        return b
    def test_initial_valid_successor_missing_predecessor_and_wrong_task_isolated(self):
        b=self.revision_fixture();out=core.compose(b);a=self.beta(out)
        self.assertNotIn('supersession-unresolved',a['lifecycle_dispositions']);self.assertNotIn('dependency-unresolved',a['lifecycle_dispositions'])
        self.assertEqual(next(a for a in out['assignments'] if a['key']['assignment_id']=='SYNTHETIC-predecessor')['decision_state'],'explicit-unverified')
        for mode in ('wrong-task','dangling','cycle'):
            c=self.revision_fixture();s=c['primary_snapshots'][1]
            if mode=='wrong-task':s['events'][0]['payload']['task_id']='SYNTHETIC-other-task'
            elif mode=='dangling':s['events'][3]['payload']['supersedes_assignment_id']='SYNTHETIC-missing'
            else:s['events'][0]['payload'].update(revision=2,supersedes_assignment_id=s['events'][3]['assignment_id'])
            reseal(s);refresh_calls(c);out=core.compose(c)
            self.assertTrue(set(self.beta(out)['lifecycle_dispositions'])&{'supersession-unresolved','dependency-unresolved'})
            self.assertTrue(any(r['relation_state']=='structurally-matched' for r in self.beta_edges(out)))
    def test_conflicting_old_terminal_taints_only_its_dependent_lifecycle(self):
        b=self.revision_fixture();s=b['primary_snapshots'][1];old=deepcopy(s['events'][2]);old['event_id']='SYNTHETIC-contradictory-terminal';old['payload']['result_sha']='c'*40;s['events'].insert(3,old);reseal(s);refresh_calls(b)
        out=core.compose(b);self.assertIn('dependency-unresolved',self.beta(out)['lifecycle_dispositions'])
        self.assertTrue(any(r['relation_state']=='structurally-matched' for r in self.beta_edges(out)))
        self.assertTrue(any(r['relation_state']=='structurally-matched' for r in out['relations'] if r['key']['assignment']['assignment_id'].endswith('gamma')))
    def test_rework_trigger_must_bind_exact_input_review_head_and_task(self):
        b=self.revision_fixture();s=b['primary_snapshots'][1];old=s['events'][0]['payload'];old.update(role='review',reviewed_sha='a'*40)
        s['events'][2]['payload'].update(reviewed_sha='a'*40,review_outcome='changes_requested');a=s['events'][3]['payload'];a.update(revision=0,supersedes_assignment_id=None,role='rework',trigger_kind='review',trigger_id=s['events'][0]['assignment_id']);reseal(s);refresh_calls(b)
        out=core.compose(b);self.assertNotIn('trigger-unresolved',self.beta(out)['lifecycle_dispositions'])
        for field,value in [('input_sha','f'*40),('task_id','SYNTHETIC-other'),('trigger_id','SYNTHETIC-missing')]:
            c=deepcopy(b);c['primary_snapshots'][1]['events'][3]['payload'][field]=value;reseal(c['primary_snapshots'][1]);refresh_calls(c)
            self.assertIn('trigger-unresolved',self.beta(core.compose(c))['lifecycle_dispositions'])
    def test_output_size_bound_refuses_without_dropping_input(self):
        b=fixture()
        with patch.object(core,'MAX_BYTES',1):
            with self.assertRaises(ContractError):core.compose(b)
        self.assertEqual(b,fixture())

    def test_empty_stream_and_null_snapshot_remain_in_inventory(self):
        b=fixture();s=deepcopy(b['primary_snapshots'][0]);s.update(events=[],as_of=None)
        s['manifest'].update(recorder_id='SYNTHETIC-empty',segments=[],last_sequence=0,last_event_digest=None,last_source_at=None)
        b['primary_snapshots'].append(s);out=core.compose(b)
        self.assertEqual(out['denominators']['streams'],4);self.assertEqual(out['denominators']['event_occurrences'],6)
        s=deepcopy(s);s['manifest']=None;s['gaps']=['manifest-unavailable'];b['primary_snapshots'].append(s)
        out=core.compose(b);self.assertEqual(out['denominators']['primary_occurrences'],5);self.assertIn('manifest-unavailable',out['gaps'])
    def test_dispatch_source_conflicting_copies_and_different_host_budgets(self):
        b=fixture();c=b['caller_transcripts'][0];binding=deepcopy(c['source_bindings'][0]);binding['digest']='f'*64;c['source_bindings'].append(binding)
        self.assertIn('source-binding-conflict',next(x for x in core.validate_protocol(b) if x['invocation_id']==c['invocation_id'])['reasons'])
        for i,ms in ((0,5001),(1,15001),(2,5001)):
            b=fixture();b['caller_transcripts'][i]['elapsed_ms']=ms
            self.assertIn('deadline-exceeded',next(x for x in core.validate_protocol(b) if x['invocation_id']==b['caller_transcripts'][i]['invocation_id'])['reasons'])
    def test_no_frozen_schema_global_mutation(self):
        from collectors.model_evidence import capture_contract,owner_lookup_contract
        before=deepcopy((capture_contract.COMMON,capture_contract.PAYLOADS,capture_contract.MANIFEST,owner_lookup_contract.OUTPUT))
        core.compose(self.b);contract.accept_output(core.compose(self.b))
        self.assertEqual(before,(capture_contract.COMMON,capture_contract.PAYLOADS,capture_contract.MANIFEST,owner_lookup_contract.OUTPUT))

    def test_strict_closed_fields_types_duplicate_keys_depth_bytes(self):
        for mutation in ({'composed_at':True},{'composed_at':10**1000},{'composed_at':float('nan')},{'qualified':True},{'role':'producer'}):
            b={**fixture(),**mutation}
            with self.assertRaises(ContractError):contract.accept_input(b)
        for raw in (b'{"schema":1,"schema":2}',b'NaN',b' '*1048577,b'['*33+b'0'+b']'*33):
            with self.assertRaises(ContractError):contract.accept_input(raw)
        b=fixture();b['caller_transcripts'][0]['exit_code']=False
        with self.assertRaises(ContractError):contract.accept_input(b)
        b=fixture();b['caller_transcripts'][0]['source_bindings'][0]['before_identity']['secret']='SYNTHETIC'
        with self.assertRaises(ContractError):contract.accept_input(b)
    def test_packet_count_and_event_row_bounds_reject_not_truncate(self):
        for field in ('primary_snapshots','lookup_observations','caller_transcripts'):
            b=fixture();b[field]=[deepcopy(b[field][0]) for _ in range(9)]
            with self.assertRaises(ContractError):core.compose(b)
        b=fixture();s=b['primary_snapshots'][0];s['events']*=17;b['primary_snapshots']=[deepcopy(s) for _ in range(3)]
        with self.assertRaises(ContractError):contract.accept_input(b)
        b=fixture();o=b['lookup_observations'][0];o['rows']['threads']*=10;o['source']['observations']['rows_seen']=len(o['rows']['threads'])+8
        o=owner_lookup.derive_owner_lookup(o['selector'],o['rows'],o['source']['observations']);b['lookup_observations']=[deepcopy(o) for _ in range(8)]
        with self.assertRaises(ContractError):contract.accept_input(b)
    def test_output_tamper_never_authenticates_or_promotes(self):
        out=core.compose(self.b)
        for field,value in [('authentication','primary-qualified'),('merge_authority',True),('normalized_rows',[None]),('input_digest','f'*64),('relations',[])]:
            altered=deepcopy(out);altered[field]=value
            with self.assertRaises(ContractError):contract.accept_output(altered)
        for c in self.b['caller_transcripts']:c['authentication']='qualified'
        with self.assertRaises(ContractError):core.compose(self.b)
    def test_fatal_cancellation_in_validation_sorting_and_dependencies(self):
        class Stop(BaseException):pass
        for operation in (contract.accept_input,core.validate_protocol,core.compose):
            for at in (1,10,100,1000):
                counter=[0]
                def check():
                    counter[0]+=1
                    if counter[0]==at:raise Stop()
                with self.assertRaises(Stop):operation(fixture(),check=check)
        def stop():raise Stop()
        with self.assertRaises(Stop):core.unique([{'id':'SYNTHETIC'}],stop)
        for phase in ('sort','dependency'):
            def phased():
                frame=sys._getframe(2)
                if phase=='sort' and frame.f_code.co_name=='order':raise Stop()
                if phase=='dependency' and frame.f_code.co_name=='assignment_facts' and 'cursor' in frame.f_locals:raise Stop()
            with self.assertRaises(Stop):core.compose(fixture(),check=phased)
        def ordinary_failure():raise ValueError('SYNTHETIC-callback')
        with self.assertRaises(contract.CompositionCancelled):core.compose(fixture(),check=ordinary_failure)
    def test_import_and_all_pure_entry_points_have_no_io_or_capability(self):
        sources=[Path(m.__file__).read_text() for m in (contract,core)];b=fixture()
        with (patch('builtins.open',side_effect=AssertionError('file')),patch('os.open',side_effect=AssertionError('file')),
                patch('sqlite3.connect',side_effect=AssertionError('sql')),patch('subprocess.Popen',side_effect=AssertionError('process')),
                patch('socket.socket',side_effect=AssertionError('network'))):
            for source in sources:exec(compile(source,'<SYNTHETIC-import>','exec'),{'__name__':'collectors.model_evidence.synthetic_poc','__package__':'collectors.model_evidence'})
            out=core.compose(b);core.validate_protocol(b);contract.accept_input(b);contract.accept_output(out)
        self.assertFalse(any('capability' in name or 'observe_' in name or 'runtime' in name for name in vars(core)))
        for source in sources:
            tree=ast.parse(source);self.assertFalse(any(ast.get_docstring(n) for n in ast.walk(tree) if isinstance(n,(ast.Module,ast.ClassDef,ast.FunctionDef))))


if __name__=='__main__':unittest.main()
