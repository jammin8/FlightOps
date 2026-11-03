from copy import deepcopy
import ast
import io
import json
from pathlib import Path
import random
import sys
import types
import unittest
from unittest.mock import patch

from collectors.model_evidence.contract import ContractError, digest, encode
from collectors.model_evidence.capture_contract import CaptureCancelled
from collectors.model_evidence.join_contract_v2 import accept_input, accept_output, HEAD, INPUT_JSON_SCHEMA, OUTPUT_JSON_SCHEMA
from collectors.model_evidence.joins_v2 import derive, empty_input, from_v1, from_selected_ci, normalize_pr_record
from collectors.model_evidence import join_contract as v1
from collectors.model_evidence import joins as old_joins
from collectors.model_evidence import joins_v2, join_contract_v2
from collectors.model_evidence.schema import validate

FIXTURES = Path(__file__).parent / 'fixtures'
V2 = FIXTURES / 'model-evidence-head-joins-v2'


def fixture():
    return json.loads((V2 / 'SYNTHETIC-input.json').read_bytes())


def events(b):
    return b['boundary_snapshots'][0]['events']


def rechain(b):
    rows = events(b)
    previous = None
    for index, row in enumerate(rows, 1):
        row['sequence'] = index
        row['previous_event_digest'] = previous
        previous = digest(row)
    b['boundary_snapshots'][0]['as_of'] = max((r['recorded_at'] for r in rows), default=None)
    return b


def kind(b, name):
    return next(e for e in events(b) if e['kind'] == name)


def assignment(out, identity='assignment:one'):
    return next(a for a in out['assignments'] if a['id'] == identity)


def add_assignment(b, identity, *, role='implementation', task='work:one', actor=None, at=200, input_sha='a'*40, result_sha='b'*40):
    originals = [deepcopy(kind(b, k)) for k in ('assignment_intent', 'dispatch_observed', 'assignment_resolution')]
    for index, row in enumerate(originals):
        row.update(event_id=identity + ':' + str(index), assignment_id=identity, recorded_at=at + index, dependencies=[])
    a, d, r = (e['payload'] for e in originals)
    a.update(task_id=task, role=role, pr=1, actor_binding_id=actor, input_sha=input_sha, memberships=[],
        reviewed_sha=input_sha if role=='review' else None, trigger_kind='initial', trigger_id=None)
    d.update(client_request_id=identity, orchestration_run_id=None, task_id_from_transport=None,
        request_observed_at=at, response_observed_at=at + 1)
    r.update(result_pr=1, result_sha=result_sha, reviewed_sha=input_sha if role=='review' else None,
        review_outcome='changes_requested' if role=='review' else None, terminal_run_id=None, worker_started_at=at,
        worker_completed_at=at+1, resolved_at=at+1, observed_at=at+2, trigger_id=None)
    events(b).extend(originals)
    return originals


class HeadAssignmentsV2(unittest.TestCase):
    def test_deterministic_golden_and_strict_output(self):
        out = derive(fixture())
        self.assertEqual(out, json.loads((V2 / 'SYNTHETIC-output.golden.json').read_bytes()))
        self.assertEqual(accept_output(out), out)
        self.assertEqual(out['normalized_rows'], [])
        self.assertFalse(out['merge_authority'])
        self.assertEqual(out['authentication'], 'not-performed')

    def test_unknown_replacement_v1_refusal_v2_preservation_and_independent_head(self):
        b = fixture()
        bad = deepcopy(b['heads'][0]);bad.pop('predecessor_revision')
        with self.assertRaises(ContractError):validate(bad, v1.HEAD)
        out = derive(b)
        unknown = next(h for h in out['heads'] if h['id'] == bad['id'])
        known = next(h for h in out['heads'] if h['id'] == 'head:independent')
        self.assertIsNone(unknown['known_current'])
        self.assertIn('replacement-state-unknown', unknown['reasons'])
        self.assertTrue(known['known_current'])
        self.assertIsNone(unknown['v1_row'])
        self.assertEqual(out['input']['heads'], joins_v2.unique(b['heads']))
        self.assertEqual(out['denominators']['selected_heads'], 2)

    def test_original_clocks_distinct_heads_do_not_force_worker_lifetime(self):
        b = fixture();out = derive(b);a = assignment(out)
        self.assertTrue(a['consistent'])
        self.assertIsNotNone(a['decision'])
        self.assertEqual((a['input_sha'], a['result_sha']), ('a'*40, 'b'*40))
        p = kind(b, 'assignment_resolution')['payload']
        self.assertLess(a['dispatches'][0]['request_observed_at'], p['worker_started_at'])
        self.assertGreater(a['decision']['observed_at'], p['worker_completed_at'])
        self.assertEqual(a['resolutions'], [p])
        self.assertIsNone(a['v1_row'])
        self.assertIn('v1-input-result-heads-differ-or-unknown', a['v1_dispositions'])
        old = json.loads((FIXTURES/'model-evidence-head-joins-v1/synthetic.json').read_bytes())
        old['assignments'][0].update(assigned_at=0)
        edge = next(e for e in old_joins.derive(old)['edges'] if e['receipt_id']==old['assignments'][0]['id'])
        self.assertIn('assignment-owner-clock-conflict', edge['reasons'])

    def test_nullable_pr_actor_nativeowner_and_process_are_independent_unknowns(self):
        b=fixture();events(b)[:] = [e for e in events(b) if e['kind']!='owner_bound'];out=derive(rechain(b));a=assignment(out)
        self.assertIsNone(a['pr']);self.assertIsNone(a['actor_id']);self.assertIsNone(a['owner'])
        self.assertIsNotNone(a['decision'])
        self.assertFalse(a['known']['native_owner'])
        self.assertFalse(out['operations'][0]['owned_process_known'])
        self.assertIn('owned-process-unavailable', out['operations'][0]['reasons'])

    def test_transport_completed_is_not_explicit_work_or_review_decision(self):
        b=fixture();events(b)[:] = [e for e in events(b) if e['kind'] not in ('assignment_resolution','owner_bound')]
        kind(b,'dispatch_observed')['payload']['status']='completed'
        a=assignment(derive(rechain(b)));self.assertIsNone(a['decision']);self.assertIsNone(a['owner'])

    def test_missing_intent_retains_dangling_assignment_nullable_repository(self):
        b=fixture();events(b)[:] = [e for e in events(b) if e['kind']=='dispatch_observed']
        a=assignment(derive(rechain(b)));self.assertIsNone(a['repository']);self.assertIsNone(a['input_sha'])
        self.assertFalse(a['consistent']);self.assertEqual(len(a['dispatches']),1)

    def test_valid_rework_binds_input_task_target_and_different_result(self):
        b=fixture();review=add_assignment(b,'assignment:review',role='review',actor='actor:review',at=200)
        work=add_assignment(b,'assignment:rework',role='rework',actor='actor:worker',at=210,result_sha='c'*40)
        work[0]['payload'].update(trigger_kind='review',trigger_id='assignment:review')
        work[2]['payload']['trigger_id']='assignment:review'
        out=derive(rechain(b));a=assignment(out,'assignment:rework')
        self.assertTrue(a['consistent']);self.assertEqual(a['input_sha'],'a'*40);self.assertEqual(a['result_sha'],'c'*40)
        self.assertEqual(assignment(out,'assignment:review')['reviewed_sha'],'a'*40)

    def test_wrong_rework_input_task_repository_pr_trigger_and_old_conflict_are_isolated(self):
        for change in ({'input_sha':'e'*40},{'task_id':'work:other'},{'repository':'other/project'},{'pr':2},{'trigger_id':'assignment:absent'}):
            b=fixture();add_assignment(b,'assignment:review',role='review',at=200)
            work=add_assignment(b,'assignment:rework',role='rework',at=210)
            work[0]['payload'].update({'trigger_kind':'review','trigger_id':'assignment:review',**change})
            work[2]['payload']['trigger_id']=work[0]['payload']['trigger_id']
            out=derive(rechain(b));self.assertFalse(assignment(out,'assignment:rework')['consistent']);self.assertTrue(assignment(out)['consistent'])
        b=fixture();review=add_assignment(b,'assignment:review',role='review',at=200)
        work=add_assignment(b,'assignment:rework',role='rework',at=210)
        work[0]['payload'].update(trigger_kind='review',trigger_id='assignment:review');work[2]['payload']['trigger_id']='assignment:review'
        contradiction=deepcopy(review[2]);contradiction.update(event_id='event:old-terminal',recorded_at=220)
        contradiction['payload'].update(resolution='failed',worker_terminal_state='failed',review_outcome=None)
        events(b).append(contradiction);out=derive(rechain(b));self.assertFalse(assignment(out,'assignment:rework')['consistent'])

    def test_successor_each_relationship_no_cross_task_borrowing(self):
        for valid in (False,True):
            b=fixture();kind(b,'assignment_intent')['payload']['pr']=1
            if valid:
                good=add_assignment(b,'assignment:valid',at=200)
                good[0]['payload'].update(supersedes_assignment_id='assignment:one',revision=1)
            bad=add_assignment(b,'assignment:wrong',task='work:other',at=210)
            bad[0]['payload'].update(supersedes_assignment_id='assignment:one',revision=1)
            out=derive(rechain(b));self.assertFalse(assignment(out,'assignment:wrong')['consistent'])
            if valid:
                self.assertTrue(assignment(out,'assignment:valid')['consistent'])
                self.assertIn('assignment-superseded',assignment(out)['reasons'])

    def test_cycles_dangling_dependencies_and_conflicting_event_copies(self):
        for mode in ('cycle','dangling','conflict'):
            b=fixture();a=kind(b,'assignment_intent')
            if mode=='cycle':a['payload']['supersedes_assignment_id']='assignment:one'
            elif mode=='dangling':a['dependencies']=['event:absent']
            else:
                other=deepcopy(a);other['payload']['task_id']='work:conflict';events(b).append(other)
            out=derive(rechain(b));self.assertFalse(assignment(out)['consistent']);self.assertEqual(out['denominators']['selected_assignments'],1)

    def test_resolution_revision_explicit_predecessor_and_old_terminal_contradiction(self):
        b=fixture();original=kind(b,'assignment_resolution');new=deepcopy(original)
        original['payload'].update(resolution='open',resolved_at=None,worker_terminal_state='in_progress')
        new.update(event_id='event:revision',recorded_at=200,dependencies=[original['event_id']]);new['payload']['revision']=1
        events(b).append(new);self.assertTrue(assignment(derive(rechain(b)))['consistent'])
        new['dependencies']=[];self.assertFalse(assignment(derive(rechain(b)))['consistent'])
        new['dependencies']=[original['event_id']];original['payload'].update(resolution='failed',worker_terminal_state='failed',resolved_at=50)
        self.assertFalse(assignment(derive(rechain(b)))['consistent'])

    def test_owner_target_actor_and_review_decision_conflicts(self):
        for field,value in (('native_session_id','session:wrong'),('attempt_run_id','run:wrong'),('child_thread_id','thread:wrong')):
            b=fixture();kind(b,'owner_bound')['payload'][field]=value;self.assertFalse(assignment(derive(rechain(b)))['consistent'])
        b=fixture();review=add_assignment(b,'assignment:review',role='review',at=200)
        review[2]['payload']['review_outcome']=None
        self.assertIsNone(assignment(derive(rechain(b)),'assignment:review')['decision'])

    def test_same_actor_review_exclusion_without_inferred_producer(self):
        b=fixture();kind(b,'assignment_intent')['payload'].update(pr=1,actor_binding_id='actor:same')
        review=add_assignment(b,'assignment:review',role='review',actor='actor:same',at=200,input_sha='b'*40)
        out=derive(rechain(b));self.assertIn('same-actor-review',assignment(out,'assignment:review')['reasons'])
        review[0]['payload']['actor_binding_id']='actor:independent'
        self.assertTrue(assignment(derive(rechain(b)),'assignment:review')['consistent'])

    def test_head_revisions_duplicates_conflicts_and_permutations(self):
        b=fixture();out=derive(b)
        duplicate=deepcopy(b)
        for key in ('heads','ci_attempts','policies','boundary_snapshots'):duplicate[key]+=deepcopy(duplicate[key])
        self.assertEqual(derive(duplicate),out)
        for _ in range(3):
            random.Random(_).shuffle(b['heads']);random.Random(_).shuffle(events(b));self.assertEqual(derive(b),out)
        h=deepcopy(b['heads'][0]);h.update(revision=1,predecessor_revision=0,replaced=True);b['heads'].append(h)
        current=next(x for x in derive(b)['heads'] if x['id']==h['id']);self.assertEqual(current['replacement_state'],'replaced')
        h['predecessor_revision']=None;b['heads'][-1]=h
        self.assertEqual(next(x for x in derive(b)['heads'] if x['id']==h['id'])['replacement_state'],'conflict')

    def test_agent_v1_v2_originals_buckets_settings_clocks_unchanged(self):
        for path in ('model-evidence-v1/complete.golden.json','model-evidence-native-v2/owned-shape.golden.json'):
            b=fixture();agent=json.loads((FIXTURES/path).read_bytes());b['agent_evidence']=agent;b['generated_at']=max(b['generated_at'],agent['generated_at'])
            out=derive(b);self.assertEqual(out['input']['agent_evidence'],agent)
            self.assertEqual(out['denominators']['captured_responses'],len(agent['requests']))
            self.assertEqual(out['denominators']['exact_pr_linked_usage'],0)

    def test_selected_ci_bridge_retains_original_and_unknown_head(self):
        raw=(FIXTURES/'model-evidence-selected-ci-v1/source.json').read_bytes()
        source=json.loads(raw);from collectors.model_evidence.native import timestamp
        b=from_selected_ci(raw,generated_at=timestamp(source['capture_time'])+1);out=derive(b)
        self.assertIsNone(out['input']['heads'][0]['replaced'])
        self.assertEqual(out['input']['selected_ci_inputs'],[source]);self.assertTrue(out['input']['ci_attempts'])
        self.assertIn('replacement-state-unknown',out['heads'][0]['reasons'])

    def test_v1_subset_and_agent_compatibility_do_not_change_old_results(self):
        raw=json.loads((FIXTURES/'model-evidence-head-joins-v1/synthetic.json').read_bytes());before=old_joins.derive(raw)
        b=v1.accept_input(raw);b['assignments']=[];b['operations']=[]
        out=derive(from_v1(b));self.assertEqual(out['compatibility']['v1_input']['agent_evidence'],b['agent_evidence'])
        self.assertEqual(old_joins.derive(raw),before)
        self.assertEqual(out['compatibility']['v1_input']['heads'],b['heads'])

    def test_pr_normalizer_counterexample_preserves_unknown_instead_of_false(self):
        b=fixture();h=b['heads'][0]
        record={'number':h['pr'],'pr':h['repository']+'#'+str(h['pr']),'anchor':h['anchor'],'anchor_kind':h['anchor_kind'],
            'r0':{},'first_decided':{},'policy_source':h['policy_source'],'required':h['required'],'gaps':[],'incomplete':[],
            'heads':[{**{k:deepcopy(h[k]) for k in ('sha','observed','read','refresh_failed','complete','sync','replaced','first_attempt','first_completed','latest')},'why':[]}]}
        with self.assertRaises(ContractError):old_joins.normalize_pr_record(record,h['repository'],h['evidence'])
        rows=normalize_pr_record(record,h['repository'],h['evidence']);self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['replaced'])
        self.assertEqual(record['heads'][0]['replaced'],None)

    def test_result_explicitly_identifies_later_pr_while_original_pr_stays_unknown(self):
        b=fixture();h=deepcopy(b['heads'][0]);h.update(id='head:result',repository='example/project',pr=1,sha='b'*40);b['heads'].append(h)
        a=assignment(derive(b));self.assertIsNone(a['pr']);self.assertEqual(a['result_pr'],1)
        self.assertEqual(a['input_head_ids'],[]);self.assertEqual(a['result_head_ids'],['head:result'])

    def test_v1_representable_assignment_is_structural_and_keeps_exact_clocks(self):
        b=fixture();agent=json.loads((FIXTURES/'model-evidence-native-v2/owned-shape.golden.json').read_bytes())
        run=deepcopy(agent['runs'][0]);run['id']='codex:attempt:one';run['attempt_id']='attempt:one';run['native_session_id']='session:one'
        run['turns']=run['turns'][:1];run['turns'][0]['native_turn_id']='turn:one';run['started_at']=0;run['ended_at']=1000
        agent['runs']=[run];agent['requests']=[];agent['links']=[]
        from collectors.model_evidence.adapter import reconcile
        agent['reconciliation']=reconcile(agent['runs'],agent['requests'],{})
        from collectors.model_evidence.observed_contract import accept_observed
        agent=accept_observed(agent);b['agent_evidence']=agent;b['generated_at']=max(b['generated_at'],agent['generated_at'])
        kind(b,'assignment_intent')['payload'].update(pr=1,actor_binding_id='actor:one')
        r=kind(b,'assignment_resolution')['payload'];r.update(result_sha='a'*40,worker_started_at=0,worker_completed_at=45,resolved_at=45,observed_at=45)
        out=derive(rechain(b));a=assignment(out)
        self.assertIsNotNone(a['v1_row']);self.assertEqual(a['v1_row']['assigned_at'],a['intent_at'])
        self.assertEqual(a['v1_row']['resolved_at'],r['observed_at'])
        self.assertEqual(out['compatibility']['v1_input']['assignments'],[a['v1_row']])

    def test_conflicting_head_scope_does_not_taint_independent_head(self):
        b=fixture();bad=deepcopy(b['heads'][0]);bad['repository']='other/project';b['heads'].append(bad)
        out=derive(b);by={h['id']:h for h in out['heads']};self.assertEqual(by[bad['id']]['replacement_state'],'conflict')
        self.assertTrue(by['head:independent']['known_current']);self.assertEqual(len(out['input']['heads']),3)

    def test_ci_invalid_interval_scoped_history_and_original_preservation(self):
        old=json.loads((FIXTURES/'model-evidence-head-joins-v1/synthetic.json').read_bytes());old=v1.accept_input(old)
        old['assignments']=[];old['operations']=[];b=from_v1(old)
        bad=b['ci_attempts'][0];bad['completed_at']=bad['started_at']-1
        out=derive(b);self.assertIn('check-history-conflict',out['ci'][0]['reasons'])
        self.assertEqual(out['ci'][0]['first_attempt'],'unknown');self.assertIn(bad,out['input']['ci_attempts'])
        self.assertIn('v1-ci-history-unrepresentable',out['compatibility']['reasons'])

    def test_v1_helper_refuses_silent_agent_health_rewrite(self):
        b=json.loads((FIXTURES/'model-evidence-head-joins-v1/synthetic.json').read_bytes());b['operations']=[];b['assignments']=[]
        b['agent_evidence']['health']['state']='complete';before=deepcopy(b)
        with self.assertRaises(ContractError):from_v1(b)
        self.assertEqual(b,before)

    def test_cross_source_assignment_does_not_select_native_owner_mapping(self):
        b=fixture();kind(b,'owner_bound')['recorder_epoch']='epoch:other'
        out=derive(b);a=assignment(out)
        self.assertFalse(a['consistent']);self.assertIsNone(a['owner']);self.assertFalse(a['known']['native_owner'])
        self.assertEqual(len(a['owners']),1)

    def test_json_closed_types_privacy_bounds_and_output_tampering(self):
        for raw in (b'{"schema":1,"schema":2}', b'NaN', b'['*33+b'0'+b']'*33, b'x'*(4*1024*1024+1)):
            with self.assertRaises(ContractError):accept_input(raw)
        for mutation in ('extra','bool','overflow','private','projection','selected'):
            b=fixture()
            if mutation=='extra':b['qualified']=True
            elif mutation=='bool':b['heads'][0]['revision']=True
            elif mutation=='overflow':b['generated_at']=10**1000
            elif mutation=='private':kind(b,'dispatch_observed')['payload']['message']='SYNTHETIC-PRIVATE'
            elif mutation=='projection':b['boundary_snapshots'][0]['authentication']='qualified'
            else:b['selected_ci_inputs']={}
            with self.assertRaises(ContractError):accept_input(b)
        out=derive(fixture());out['denominators']['authenticated_rows']=1
        with self.assertRaises(ContractError):accept_output(out)

    def test_cancellation_and_import_purity(self):
        def cancel():raise CaptureCancelled()
        with self.assertRaises(CaptureCancelled):derive(fixture(),check=cancel)
        sources=[(module,Path(module.__file__).read_text()) for module in (join_contract_v2,joins_v2)]
        with patch('builtins.open',side_effect=AssertionError('file')),patch('os.open',side_effect=AssertionError('file')),patch('subprocess.Popen',side_effect=AssertionError('process')),patch('socket.socket',side_effect=AssertionError('network')),patch('sqlite3.connect',side_effect=AssertionError('database')):
            for module,source in sources:
                namespace={'__name__':'collectors.model_evidence.synthetic_import','__package__':'collectors.model_evidence'}
                exec(compile(source,'<SYNTHETIC-import>','exec'),namespace)

    def test_schema_documents_exact_and_legacy_globals_unchanged(self):
        for name,expected in [('evidence-head-input-v2.schema.json',INPUT_JSON_SCHEMA),('evidence-head-output-v2.schema.json',OUTPUT_JSON_SCHEMA)]:
            self.assertEqual(json.loads((Path(join_contract_v2.__file__).parent/name).read_bytes()),expected)
        before=deepcopy(v1.INPUT_JSON_SCHEMA);derive(fixture());self.assertEqual(v1.INPUT_JSON_SCHEMA,before)


if __name__=='__main__':
    unittest.main()
