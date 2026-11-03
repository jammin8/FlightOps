from copy import deepcopy
import importlib
import json
from pathlib import Path
import unittest
from unittest import mock

from collectors.model_evidence.contract import ContractError,encode,digest
from collectors.model_evidence.capture_contract import event,payload,bounded_json,manifest,AssignmentIntent,LedgerSnapshot,EVENT_BYTES,REPLAY_BYTES
from collectors.model_evidence.capture_projection import project,import_supplied

FIXTURE=Path(__file__).parent/'fixtures'/'model-evidence-capture'/'synthetic.json'


def fixture():
    return json.loads(FIXTURE.read_bytes())


def rechained(rows):
    rows=deepcopy(rows)
    for index,row in enumerate(rows,1):
        row['sequence']=index;row['previous_event_digest']=digest(rows[index-2]) if index>1 else None
    return rows


class CaptureContract(unittest.TestCase):
    def test_closed_synthetic_golden_preserves_all_observations(self):
        raw=FIXTURE.read_bytes();value=fixture();result=import_supplied(raw)
        self.assertEqual(value['label'],'SYNTHETIC')
        self.assertEqual(encode(result),FIXTURE.with_name('synthetic.golden.json').read_bytes())
        self.assertEqual(result['events'],value['events'])
        self.assertEqual(len(result['field_knownness']),8)
        self.assertEqual(result['normalized_rows'],[])
        self.assertEqual(result['custody'],'supplied-replay');self.assertEqual(result['authentication'],'not-performed')

    def test_decoded_duplicate_keys_nan_and_deep_json_rejected(self):
        for raw in (b'{"event_id":1,"event\\u005fid":2}',b'NaN',b'['*1500+b']'*1500):
            with self.assertRaises(ContractError):bounded_json(raw,REPLAY_BYTES)

    def test_unknown_fields_private_values_and_authority_flags_rejected(self):
        for name in ('prompt','message','command','argv','cwd','stdout','stderr','output','auth','secret','private_digest','qualified','source_authority'):
            with self.subTest(name=name):
                row=fixture()['events'][0];row['payload'][name]='PRIVATE-SENTINEL'
                with self.assertRaises(ContractError):event(row)
        value=fixture();value['qualified']=True
        with self.assertRaises(ContractError):import_supplied(encode(value))

    def test_types_boolean_numbers_overflow_and_resource_limits(self):
        for field,value in (('sequence',True),('sequence',1.5),('sequence',-1),('recorded_at',10**1000),('recorded_at',True)):
            row=fixture()['events'][0];row[field]=value
            with self.assertRaises(ContractError):event(row)
        for field,value in (('pr',True),('input_sha','a'*12),('repository','Example/Project'),('task_kind','review this task text')):
            row=fixture()['events'][0];row['payload'][field]=value
            with self.assertRaises(ContractError):event(row)
        row=fixture()['events'][0];row['dependencies']=['event:one']*65
        with self.assertRaises(ContractError):event(row)
        row=fixture()['events'][3];row['payload']['parent_shas']=['a'*40]*9
        with self.assertRaises(ContractError):event(row)
        with self.assertRaises(ContractError):bounded_json(b' '*(REPLAY_BYTES+1),REPLAY_BYTES)

    def test_event_bytes_and_clock_limits(self):
        row=fixture()['events'][0]
        ref={'artifact_id':'a'*160,'digest':'b'*64,'ordinal':1,'source_at':1}
        row['evidence_refs']=[ref]*64
        with self.assertRaises(ContractError):event(row)
        row=fixture()['events'][0];row['effective_at']=row['recorded_at']+1
        with self.assertRaises(ContractError):event(row)
        row=fixture()['events'][4];row['payload']['completed_at']=row['recorded_at']+1
        with self.assertRaises(ContractError):event(row)

    def test_reference_paths_and_private_prose_cannot_enter_ids(self):
        for value in ('/private/path','../escape','PRIVATE SENTINEL','x\ny'):
            row=fixture()['events'][0];row['event_id']=value
            with self.assertRaises(ContractError):event(row)
        row=fixture()['events'][5];row['payload']['destination_ref']='refs/heads/a/../b'
        with self.assertRaises(ContractError):event(row)

    def test_relabelled_import_cannot_promote_custody(self):
        result=import_supplied(FIXTURE.read_bytes())
        self.assertFalse(result['merge_authority']);self.assertEqual(result['denominators']['authenticated_rows'],0)
        value=fixture();value['custody']='live-observed'
        with self.assertRaises(ContractError):import_supplied(encode(value))
        row=fixture()['events'][0];row['source']='trusted'
        with self.assertRaises(ContractError):event(row)
        with self.assertRaises(ContractError):project(fixture()['events'],ledger_gaps=['PRIVATE ERROR BODY'])
        row=fixture()['events'][-1];row['payload']['explicit_gaps']=['PRIVATE ERROR BODY']
        with self.assertRaises(ContractError):event(row)
        with self.assertRaises(ContractError):LedgerSnapshot(None,[{'prompt':'PRIVATE'}],[],100).fields()
        with self.assertRaises(ContractError):LedgerSnapshot(None,fixture()['events'],[],999).fields()

    def test_unknown_replacement_and_shared_inventory_retained(self):
        result=import_supplied(FIXTURE.read_bytes())
        self.assertIsNone(result['head_observations'][0]['replaced'])
        self.assertFalse(result['head_observations'][0]['replacement_known'])
        self.assertEqual(result['head_observations'][0]['bridge'],'unavailable-v1-unknown-replacement')
        a=result['assignments'][0]
        self.assertEqual(len(a['intent']['memberships']),2)
        self.assertIn('shared-pr-membership-allocation-unknown',a['reasons'])
        self.assertIn('usage-unbound',a['reasons']);self.assertIn('retention-unproven',result['gaps'])

    def test_exact_duplicates_idempotent_conflicts_affect_identity(self):
        rows=fixture()['events'];result=project(rows+[deepcopy(rows[0])])
        self.assertNotIn('event-id-conflict',[d['reason'] for d in result['dispositions']])
        changed=deepcopy(rows[0]);changed['payload']['input_sha']='e'*40
        result=project(rows+[changed])
        self.assertIn('event-id-conflict',[d['reason'] for d in result['dispositions']])
        self.assertIn('assignment-conflict',result['assignments'][0]['reasons'])
        self.assertEqual(result['denominators']['selected_events'],9)

    def test_sequence_holes_and_missing_dependencies_remain_gaps(self):
        rows=fixture()['events'];result=project(rows[1:])
        self.assertIn('source-sequence-gap',result['gaps'])
        rows=fixture()['events'];rows[0]['dependencies']=['missing:event']
        result=project(rows)
        self.assertIn('event-dependency-missing-or-cyclic',[d['reason'] for d in result['dispositions']])
        self.assertIn('assignment-conflict',result['assignments'][0]['reasons'])

    def test_event_limit_unknown_kind_and_depth_lexing(self):
        with self.assertRaises(ContractError):project([fixture()['events'][0]]*5001)
        for kind in ([],{},'unrecognized'):
            row=fixture()['events'][0];row['kind']=kind
            with self.assertRaises(ContractError):event(row)
        self.assertEqual(bounded_json(b'{"x":"[[[\\\"}]]"}',1024),{'x':'[[["}]]'})

    def test_coverage_cannot_close_missing_sequence_or_checkpoint_identity(self):
        rows=fixture()['events'];rows[-1]['payload'].update(last_sequence=999999,prior_checkpoint_id=rows[0]['event_id'])
        result=project(rows)
        self.assertIn('checkpoint-sequence-gap',result['gaps'])
        self.assertIn('checkpoint-predecessor-missing-or-invalid',result['gaps'])
        self.assertIn('fleet-inventory-unknown',result['gaps'])

    def test_dangling_and_cyclic_event_dependencies(self):
        rows=fixture()['events'];rows[0]['dependencies']=[rows[1]['event_id']];rows[1]['dependencies']=[rows[0]['event_id']]
        result=project(rows)
        self.assertEqual(len([d for d in result['dispositions'] if d['reason']=='event-dependency-missing-or-cyclic']),2)

    def test_source_local_order_not_global_equal_clock_order(self):
        rows=fixture()['events'];before=project(rows)
        after=project(list(reversed(rows)))
        self.assertEqual(before['assignments'],after['assignments'])
        self.assertEqual(before['operations'],after['operations'])
        self.assertEqual(before['gaps'],after['gaps'])

    def test_role_is_explicit_type_not_task_text(self):
        value=fixture()['events'][0]['payload'];value['role']='review'
        intent=AssignmentIntent(**value);self.assertEqual(intent.role,'review')
        value['task_kind']='task says implementation'
        with self.assertRaises(ContractError):AssignmentIntent(**value)

    def test_imports_do_not_open_application_state(self):
        modules=['capture_contract','capture_ledger','capture_projection','capture_boundary','capture_cli']
        loaded=[importlib.import_module('collectors.model_evidence.'+name) for name in modules]
        compiled=[(compile(Path(module.__file__).read_bytes(),module.__file__,'exec'),dict(module.__dict__)) for module in loaded]
        with mock.patch('builtins.open',side_effect=AssertionError('file')),mock.patch('os.open',side_effect=AssertionError('file')),mock.patch('socket.socket',side_effect=AssertionError('network')),mock.patch('subprocess.Popen',side_effect=AssertionError('process')),mock.patch('sqlite3.connect',side_effect=AssertionError('database')):
            for code,namespace in compiled:exec(code,namespace)


if __name__=='__main__':
    unittest.main()
