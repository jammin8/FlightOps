from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from collectors.model_evidence import owner_lookup as lookup
from collectors.model_evidence import owner_lookup_contract as contract
from collectors.model_evidence import owner_lookup_cli as cli
from collectors.model_evidence.contract import encode

FIXTURE=Path(__file__).parent/'fixtures/model-evidence-owner-lookup-synthetic.json'
PRIVATE='SYNTHETIC PRIVATE BODY / command output auth must never escape'


def fixture():return json.loads(FIXTURE.read_bytes())


def derive(b):return lookup.derive_owner_lookup(b['selector'],b['rows'],b['acquisition_observations'])


def counts(b):
    b['acquisition_observations']['rows_seen']=sum(len(r) for r in b['rows'].values())
    return b


class TrackingConnection(sqlite3.Connection):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.rolled=0;self.closed=0;self.query_only=None
    def set_authorizer(self,callback):
        self.query_only=self.execute('PRAGMA query_only').fetchone()[0]
        super().set_authorizer(callback)
    def rollback(self):
        self.rolled+=1;return super().rollback()
    def close(self):
        self.closed+=1;return super().close()


def populate(connection,b,private=True):
    for kind in contract.KINDS:
        connection.execute('CREATE TABLE '+lookup.PREFIX+kind+' ('+','.join(contract.COLUMNS[kind])+',payload_json TEXT)')
        for row in b['rows'][kind]:
            payload=deepcopy(row['payload'])
            if private:payload.update(private=PRIVATE,modelSelection=PRIVATE,lineage=PRIVATE,turnTokenUsage=PRIVATE,cwd=PRIVATE,aggregated_output=PRIVATE)
            connection.execute('INSERT INTO '+lookup.PREFIX+kind+' VALUES ('+','.join('?' for _ in range(len(contract.COLUMNS[kind])+1))+')',(*row['columns'].values(),json.dumps(payload)))
    connection.execute('CREATE TABLE unrelated (private TEXT)')
    connection.commit()
    return connection


class OwnerLookupTests(unittest.TestCase):
    def setUp(self):
        self.b=fixture();self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def read(self,b=None,guard=lambda:None):
        b=self.b if b is None else b
        c=populate(sqlite3.connect(':memory:',factory=TrackingConnection),b)
        obs=lookup._observations();rows=lookup._read_connection(c,b['selector'],guard,obs)
        self.assertEqual((c.rolled,c.closed,c.query_only),(1,1,1))
        return lookup.derive_owner_lookup(b['selector'],rows,obs)

    def observer(self,*,identity=None,check=lambda:None,changes=None):
        database=self.root/'synthetic.sqlite'
        if not database.exists():
            c=sqlite3.connect(database);populate(c,self.b);c.close()
        real=sqlite3.connect;connections=[]
        def connect(database_uri,**kwargs):
            self.assertEqual(database_uri,'file:'+contract.DATABASE_PATH+'?mode=ro')
            self.assertEqual(kwargs,{'uri':True,'timeout':0})
            c=real('file:'+str(database)+'?mode=ro',uri=True,timeout=0,factory=TrackingConnection);connections.append(c);return c
        identity={'device':7,'inode':8} if identity is None else identity
        ident=identity if callable(identity) else lambda _:dict(identity)
        output=self.root/'sidecar.json'
        with patch.object(lookup,'_path_identity',side_effect=ident),patch.object(lookup.sqlite3,'connect',side_effect=connect):
            result=lookup.observe_exact_run(self.b['selector'],contract.DATABASE_PATH,output,check)
        self.assertTrue(all((c.rolled,c.closed,c.query_only)==(1,1,1) for c in connections))
        return result,output

    def test_exact_five_row_strong_positive_pure_and_same_sql(self):
        for out in (derive(self.b),self.read()):
            self.assertEqual(out['inventory']['retained_rows'],5)
            self.assertIsNotNone(out['inventory']['selected_candidate_id'])
            self.assertEqual(len(out['candidates']),1)
            c=out['candidates'][0];self.assertEqual(c['native_session_id'],'session:SYNTHETIC')
            self.assertEqual(c['turn_session_id'],c['native_session_id']);self.assertEqual(c['native_turn_id'],'native-turn:SYNTHETIC')
            self.assertEqual(c['orchestration_run_id'],'run:SYNTHETIC');self.assertEqual(c['attempt_id'],'attempt:SYNTHETIC')
            self.assertIsNone(c['task_id_from_transport']);self.assertIsNone(c['actor_binding_id'])
            self.assertIsNone(out['complete_through']);self.assertEqual(out['import_authentication'],'not-performed')
            self.assertEqual(contract.accept_result(out),out)

    def test_exact_five_canonical_projects_only_no_aliases(self):
        expected={'15422c94-4afe-4fff-b056-f3b240bc63ce','93db29b0-fce2-415c-9c1b-48a687f8302f','175492b5-29c1-4884-9919-3b56a5d0f1c9','851362d8-b65b-4e0e-8781-53c2aadaa12e','mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914'}
        self.assertEqual(set(contract.PROJECTS),expected)
        for project in expected:
            s=deepcopy(self.b['selector']);s['project_id']=project;self.assertEqual(contract.accept_selector(s)['project_id'],project)
        for project in ['scratch15422c94-4afe-4fff-b056-f3b240bc63ce','mcp:15422c94-4afe-4fff-b056-f3b240bc63ce','5cffed67-debf-4e5f-ace3-1d3ca260d914','other:project']:
            s=deepcopy(self.b['selector']);s['project_id']=project
            with self.assertRaises(contract.LookupError):contract.accept_selector(s)

    def test_selector_pinned_clocks_closed_json_types_depth_and_bounds(self):
        for change in ({'as_of':99989},{'window_start':0},{'as_of':True},{'as_of':10**1000},{'qualified':True},{'target_thread_id':None}):
            s={**self.b['selector'],**change}
            with self.assertRaises(contract.LookupError):contract.accept_selector(s)
        for raw in (b'{"schema":1,"schema":2}',b'NaN',b'['*25+b'0'+b']'*25,b' '*1048577):
            with self.assertRaises(contract.LookupError):contract.accept_selector(raw)

    def test_wrong_run_thread_project_and_payload_columns_withhold_selection(self):
        for kind,key,value in [('runs','id','run:other'),('runs','threadId','thread:other'),('threads','projectId','15422c94-4afe-4fff-b056-f3b240bc63ce'),('run_attempts','runId','run:other'),('provider_turns','providerThreadId','provider:other')]:
            b=fixture();b['rows'][kind][0]['payload'][key]=value
            out=self.read(b);self.assertIsNone(out['inventory']['selected_candidate_id']);self.assertTrue(out['rows'][kind]);self.assertTrue(out['dispositions'])

    def test_sql_scope_excludes_other_run_and_thread_without_latest_guess(self):
        b=fixture();other=deepcopy(b['rows']['runs'][0]);other['columns']['run_id']='run:later';other['payload']['id']='run:later'
        b['rows']['runs'].append(other);out=self.read(b)
        self.assertEqual(out['inventory']['counts']['runs'],1);self.assertEqual(out['rows']['runs'][0]['payload']['id'],'run:SYNTHETIC')

    def test_missing_weak_unsupported_and_provider_instance_conflicts(self):
        for mode in ('missing','weak','driver','session','turn','instance','thread'):
            b=fixture();p=b['rows']['provider_threads'][0]['payload'];a=b['rows']['run_attempts'][0]['payload'];t=b['rows']['provider_turns'][0]['payload']
            if mode=='missing':p['nativeThreadRef']=None
            elif mode=='weak':p['nativeThreadRef']['strength']='weak'
            elif mode=='driver':p['driver']='claude'
            elif mode=='session':a['nativeThreadId']='session:other'
            elif mode=='turn':t['nativeTurnRef']['driver']='claude'
            elif mode=='instance':a['providerInstanceId']='codex:other'
            else:p['appThreadId']='thread:other'
            out=self.read(b);self.assertIsNone(out['inventory']['selected_candidate_id']);self.assertEqual(out['inventory']['retained_rows'],5)

    def test_missing_turn_and_attempt_remain_gaps_not_healthy_zero(self):
        for kind in ('provider_turns','run_attempts'):
            b=fixture();b['rows'][kind]=[];out=self.read(b)
            self.assertIsNone(out['inventory']['selected_candidate_id']);self.assertTrue(out['gaps'])

    def test_all_multiple_turn_and_attempt_candidates_retained(self):
        b=fixture();second=deepcopy(b['rows']['provider_turns'][0]);second['columns']['provider_turn_id']='turn:second';second['payload'].update(id='turn:second',nativeTurnRef={'strength':'strong','driver':'codex','nativeId':'native:second'});b['rows']['provider_turns'].append(second)
        out=self.read(b);self.assertEqual(len(out['candidates']),2);self.assertIsNone(out['inventory']['selected_candidate_id'])
        a=deepcopy(b['rows']['run_attempts'][0]);a['columns']['attempt_id']='attempt:second';a['payload']['id']='attempt:second';b['rows']['run_attempts'].append(a)
        second['columns']['run_attempt_id']='attempt:second';second['payload']['runAttemptId']='attempt:second'
        out=self.read(b);self.assertEqual({c['attempt_id'] for c in out['candidates']},{'attempt:SYNTHETIC','attempt:second'});self.assertIsNone(out['inventory']['selected_candidate_id'])

    def test_duplicate_identical_conflicting_and_permutation_replay(self):
        baseline=derive(self.b);b=deepcopy(self.b)
        for kind in contract.KINDS:b['rows'][kind]+=deepcopy(b['rows'][kind])
        out=derive(counts(b));self.assertIsNotNone(out['inventory']['selected_candidate_id']);self.assertEqual(out['candidates'][0]['id'],baseline['candidates'][0]['id'])
        for table in b['rows'].values():table.reverse()
        self.assertEqual(derive(b),out)
        b['rows']['run_attempts'][1]['payload']['nativeThreadId']='session:conflict'
        out=derive(b);self.assertIsNone(out['inventory']['selected_candidate_id']);self.assertIn('conflicting-identity',out['gaps'])

    def test_cap_plus_one_shared_total_retains100_and_withholds_uniqueness(self):
        b=fixture()
        for i in range(110):
            row=deepcopy(b['rows']['provider_turns'][0]);row['columns']['provider_turn_id']='turn:extra-'+str(i);row['payload']['id']=row['columns']['provider_turn_id'];b['rows']['provider_turns'].append(row)
        out=self.read(b);self.assertEqual(out['inventory']['retained_rows'],100);self.assertEqual(out['inventory']['rows_seen'],101)
        self.assertTrue(out['inventory']['truncated']);self.assertIsNone(out['inventory']['selected_candidate_id']);self.assertIn('row-limit',out['gaps'])

    def test_null_future_old_and_original_source_clocks_never_acquisition_substituted(self):
        for value,reason in [(None,'source-clock-unknown'),(100001,'source-after-as-of'),(1,'outside-selection-window')]:
            b=fixture();b['rows']['runs'][0]['payload']['requestedAt']=value;out=self.read(b)
            self.assertEqual(out['rows']['runs'][0]['payload']['requestedAt'],value);self.assertIn(reason,out['gaps']);self.assertIsNone(out['inventory']['selected_candidate_id'])
        out=self.read();refs=out['candidates'][0]['original_source_refs'];self.assertTrue(any(r['source_at'] is None for r in refs));self.assertTrue(any(r['source_at']==99989 for r in refs))

    def test_sql_never_returns_raw_payload_or_private_nested_values(self):
        out=self.read();raw=encode(out);self.assertNotIn(PRIVATE.encode(),raw)
        for kind,values in [('runs',('run:SYNTHETIC','thread:SYNTHETIC')),('threads',('thread:SYNTHETIC',)),('run_attempts',('run:SYNTHETIC',)),('provider_turns',('attempt:SYNTHETIC',)),('provider_threads',('provider:SYNTHETIC',))]:
            sql,args=lookup._query(kind,values,101)
            for forbidden in ('lineage','modelSelection','turnTokenUsage','aggregated_output','cwd','SELECT payload_json','$.private'):
                self.assertNotIn(forbidden,sql)
            self.assertEqual(args[-1],101);self.assertNotIn('latest',sql.lower())

    def test_malformed_nested_fields_and_duplicate_selected_json_withhold(self):
        for payload in ('{"id":"thread:SYNTHETIC","id":"thread:other","projectId":"'+self.b['selector']['project_id']+'"}', '{"id":'+json.dumps(PRIVATE)+'}', '{bad'):
            c=populate(sqlite3.connect(':memory:',factory=TrackingConnection),self.b)
            c.execute('UPDATE '+lookup.PREFIX+'threads SET payload_json=?',(payload,));c.commit();obs=lookup._observations();rows=lookup._read_connection(c,self.b['selector'],lambda:None,obs)
            out=lookup.derive_owner_lookup(self.b['selector'],rows,obs);self.assertIsNone(out['inventory']['selected_candidate_id']);self.assertNotIn(PRIVATE.encode(),encode(out))
        c=populate(sqlite3.connect(':memory:',factory=TrackingConnection),self.b)
        value=deepcopy(self.b['rows']['provider_threads'][0]['payload']);value['nativeThreadRef']['nativeId']={'private':PRIVATE}
        c.execute('UPDATE '+lookup.PREFIX+'provider_threads SET payload_json=?',(json.dumps(value),));c.commit();obs=lookup._observations();rows=lookup._read_connection(c,self.b['selector'],lambda:None,obs)
        self.assertNotIn(PRIVATE.encode(),encode(rows));self.assertIsNone(lookup.derive_owner_lookup(self.b['selector'],rows,obs)['inventory']['selected_candidate_id'])

    def test_authorizer_denies_writes_unrelated_tables_pragmas_and_functions(self):
        c=populate(sqlite3.connect(':memory:'),self.b);self.addCleanup(c.close);c.execute('PRAGMA query_only=ON');c.set_authorizer(lookup._authorize)
        for sql in ('SELECT * FROM unrelated','DELETE FROM '+lookup.PREFIX+'runs','PRAGMA query_only=OFF',"ATTACH ':memory:' AS other",'SELECT random()'):
            with self.assertRaises(sqlite3.Error):c.execute(sql)

    def test_shared_deadline_and_cancellation_rollback_close(self):
        for failure in (contract.LookupCancelled(),contract.LookupCancelled('deadline'),KeyboardInterrupt()):
            c=populate(sqlite3.connect(':memory:',factory=TrackingConnection),self.b)
            def guard():raise failure
            with self.assertRaises(BaseException):lookup._read_connection(c,self.b['selector'],guard,lookup._observations())
            self.assertEqual((c.rolled,c.closed),(1,1))
        times=iter((0,1,5))
        with patch.object(lookup.time,'monotonic',side_effect=lambda:next(times)):
            guard=lookup._Budget(lambda:None);guard()
            with self.assertRaises(contract.LookupCancelled):guard()

    def test_progress_callback_cancellation_is_not_swallowed_by_sqlite(self):
        c=populate(sqlite3.connect(':memory:',factory=TrackingConnection),self.b);called=0
        def guard():
            nonlocal called
            called+=1
            if called>4:raise contract.LookupCancelled()
        with self.assertRaises(contract.LookupCancelled):lookup._read_connection(c,self.b['selector'],guard,lookup._observations())
        self.assertEqual((c.rolled,c.closed),(1,1))

    def test_sqlite_failure_and_missing_schema_rollback_close(self):
        c=sqlite3.connect(':memory:',factory=TrackingConnection)
        with self.assertRaises(contract.LookupError):lookup._read_connection(c,self.b['selector'],lambda:None,lookup._observations())
        self.assertEqual((c.rolled,c.closed),(1,1))

    def test_observer_source_owned_transaction_and_atomic_output(self):
        result,path=self.observer();self.assertEqual(contract.accept_result(path.read_bytes()),result)
        self.assertEqual(len(result['source']['observations']['code_leases']),5)
        self.assertEqual(result['source']['observations']['database_before'],{'device':7,'inode':8})
        self.assertIsNotNone(result['inventory']['selected_candidate_id']);self.assertEqual(result['import_authentication'],'not-performed')

    def test_database_path_replacement_preserves_last_good_and_no_retry(self):
        _,path=self.observer();before=path.read_bytes();calls=[]
        def identity(_):
            calls.append(1);return {'device':7,'inode':8 if len(calls)==1 else 9}
        with self.assertRaisesRegex(contract.LookupError,'database-replaced'):self.observer(identity=identity)
        self.assertEqual(len(calls),2);self.assertEqual(path.read_bytes(),before)

    def test_wal_identity_uses_only_device_inode_not_db_hash_mtime(self):
        fake=types.SimpleNamespace(st_mode=0o100600,st_dev=7,st_ino=8,st_size=999,st_mtime_ns=1)
        with patch.object(lookup.os,'lstat',return_value=fake),patch.object(lookup.os,'stat',return_value=fake):
            a=lookup._path_identity(contract.DATABASE_PATH);fake.st_size=8888;fake.st_mtime_ns=999
            self.assertEqual(lookup._path_identity(contract.DATABASE_PATH),a)
        self.assertEqual(set(a),{'device','inode'})

    def test_invalid_database_and_output_paths_refuse_before_open(self):
        for database in ('/tmp/fake.sqlite',contract.DATABASE_PATH+'/../statev2.sqlite'):
            with patch.object(lookup.sqlite3,'connect') as connect:
                with self.assertRaises(contract.LookupError):lookup.observe_exact_run(self.b['selector'],database,self.root/'out')
                connect.assert_not_called()
        with patch.object(lookup.sqlite3,'connect') as connect:
            with self.assertRaises(contract.LookupError):lookup.observe_exact_run(self.b['selector'],contract.DATABASE_PATH,'/var/tmp/escape')
            connect.assert_not_called()

    def test_last_good_cancel_code_change_and_invalid_existing_never_partially_replace(self):
        _,path=self.observer();before=path.read_bytes()
        with patch.object(lookup._CodeLease,'verify',side_effect=contract.LookupError('code-lease')):
            with self.assertRaises(contract.LookupError):self.observer()
        self.assertEqual(path.read_bytes(),before)
        def cancel():raise contract.LookupCancelled()
        with self.assertRaises(contract.LookupCancelled):self.observer(check=cancel)
        self.assertEqual(path.read_bytes(),before)
        path.write_text(PRIVATE)
        with self.assertRaises(contract.LookupError):self.observer()
        self.assertEqual(path.read_text(),PRIVATE)

    def test_publication_fault_before_replace_preserves_and_after_replace_is_ambiguous(self):
        result,path=self.observer();before=path.read_bytes()
        lease=types.SimpleNamespace(verify=lambda:None)
        with patch.object(lookup.os,'replace',side_effect=OSError(PRIVATE)):
            with self.assertRaises(contract.LookupError) as error:lookup._publish(path,result,lambda:None,lease)
        self.assertNotIn(PRIVATE,str(error.exception));self.assertEqual(path.read_bytes(),before)
        real_replace=lookup.os.replace;replaced=False
        def replace(*args,**kwargs):
            nonlocal replaced
            real_replace(*args,**kwargs);replaced=True
        def cancel():
            if replaced:raise contract.LookupCancelled()
        with patch.object(lookup.os,'replace',side_effect=replace):
            with self.assertRaises(contract.LookupCancelled):lookup._publish(path,result,cancel,lease)
        self.assertEqual(contract.accept_result(path.read_bytes()),result)

    def test_code_leases_detect_replacement_and_close_every_fd(self):
        lease=lookup._CodeLease(lambda:None)
        with lease:
            fields=lease.fields();self.assertEqual(len(fields),5)
            real_stat=lookup.os.stat
            def replaced(path,**kwargs):
                actual=real_stat(path,**kwargs)
                return types.SimpleNamespace(st_dev=actual.st_dev,st_ino=actual.st_ino+1,st_size=actual.st_size,st_mtime_ns=actual.st_mtime_ns,st_ctime_ns=actual.st_ctime_ns)
            with patch.object(lookup.os,'stat',side_effect=replaced):
                with self.assertRaises(contract.LookupError):lease.verify()
        self.assertEqual(lease.entries,[])

    def test_unresolved_exact_run_never_queries_related_attempts_or_turns(self):
        b=fixture();b['selector']['orchestration_run_id']='run:SYNTHETIC-wrong'
        out=self.read(b)
        self.assertEqual(out['inventory']['counts']['run_attempts'],0)
        self.assertEqual(out['source']['observations']['table_states']['run_attempts'],'not-read')
        self.assertIn('exact-run-unresolved',out['gaps'])
        self.assertEqual(out['inventory']['retained_rows'],1)

    def test_sqlite_busy_is_fixed_and_cleanup_closes_without_retry(self):
        path=self.root/'busy.sqlite';writer=sqlite3.connect(path);populate(writer,self.b)
        writer.execute('BEGIN EXCLUSIVE')
        reader=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=0,factory=TrackingConnection)
        try:
            with self.assertRaisesRegex(contract.LookupError,'sqlite-busy'):
                lookup._read_connection(reader,self.b['selector'],lambda:None,lookup._observations())
            self.assertEqual((reader.rolled,reader.closed),(1,1))
        finally:writer.rollback();writer.close()

    def test_cleanup_interrupts_remain_fatal_and_all_cleanup_steps_are_attempted(self):
        class Interrupted(TrackingConnection):
            def rollback(self):
                super().rollback()
                raise contract.LookupCancelled()
        c=populate(sqlite3.connect(':memory:',factory=Interrupted),self.b)
        with self.assertRaises(contract.LookupCancelled):
            lookup._read_connection(c,self.b['selector'],lambda:None,lookup._observations())
        self.assertEqual((c.rolled,c.closed),(1,1))
        lease=lookup._CodeLease(lambda:None);lease.__enter__();real=lookup.os.close;seen=[]
        def interrupted(fd):
            real(fd);seen.append(fd)
            if len(seen)==1:raise contract.LookupCancelled()
        with patch.object(lookup.os,'close',side_effect=interrupted):
            with self.assertRaises(contract.LookupCancelled):lease.__exit__(None,None,None)
        self.assertEqual(len(seen),5);self.assertEqual(lease.entries,[])

    def test_invalid_monotonic_values_and_pure_cancel_cannot_publish(self):
        for value in (True,float('nan'),float('inf'),10**1000):
            with patch.object(lookup.time,'monotonic',return_value=value):
                with self.assertRaises(contract.LookupError):lookup._Budget(lambda:None)
        def cancel():raise contract.LookupCancelled()
        with self.assertRaises(contract.LookupCancelled):
            lookup.derive_owner_lookup(self.b['selector'],self.b['rows'],self.b['acquisition_observations'],check=cancel)

    def test_symlink_hardlink_escape_and_concurrent_writer_refuse_safely(self):
        result,path=self.observer();before=path.read_bytes();lease=types.SimpleNamespace(verify=lambda:None)
        alias=self.root/'symlink.json';alias.symlink_to(path)
        with self.assertRaises(contract.LookupError):lookup._publish(alias,result,lambda:None,lease)
        import os
        hard=self.root/'hard.json';os.link(path,hard)
        with self.assertRaises(contract.LookupError):lookup._publish(hard,result,lambda:None,lease)
        hard.unlink()
        with patch.object(lookup.fcntl,'flock',side_effect=BlockingIOError(PRIVATE)):
            with self.assertRaises(contract.LookupError) as error:lookup._publish(path,result,lambda:None,lease)
        self.assertNotIn(PRIVATE,str(error.exception));self.assertEqual(path.read_bytes(),before)

    def test_unretained_count_and_duplicate_native_turn_ids_cannot_select_unique_owner(self):
        b=fixture();b['acquisition_observations']['rows_seen']=6
        out=derive(b);self.assertIsNone(out['inventory']['selected_candidate_id']);self.assertIn('incomplete-query',out['gaps'])
        b=fixture();second=deepcopy(b['rows']['provider_turns'][0]);second['columns']['provider_turn_id']='turn:second';second['payload']['id']='turn:second';b['rows']['provider_turns'].append(second)
        out=self.read(b);self.assertIn('native-turn-id-conflict',out['gaps']);self.assertEqual(len(out['candidates']),2)

    def test_unsupported_status_and_oversized_payload_remain_safe_observations(self):
        b=fixture();b['rows']['runs'][0]['payload']['status']='unsupported-status'
        out=self.read(b);self.assertIn('unsupported-status',out['gaps']);self.assertIsNone(out['inventory']['selected_candidate_id'])
        c=populate(sqlite3.connect(':memory:',factory=TrackingConnection),self.b)
        c.execute('UPDATE '+lookup.PREFIX+'runs SET payload_json=?',('x'*(contract.MAX_BYTES+1),));c.commit()
        obs=lookup._observations();rows=lookup._read_connection(c,self.b['selector'],lambda:None,obs)
        out=lookup.derive_owner_lookup(self.b['selector'],rows,obs);self.assertIn('invalid-payload',out['gaps']);self.assertIsNone(out['inventory']['selected_candidate_id'])

    def test_scalar_output_refuses_caller_projection_extra_fields_and_no_observer_injection(self):
        b=fixture();b['rows']['runs'][0]['payload']['message']=PRIVATE
        with self.assertRaises(contract.LookupError):derive(b)
        with self.assertRaises(TypeError):lookup.observe_exact_run(self.b['selector'],contract.DATABASE_PATH,self.root/'out',runner=lambda:None)
        out=derive(self.b);out['inventory']['selected_candidate_id']='owner:forged'
        with self.assertRaises(contract.LookupError):contract.accept_result(out)

    def test_pure_replay_cannot_promote_or_tamper_and_total_byte_bound(self):
        out=derive(self.b);out['import_authentication']='qualified'
        with self.assertRaises(contract.LookupError):contract.accept_result(out)
        obs=deepcopy(self.b['acquisition_observations']);obs['observed']=True
        with self.assertRaises(contract.LookupError):lookup.derive_owner_lookup(self.b['selector'],self.b['rows'],obs)
        with patch.object(lookup,'MAX_BYTES',1):
            with self.assertRaises(contract.LookupError):derive(self.b)

    def test_import_constructor_and_cli_path_flags_are_inert_and_safe(self):
        sources=[Path(m.__file__).read_text() for m in (contract,lookup,cli)]
        with patch('builtins.open',side_effect=AssertionError('file')),patch('os.open',side_effect=AssertionError('file')),patch('sqlite3.connect',side_effect=AssertionError('database')),patch('subprocess.Popen',side_effect=AssertionError('process')),patch('socket.socket',side_effect=AssertionError('network')):
            for source in sources:exec(compile(source,'<SYNTHETIC-import>','exec'),{'__name__':'collectors.model_evidence.synthetic_owner_import','__package__':'collectors.model_evidence'})
            lease=lookup._CodeLease(lambda:None);self.assertEqual(lease.entries,[])
        for args in (['--selector','/tmp/unused','--database','/tmp/escape','--output','/tmp/out'],['--qualified',PRIVATE]):
            err=io.StringIO()
            with patch.object(lookup.sqlite3,'connect') as connect,patch.object(sys,'stderr',err):self.assertEqual(cli.main(args),2)
            connect.assert_not_called();self.assertNotIn(PRIVATE,err.getvalue());self.assertNotIn('/tmp/escape',err.getvalue())


if __name__=='__main__':unittest.main()
