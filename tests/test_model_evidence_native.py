import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collectors import runs
from collectors.model_evidence import ContractError, encode
from collectors.model_evidence.contract import digest
from collectors.model_evidence.acquisition import Limits, acquire, read_native, discover, safe_path, ALLOWED_PROJECTS
from collectors.model_evidence.adapter import derive_observed
from collectors.model_evidence.native import timestamp
from collectors.model_evidence.observed_contract import accept_observed, JSON_SCHEMA
from collectors.model_evidence.projection import PREFIX, FIELDS, connection, read_projection, AcquisitionCancelled
from collectors.model_evidence.replay import replay_owned

DIR = Path(__file__).parent/'fixtures'/'model-evidence-native-v2'
PROJECT = 'mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914'


def shape():
    return json.loads((DIR/'owned-shape.json').read_text())


def replay(s=None):
    s=s or shape()
    return replay_owned(s['fixture'],s['thread'],correction=s['correction'],origin='synthetic-replay')


def projection(s):
    f=s['fixture']
    return {'threads':[s['thread']],'runs':[f['run']],'run_attempts':f['attempts'],
            'provider_threads':[f['provider']],'provider_turns':f['turns'],'gaps':[]}


def native_rows(s):
    f=s['fixture']; rows={r['line']:r for r in f['native']['records']}
    for r in s['correction']['records']:
        rows.setdefault(r['line'], {'line':r['line'],'timestamp':r['timestamp'],'type':'event_msg',
                                   'payload':{k:v for k,v in r.items() if k not in ('line','timestamp')}})
    return [rows[k] for k in sorted(rows)]


def make_db(path,s):
    c=sqlite3.connect(path)
    for kind in FIELDS:
        key={'threads':'thread_id','runs':'run_id','run_attempts':'attempt_id','provider_threads':'provider_thread_id','provider_turns':'provider_turn_id'}[kind]
        columns={name:'TEXT' for name in (key,'project_id','thread_id','run_id','run_attempt_id','provider_thread_id','requested_at','completed_at','payload_json')}
        c.execute('CREATE TABLE '+PREFIX+kind+' ('+','.join(name+' '+typ+(' PRIMARY KEY' if name==key else '') for name,typ in columns.items())+')')
    p=projection(s)
    for kind,rows in p.items():
        if kind=='gaps':continue
        key={'threads':'thread_id','runs':'run_id','run_attempts':'attempt_id','provider_threads':'provider_thread_id','provider_turns':'provider_turn_id'}[kind]
        for row in rows:
            values={key:row['id'],'project_id':s['thread']['projectId'],'payload_json':json.dumps(row)}
            if key!='thread_id':values['thread_id']=s['thread']['id']
            if key!='run_id':values['run_id']=row.get('runId')
            if key!='provider_thread_id':values['provider_thread_id']=row.get('providerThreadId')
            values.update(run_attempt_id=row.get('runAttemptId'),requested_at=row.get('requestedAt'),completed_at=row.get('completedAt'))
            c.execute('INSERT INTO '+PREFIX+kind+' ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
    c.execute('CREATE TABLE forbidden_messages (body TEXT)')
    c.execute("INSERT INTO forbidden_messages VALUES ('PRIVATE-BODY')")
    c.commit();c.close()


def make_sources(d,s=None):
    s=s or shape(); root=Path(d)/'sessions'; day=root/'2026'/'10'/'29';day.mkdir(parents=True)
    session=s['fixture']['attempts'][0]['nativeThreadId']
    path=day/('rollout-2026-10-29T00-00-00-'+session+'.jsonl')
    rows=native_rows(s)
    rows.insert(1,{'timestamp':'2026-10-29T21:40:00Z','type':'response_item','payload':{'type':'message','content':'PRIVATE-PROMPT'}})
    rows.append({'timestamp':'2026-10-29T22:40:00Z','type':'response_item','payload':{'type':'custom_tool_call','input':'PRIVATE-COMMAND','call_id':'call-private'}})
    path.write_bytes(b''.join(encode({k:v for k,v in r.items() if k!='line'}) for r in rows))
    db=Path(d)/'state.sqlite';make_db(db,s)
    at=timestamp(s['fixture']['observed_at'])
    scope={'sqlite_path':db,'native_roots':[root],'projects':[PROJECT],'window':{'start':at-90*86400,'end':at}}
    return s,scope,path,at


class NativeReplay(unittest.TestCase):
    def test_exact_fifteen_responses_match_all_five_processed_buckets_and_golden(self):
        out=replay();r=out['reconciliation'][0]
        self.assertEqual(len(out['requests']),15)
        self.assertEqual(r['state'],'match');self.assertTrue(all(v is True for v in r['matches'].values()))
        self.assertEqual(r['observed']['input_total'],4375823)
        self.assertEqual(r['observed']['buckets'],{'fresh_input':63247,'cache_read':4312576,'cache_write':0,'output':28370,'reasoning':13749})
        self.assertEqual(encode(out),(DIR/'owned-shape.golden.json').read_bytes())
        self.assertEqual(out['operations'],[]);self.assertFalse(out['health']['live_qualified'])
        self.assertEqual(out['health']['state'],'partial')

    def test_settings_before_task_start_and_context_are_reported_not_requested(self):
        out=replay();t=out['runs'][0]['turns'][0]
        self.assertEqual(t['reported']['service_tier']['value'],'default')
        self.assertEqual(t['reported']['service_tier']['evidence']['line'],605)
        self.assertEqual(t['reported']['provider']['value'],'openai')
        self.assertEqual(t['reported']['model_id']['basis'],'native-turn-context')
        self.assertEqual(t['requested']['model_id']['basis'],'orch-request')
        self.assertIsNone(t['reported']['instance']['value'])

    def test_original_late_settings_do_not_backfill_earlier_turn(self):
        s=shape();out=replay_owned(s['fixture'],s['thread'],origin='synthetic-replay')
        self.assertIsNone(out['runs'][0]['turns'][0]['reported']['service_tier']['value'])
        self.assertEqual(out['reconciliation'][0]['state'],'match')
        self.assertTrue(any(o['settings']['service_tier']['value']=='default' and o['evidence']['line']==755 for o in out['settings_observations']))

    def test_next_turn_setting_switch_never_relabels_previous_responses(self):
        s=shape()
        for row in s['fixture']['native']['records']:
            if row['line']==755:row['payload']['thread_settings']['service_tier']='priority'
        for row in s['correction']['records']:
            if row['line']==755:row['thread_settings']['service_tier']='priority'
        out=replay(s)
        self.assertTrue(all(q['reported']['service_tier']['value']=='default' for q in out['requests']))
        self.assertTrue(any(o['settings']['service_tier']['value']=='priority' for o in out['settings_observations']))

    def order_case(self):
        s=shape();rows=native_rows(s)
        first=next(r for r in rows if r['type']=='token_usage_record')
        session=s['fixture']['attempts'][0]['nativeThreadId'];at=timestamp(s['fixture']['observed_at'])
        def context(line):
            row=copy.deepcopy(next(r for r in rows if r['type']=='turn_context'))
            row.update(line=line,timestamp=first['timestamp']);row['payload']['model']='model-at-the-same-millisecond'
            return row
        def derive(*snapshots):
            snaps=[{'records':rows,'digest':digest(rows),'gaps':[]} for rows in snapshots]
            return derive_observed(projection(s),{session:snaps},projects=[PROJECT],window={'start':at-90*86400,'end':at},as_of=at,acquired_at=at)
        def reported(out):
            return next(q for q in out['requests'] if q['native_request_id']==first['payload']['response_id'])['reported']['model_id']
        return rows,first,context,derive,reported

    def test_a_later_line_at_the_same_millisecond_never_relabels_the_response(self):
        rows,first,context,derive,reported=self.order_case()
        out=derive(rows+[context(first['line']+1)])
        self.assertEqual(reported(out)['value'],'gpt-6.1-sol')
        self.assertEqual(reported(out)['evidence']['line'],607)
        self.assertNotIn('native-settings-order-unknown',out['inventory']['gaps'])

    def test_an_earlier_line_at_the_same_millisecond_applies_to_the_response(self):
        rows,first,context,derive,reported=self.order_case()
        out=derive(rows+[context(first['line']-1)])
        self.assertEqual(reported(out)['value'],'model-at-the-same-millisecond')
        self.assertEqual(reported(out)['evidence']['line'],first['line']-1)

    def test_copies_that_disagree_on_order_leave_the_setting_unknown(self):
        rows,first,context,derive,reported=self.order_case()
        out=derive(rows+[context(first['line']+1)],rows+[context(first['line']-1)])
        self.assertIsNone(reported(out)['value'])
        self.assertIn('native-settings-order-unknown',out['inventory']['gaps'])

    def test_a_copy_without_the_response_cannot_order_the_context(self):
        rows,first,context,derive,reported=self.order_case()
        out=derive(rows,[r for r in rows if r is not first]+[context(first['line']+1)])
        self.assertIsNone(reported(out)['value'])
        self.assertIn('native-settings-order-unknown',out['inventory']['gaps'])

    def test_order_unknown_at_the_same_millisecond_does_not_reach_a_later_response(self):
        rows,first,context,derive,reported=self.order_case()
        second=[r for r in rows if r['type']=='token_usage_record'][1]
        out=derive(rows+[context(first['line']+1)],rows+[context(first['line']-1)])
        later=next(q for q in out['requests'] if q['native_request_id']==second['payload']['response_id'])
        self.assertEqual(later['reported']['model_id']['value'],'model-at-the-same-millisecond')

    def provider_case(self):
        s=shape();rows=native_rows(s);p=projection(s)
        session=s['fixture']['attempts'][0]['nativeThreadId'];at=timestamp(s['fixture']['observed_at'])
        def derive():
            return derive_observed(p,{session:[{'records':rows,'digest':digest(rows),'gaps':[]}]},projects=[PROJECT],window={'start':at-90*86400,'end':at},as_of=at,acquired_at=at)
        return p,derive

    def test_contradictory_provider_relations_are_not_a_matched_run(self):
        cases=[('runs','providerThreadId','other-provider-thread','run-provider-reference-mismatch')]
        for table in ('runs','run_attempts','provider_threads'):
            cases.append((table,'providerInstanceId','other-instance','provider-instance-conflict'))
            cases.append((table,'providerInstanceId',None,'provider-instance-unknown'))
        for table,field,value,code in cases:
            p,derive=self.provider_case()
            p[table][0][field]=value
            out=derive()
            self.assertEqual((out['runs'],out['requests'],out['reconciliation']),([],[],[]),(table,code))
            self.assertEqual([e['code'] for e in out['errors']],[code],(table,code))
            self.assertIn('unsupported-run-or-attempt',out['inventory']['gaps'],(table,code))

    def test_both_conflicts_from_the_teardown_scenario_refuse_the_run(self):
        p,derive=self.provider_case()
        p['runs'][0]['providerThreadId']='other-provider-thread';p['run_attempts'][0]['providerInstanceId']='other-instance'
        out=derive()
        self.assertEqual((out['runs'],out['reconciliation']),([],[]))
        self.assertEqual(len(out['errors']),1)

    def test_a_run_that_names_no_provider_thread_keeps_its_attempt(self):
        p,derive=self.provider_case()
        p['runs'][0]['providerThreadId']=None
        out=derive()
        self.assertEqual(len(out['runs']),1)
        self.assertEqual(out['reconciliation'][0]['state'],'match')

    def test_a_contradictory_attempt_leaves_an_independent_run_accepted(self):
        p,derive=self.provider_case()
        other=copy.deepcopy(p['run_attempts'][0]);other.update(id='attempt:synthetic:2',providerInstanceId='other-instance')
        p['run_attempts'].append(other)
        out=derive()
        self.assertEqual([r['attempt_id'] for r in out['runs']],['attempt:synthetic:1'])
        self.assertEqual(out['reconciliation'][0]['state'],'match')
        self.assertEqual([e['code'] for e in out['errors']],['provider-instance-conflict'])

    def test_context_switch_stays_per_response_and_turn_aggregate_is_unknown(self):
        s=shape();row=copy.deepcopy(next(r for r in s['fixture']['native']['records'] if r['type']=='turn_context'))
        row.update(line=640,timestamp='2026-10-29T22:30:00Z');row['payload']['model']='switched-model'
        s['fixture']['native']['records'].append(row)
        out=replay(s)
        self.assertEqual({q['reported']['model_id']['value'] for q in out['requests']},{'gpt-6.1-sol','switched-model'})
        self.assertIsNone(out['runs'][0]['turns'][0]['reported']['model_id']['value'])

    def test_optional_cache_write_missing_is_unknown_not_zero(self):
        s=shape();row=next(r for r in s['fixture']['native']['records'] if r['type']=='token_usage_record')
        del row['payload']['usage']['cache_write_input_tokens']
        out=replay(s)
        self.assertEqual(out['reconciliation'][0]['state'],'unknown')
        self.assertIsNone(out['reconciliation'][0]['observed']['buckets']['cache_write'])

    def test_copied_responses_count_once_and_conflicting_copies_are_unknown(self):
        s=shape();p=projection(s);rows=native_rows(s);session=s['fixture']['attempts'][0]['nativeThreadId'];at=timestamp(s['fixture']['observed_at'])
        snaps=[{'records':rows,'digest':'a'*64,'gaps':[]},{'records':copy.deepcopy(rows),'digest':'b'*64,'gaps':[]}]
        def derive():return derive_observed(p,{session:snaps},projects=[PROJECT],window={'start':at-90*86400,'end':at},as_of=at,acquired_at=at)
        out=derive();self.assertEqual(len(out['requests']),15);self.assertTrue(all(q['copies']==2 for q in out['requests']))
        row=next(r for r in snaps[1]['records'] if r['type']=='token_usage_record');row['payload']['usage']['output_tokens']+=1
        out=derive();self.assertTrue(any(q['conflict'] for q in out['requests']));self.assertEqual(out['reconciliation'][0]['state'],'unknown')

    def test_late_revision_updates_original_request_without_context_total_fallback(self):
        s=shape();old=next(r for r in s['fixture']['native']['records'] if r['type']=='token_usage_record');later=copy.deepcopy(old)
        later.update(line=760,timestamp='2026-10-29T22:59:00Z');later['payload']['usage'].pop('cache_write_input_tokens')
        s['fixture']['native']['records'].append(later)
        out=replay(s);q=next(q for q in out['requests'] if q['native_request_id']==old['payload']['response_id'])
        self.assertEqual(len(out['requests']),15);self.assertEqual(q['copies'],2);self.assertIsNone(q['usage']['buckets']['cache_write'])
        self.assertEqual(q['original_native_turn_id'],old['payload']['turn_id'])

    def test_context_occupancy_never_substitutes_for_missing_processed_usage(self):
        s=shape();del s['fixture']['turns'][0]['turnTokenUsage']
        out=replay(s)
        self.assertFalse(out['reconciliation'][0]['expected']['complete'])
        self.assertEqual(out['reconciliation'][0]['observed']['input_total'],4375823)
        self.assertIn('processed-main-usage-unknown',out['inventory']['gaps'])

    def test_child_and_in_progress_and_missing_completion_remain_incomplete(self):
        for variant in ('child','progress','missing-completion'):
            s=shape()
            if variant=='child':s['fixture']['turns'][0]['turnTokenUsage']['hasSubagents']=True
            elif variant=='progress':s['fixture']['turns'][0]['status']='running'
            else:
                s['fixture']['native']['records']=[r for r in s['fixture']['native']['records'] if r['line']!=754]
                s['correction']['records']=[r for r in s['correction']['records'] if r['line']!=754]
            out=replay(s);self.assertEqual(out['health']['state'],'partial')
            if variant=='child':self.assertIsNone(out['runs'][0]['expected_children'])
            else:self.assertNotEqual(out['reconciliation'][0]['state'],'match')

    def test_unrelated_native_session_cannot_own_usage(self):
        s=shape();row=next(r for r in s['fixture']['native']['records'] if r['type']=='token_usage_record')
        row['payload']['session_id']='wrong'
        out=replay(s);self.assertEqual(len(out['requests']),14);self.assertEqual(out['reconciliation'][0]['state'],'mismatch')

    def test_schema_document_and_last_good_version_dispatch(self):
        self.assertEqual(json.loads(Path('collectors/model_evidence/agent-evidence-v2.schema.json').read_text()),JSON_SCHEMA)
        out=replay();now=out['generated_at']+1
        good=runs.ingest(None,encode(out),now=now)
        self.assertIsNone(good['last_error'])
        for raw in (None,b'{"schema":"agent-evidence/0"}',b'{"schema":'):
            bad=runs.ingest(good,raw,now=now+100)
            self.assertEqual(bad['export'],good['export']);self.assertEqual(bad['received_at'],now)
        corrupt=copy.deepcopy(out);corrupt['reconciliation'][0]['observed']['input_total']=999
        self.assertEqual(runs.ingest(good,encode(corrupt),now=now+101)['last_error'],'source-invalid')


class CanonicalProjectIdentity(unittest.TestCase):
    canonical_projects = (
        '15422c94-4afe-4fff-b056-f3b240bc63ce',
        '93db29b0-fce2-415c-9c1b-48a687f8302f',
        '175492b5-29c1-4884-9919-3b56a5d0f1c9',
        '851362d8-b65b-4e0e-8781-53c2aadaa12e',
        'mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914',
    )

    def acquire_or_fail(self, scope, at):
        try:
            return acquire(scope, acquired_at=at)
        except ContractError as exc:
            self.fail('Canonical registry project unexpectedly refused: '+str(exc))

    def assert_project_acquired(self, project):
        s = shape()
        s['thread']['projectId'] = project
        with tempfile.TemporaryDirectory() as d:
            _, scope, _, at = make_sources(d, s)
            scope['projects'] = [project]
            scope['window']['start'] = at-86400
            out = self.acquire_or_fail(scope, at)
        self.assertEqual(out['inventory']['projects'], [project])
        self.assertEqual([link['project_id'] for link in out['links']], [project])
        self.assertEqual(out['inventory']['compatible_turns'], 1)
        self.assertEqual(len(out['requests']), 15)
        self.assertEqual(out['reconciliation'][0]['state'], 'match')
        self.assertTrue(all(v is True for v in out['reconciliation'][0]['matches'].values()))
        self.assertEqual(out['reconciliation'][0]['observed']['buckets'],
                         {'fresh_input':63247,'cache_read':4312576,'cache_write':0,'output':28370,'reasoning':13749})
        self.assertEqual(out['operations'], [])
        self.assertFalse(out['health']['live_qualified'])
        self.assertNotIn(b'PRIVATE-', encode(out))

    def test_scratch_bare_registry_uuid_is_admitted(self):
        self.assert_project_acquired('15422c94-4afe-4fff-b056-f3b240bc63ce')

    def test_backend_bare_registry_uuid_is_admitted(self):
        self.assert_project_acquired('93db29b0-fce2-415c-9c1b-48a687f8302f')

    def test_web_bare_registry_uuid_is_admitted(self):
        self.assert_project_acquired('175492b5-29c1-4884-9919-3b56a5d0f1c9')

    def test_ios_bare_registry_uuid_is_admitted(self):
        self.assert_project_acquired('851362d8-b65b-4e0e-8781-53c2aadaa12e')

    def test_flightops_prefixed_registry_id_is_admitted(self):
        self.assert_project_acquired('mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914')

    def test_all_five_ids_reach_projection_verbatim(self):
        projects = list(self.canonical_projects)
        with tempfile.TemporaryDirectory() as d:
            _, scope, _, at = make_sources(d)
            scope['projects'] = projects
            scope['window']['start'] = at-86400
            with patch('collectors.model_evidence.acquisition.read_projection', wraps=read_projection) as reader:
                out = self.acquire_or_fail(scope, at)
                self.assertEqual(reader.call_args.args[1], projects)
        self.assertEqual(out['inventory']['projects'], sorted(projects))
        self.assertEqual([link['project_id'] for link in out['links']], [PROJECT])
        self.assertEqual(len(out['requests']), 15)
        self.assertEqual(out['reconciliation'][0]['state'], 'match')

    def assert_rejected_before_source_access(self, projects):
        for project in projects:
            with self.subTest(project=project):
                scope = {'projects':[project],'window':{'start':0,'end':1},'native_roots':[],
                         'sqlite_path':Path('unopened.sqlite')}
                with patch('collectors.model_evidence.acquisition.read_projection',
                           side_effect=AssertionError('Unapproved project reached source access')) as reader:
                    with self.assertRaisesRegex(ContractError, 'project-allowlist'):
                        acquire(scope, acquired_at=1)
                    reader.assert_not_called()

    def test_incorrectly_prefixed_bare_registry_ids_are_rejected(self):
        self.assert_rejected_before_source_access('mcp:'+p for p in self.canonical_projects[:4])

    def test_flightops_bare_and_double_prefixed_aliases_are_rejected(self):
        self.assert_rejected_before_source_access((
            '5cffed67-debf-4e5f-ace3-1d3ca260d914',
            'mcp:mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914',
        ))

    def test_unrelated_registry_shaped_ids_are_rejected(self):
        self.assert_rejected_before_source_access((
            '00000000-0000-4000-8000-000000000000',
            'mcp:00000000-0000-4000-8000-000000000000',
        ))

    def test_cli_bare_backend_succeeds_and_prefixed_alias_preserves_last_good(self):
        project = '93db29b0-fce2-415c-9c1b-48a687f8302f'
        s = shape()
        s['thread']['projectId'] = project
        with tempfile.TemporaryDirectory() as d:
            _, scope, _, at = make_sources(d, s)
            dest = Path(d)/'publication.json'
            args = [sys.executable,'-B','-m','collectors.model_evidence.acquire_cli',
                    '--sqlite',str(scope['sqlite_path']),'--native-root',str(scope['native_roots'][0]),
                    '--project',project,'--window-start',str(at-86400),'--as-of',str(at),'--output',str(dest)]
            good = subprocess.run(args, capture_output=True, text=True, timeout=7)
            self.assertEqual(good.returncode, 0, good.stderr)
            before = dest.read_bytes()
            out = json.loads(before)
            self.assertEqual(out['inventory']['projects'], [project])
            self.assertEqual([link['project_id'] for link in out['links']], [project])
            self.assertEqual(len(out['requests']), 15)
            self.assertEqual(out['reconciliation'][0]['state'], 'match')
            self.assertFalse(out['health']['live_qualified'])
            args[args.index('--project')+1] = 'mcp:'+project
            bad = subprocess.run(args, capture_output=True, text=True, timeout=7)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(dest.read_bytes(), before)
            self.assertNotIn('PRIVATE-', bad.stderr)


class ReadOnlyAcquisition(unittest.TestCase):
    def test_current_projection_joins_and_native_files_without_raw_body_publication(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d)
            before=hashlib.sha256(scope['sqlite_path'].read_bytes()).hexdigest()
            out=acquire(scope,acquired_at=at)
            self.assertEqual(out['origin']['kind'],'readonly-acquisition')
            self.assertEqual(out['reconciliation'][0]['state'],'match');self.assertEqual(len(out['requests']),15)
            self.assertEqual(hashlib.sha256(scope['sqlite_path'].read_bytes()).hexdigest(),before)
            self.assertFalse(any(x in encode(out) for x in (b'PRIVATE-',b'forbidden_messages',b'rollout-',str(path).encode())))
            self.assertIn('unsupported-operation-binding',out['inventory']['gaps'])

    def test_sqlite_is_readonly_queryonly_and_denies_nonmetadata_reads(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,_,at=make_sources(d)
            c=connection(scope['sqlite_path'],2)
            try:
                with self.assertRaises(sqlite3.DatabaseError):c.execute('SELECT * FROM forbidden_messages').fetchall()
                with self.assertRaises(sqlite3.DatabaseError):c.execute('CREATE TABLE write_attempt (x)')
                with self.assertRaises(sqlite3.DatabaseError):c.execute('ATTACH DATABASE ? AS other',(':memory:',))
                c.set_authorizer(None)
                self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0],1)
                c.execute('PRAGMA query_only=OFF')
                with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM '+PREFIX+'runs')
            finally:c.close()

    def test_project_allowlist_and_explicit_ninety_day_window(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,_,at=make_sources(d)
            scope['projects']=['mcp:unrelated']
            with self.assertRaises(ContractError):acquire(scope,acquired_at=at)
            scope['projects']=[PROJECT];scope['window']['start']=at-91*86400
            with self.assertRaises(ContractError):acquire(scope,acquired_at=at)

    def test_missing_and_old_projection_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'missing.sqlite'
            with self.assertRaises((ContractError,sqlite3.Error)):read_projection(db,[PROJECT],{'start':0,'end':1})
            sqlite3.connect(db).close()
            with self.assertRaises(ContractError):read_projection(db,[PROJECT],{'start':0,'end':1})

    def test_missing_native_file_and_unsupported_provider_are_explicit_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);path.unlink()
            out=acquire(scope,acquired_at=at)
            self.assertIn('native-source-missing',out['inventory']['gaps']);self.assertEqual(out['inventory']['compatible_turns'],0)
            p=projection(s);p['provider_threads'][0]['driver']='other-provider'
            out=derive_observed(p,{},projects=[PROJECT],window=scope['window'],as_of=at,acquired_at=at)
            self.assertEqual(out['runs'],[]);self.assertIn('unsupported-provider',[e['code'] for e in out['errors']])

    def test_relevant_filename_inventory_limits_and_symlink_scope(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);session=s['fixture']['attempts'][0]['nativeThreadId']
            outside=Path(d)/'outside.jsonl';outside.write_bytes(b'not read')
            link=path.parent/'rollout-link.jsonl';link.symlink_to(outside)
            with self.assertRaises(ContractError):read_native(link,scope['native_roots'][0],Limits())
            with self.assertRaises(ContractError):safe_path(outside,scope['native_roots'][0])
            paths,gaps=discover(scope['native_roots'],{session},scope['window'],Limits(entries=1))
            self.assertIn('native-inventory-limit',gaps)

    def test_native_byte_line_and_incomplete_line_limits(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);root=scope['native_roots'][0]
            out=read_native(path,root,Limits(file_bytes=200))
            self.assertTrue(set(out['gaps']) & {'native-byte-or-line-limit','native-prefix-only'})
            out=read_native(path,root,Limits(lines=1))
            self.assertIn('native-line-limit',out['gaps'])
            path.write_bytes(encode({'type':'session_meta','timestamp':'2026-10-29T00:00:00Z','payload':{'id':'x'}}).rstrip())
            self.assertIn('native-interrupted-line',read_native(path,root,Limits())['gaps'])

    def test_native_changed_while_reading_invalidates_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);calls=0
            def change():
                nonlocal calls
                calls+=1
                if calls==2:
                    with path.open('ab') as f:f.write(b'{}\n')
            result=read_native(path,scope['native_roots'][0],Limits(),check=change)
            self.assertIn('native-changed-during-read',result['gaps'])

    def test_duplicate_and_nonfinite_native_json_cannot_be_exact_facts(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d)
            with path.open('ab') as f:f.write(b'{"type":"token_usage_record","payload":{},"payload":{}}\n{"x":NaN}\n')
            result=read_native(path,scope['native_roots'][0],Limits())
            self.assertIn('invalid-native-record',result['gaps'])

    def test_cancelled_reader_propagates_fatal_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);before=path.read_bytes()
            def cancel():raise AcquisitionCancelled()
            with self.assertRaises(AcquisitionCancelled):read_native(path,scope['native_roots'][0],Limits(),check=cancel)
            self.assertEqual(path.read_bytes(),before)

    def test_projection_row_budget_is_explicit_and_not_complete_empty(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d)
            c=sqlite3.connect(scope['sqlite_path']);r=copy.deepcopy(s['fixture']['run']);r['id']='run:extra'
            c.execute('INSERT INTO '+PREFIX+'runs (run_id,thread_id,requested_at,completed_at,payload_json) VALUES (?,?,?,?,?)',
                      (r['id'],r['threadId'],r['requestedAt'],r['completedAt'],json.dumps(r)))
            c.commit();c.close()
            out=read_projection(scope['sqlite_path'],[PROJECT],scope['window'],max_rows=1)
            self.assertIn('projection-row-limit',out['gaps']);self.assertEqual(len(out['runs']),1)

    def test_real_alarm_inside_native_reader_keeps_last_good_destination(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);dest=Path(d)/'publication.json';dest.write_bytes(b'LAST-GOOD')
            script='''
import sys,time
from collectors.model_evidence import acquire_cli,acquisition
import os as _os
import tempfile as _tempfile
_tempfile.tempdir = _os.path.realpath(_tempfile.gettempdir())
original=acquisition.project_record
def stalled(*a,**kw):
 time.sleep(2)
 return original(*a,**kw)
acquisition.project_record=stalled
raise SystemExit(acquire_cli.main(sys.argv[1:]))
'''
            args=['--sqlite',str(scope['sqlite_path']),'--native-root',str(scope['native_roots'][0]),'--project',PROJECT,
                  '--window-start',str(scope['window']['start']),'--as-of',str(at),'--output',str(dest),'--timeout','1']
            p=subprocess.run([sys.executable,'-c',script,*args],capture_output=True,text=True,timeout=5)
            self.assertEqual(p.returncode,2,p.stderr);self.assertEqual(dest.read_bytes(),b'LAST-GOOD')
            self.assertNotIn('PRIVATE-',p.stderr)

    def test_cli_success_writes_only_the_explicit_tmp_output(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);dest=Path(d)/'publication.json'
            args=['--sqlite',str(scope['sqlite_path']),'--native-root',str(scope['native_roots'][0]),'--project',PROJECT,
                  '--window-start',str(scope['window']['start']),'--as-of',str(at),'--output',str(dest)]
            p=subprocess.run([sys.executable,'-m','collectors.model_evidence.acquire_cli',*args],capture_output=True,text=True,timeout=7)
            self.assertEqual(p.returncode,0,p.stderr)
            out=json.loads(dest.read_bytes());self.assertEqual(out['origin']['kind'],'readonly-acquisition')
            self.assertEqual(len(out['requests']),15);self.assertFalse(out['health']['live_qualified'])

    def test_relational_payload_identity_mismatch_cannot_create_run(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d)
            c=sqlite3.connect(scope['sqlite_path']);c.execute('UPDATE '+PREFIX+'runs SET run_id=?',('wrong-relational-id',));c.commit();c.close()
            result=acquire(scope,acquired_at=at)
            self.assertEqual(result['runs'],[])
            self.assertIn('projection-identity-mismatch',result['inventory']['gaps'])

    def test_child_thread_metadata_is_preserved_without_inventing_parent_turn(self):
        s=shape();p=projection(s);child={'id':'thread:child','projectId':PROJECT,'lineage':{'parentThreadId':s['thread']['id']}}
        p['threads'].append(child);session=s['fixture']['attempts'][0]['nativeThreadId'];at=timestamp(s['fixture']['observed_at'])
        result=derive_observed(p,{session:[{'records':native_rows(s),'digest':'a'*64,'gaps':[]}]},projects=[PROJECT],
                               window={'start':at-90*86400,'end':at},as_of=at,acquired_at=at)
        self.assertEqual(result['links'][0]['child_thread_ids'],['thread:child'])
        self.assertIsNone(result['runs'][0]['expected_children'])
        self.assertIn('child-turn-binding-unknown',result['inventory']['gaps'])

    def test_shared_native_turn_in_conflicting_attempts_cannot_allocate_twice(self):
        s=shape();p=projection(s)
        attempt=copy.deepcopy(p['run_attempts'][0]);attempt['id']='attempt:second';p['run_attempts'].append(attempt)
        turn=copy.deepcopy(p['provider_turns'][0]);turn['id']='provider-turn:second';turn['runAttemptId']=attempt['id'];p['provider_turns'].append(turn)
        session=s['fixture']['attempts'][0]['nativeThreadId'];at=timestamp(s['fixture']['observed_at'])
        result=derive_observed(p,{session:[{'records':native_rows(s),'digest':'a'*64,'gaps':[]}]},projects=[PROJECT],
                               window={'start':at-90*86400,'end':at},as_of=at,acquired_at=at)
        self.assertEqual(len(result['requests']),15)
        self.assertTrue(all(q['conflict'] and q['original_run_id'] is None for q in result['requests']))
        self.assertEqual(result['inventory']['compatible_turns'],0)

    def test_projection_transaction_retains_one_snapshot_across_concurrent_update(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d)
            c=sqlite3.connect(scope['sqlite_path']);c.execute('PRAGMA journal_mode=WAL');c.close()
            calls=0
            def update():
                nonlocal calls
                calls+=1
                if calls==4:
                    c=sqlite3.connect(scope['sqlite_path']);turn=copy.deepcopy(s['fixture']['turns'][0]);turn['turnTokenUsage']['inputTokens']=1
                    c.execute('UPDATE '+PREFIX+'provider_turns SET payload_json=?',(json.dumps(turn),));c.commit();c.close()
            result=read_projection(scope['sqlite_path'],[PROJECT],scope['window'],check=update)
            self.assertGreater(calls,4)
            self.assertEqual(result['provider_turns'][0]['turnTokenUsage']['inputTokens'],4375823)

    def test_fatal_projection_progress_cancellation_is_not_source_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d)
            with patch('collectors.model_evidence.projection.time.monotonic',side_effect=[0,1,31,31,31,31,31]):
                with self.assertRaises(AcquisitionCancelled):read_projection(scope['sqlite_path'],[PROJECT],scope['window'])

    def test_missing_sqlite_cli_keeps_existing_output_and_redacts_error(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d);dest=Path(d)/'output.json';dest.write_bytes(b'LAST-GOOD')
            p=subprocess.run([sys.executable,'-m','collectors.model_evidence.acquire_cli','--sqlite',str(Path(d)/'missing.sqlite'),
                              '--native-root',str(scope['native_roots'][0]),'--project',PROJECT,
                              '--window-start',str(scope['window']['start']),'--as-of',str(at),'--output',str(dest)],
                             capture_output=True,text=True,timeout=5)
            self.assertEqual(p.returncode,2);self.assertNotIn('Traceback',p.stderr);self.assertEqual(dest.read_bytes(),b'LAST-GOOD')

    def test_native_command_execution_startup_completed_is_not_git_or_terminal_proof(self):
        s=shape();session=s['fixture']['attempts'][0]['nativeThreadId'];tid=s['fixture']['turns'][0]['nativeTurnRef']['nativeId']
        s['fixture']['native']['records'].append({'line':614,'timestamp':'2026-10-29T22:28:41.518Z','type':'event_msg',
            'payload':{'type':'item_completed','thread_id':session,'turn_id':tid,
                       'item':{'type':'CommandExecution','id':'exec-synthetic','source':'unified_exec_startup','status':'completed',
                               'exit_code':0,'process_id':'123','command':['PRIVATE-COMMAND'],'aggregated_output':'PRIVATE-OUTPUT'}}})
        out=replay(s)
        self.assertEqual(out['operations'],[]);self.assertIn('unqualified-command-lifecycle',out['inventory']['gaps'])
        self.assertNotIn(b'PRIVATE-',encode(out));self.assertEqual(out['reconciliation'][0]['state'],'match')

    def test_projection_completion_after_frozen_asof_cannot_be_historical_complete(self):
        s=shape();s['fixture']['attempts'][0]['completedAt']='2026-10-30T00:00:00Z'
        s['fixture']['turns'][0]['completedAt']='2026-10-30T00:00:00Z'
        out=replay(s)
        self.assertEqual(out['runs'][0]['status'],'in_progress')
        self.assertIsNone(out['runs'][0]['ended_at'])
        self.assertEqual(out['reconciliation'][0]['state'],'unknown')
        self.assertIn('projection-after-as-of',out['inventory']['gaps'])

    def test_unsupported_projection_objects_are_gaps_not_exact_facts_or_crashes(self):
        for path in (('thread','lineage'),('fixture','run','modelSelection'),('fixture','provider','nativeThreadRef')):
            s=shape();target=s
            for k in path[:-1]:target=target[k]
            target[path[-1]]=[]
            out=replay(s)
            self.assertEqual(out['runs'],[])
            self.assertIn('invalid-projection-record',out['inventory']['gaps'])

    def test_cli_cannot_replace_the_source_database_or_native_file(self):
        with tempfile.TemporaryDirectory() as d:
            s,scope,path,at=make_sources(d)
            for destination in (scope['sqlite_path'],path):
                before=destination.read_bytes()
                p=subprocess.run([sys.executable,'-m','collectors.model_evidence.acquire_cli','--sqlite',str(scope['sqlite_path']),
                    '--native-root',str(scope['native_roots'][0]),'--project',PROJECT,'--window-start',str(scope['window']['start']),
                    '--as-of',str(at),'--output',str(destination)],capture_output=True,text=True,timeout=5)
                self.assertEqual(p.returncode,2);self.assertEqual(destination.read_bytes(),before)
