from collections import defaultdict
from copy import deepcopy
import hashlib
import fcntl
import sys
import os
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import quote
from uuid import uuid4

from .contract import ContractError, digest, encode, identifier, stamp
from .schema import validate
from .owner_lookup_contract import (DATABASE_PATH, MAX_ROWS, MAX_BYTES, SECONDS, KINDS, COLUMNS, FIELDS,
    CLOCKS, REFS, LEASE_FILES, OUTPUT, LookupError, LookupCancelled, accept_selector, accept_inputs, accept_result, bounded, clock)

PREFIX='orchestration_v2_projection_'
TABLES=frozenset(PREFIX+k for k in KINDS)


def _check(callback):
    try:callback()
    except LookupCancelled:raise
    except BaseException:raise LookupCancelled() from None


class _Budget:
    def __init__(self,check):
        self.callback=check;self.started=time.monotonic()
        if not stamp(self.started) or self.started>2**53-1-SECONDS:raise LookupError('clock')
        self.last=self.started;self.deadline=self.started+SECONDS
    def __call__(self):
        _check(self.callback)
        now=time.monotonic()
        if not stamp(now) or now<self.last:raise LookupError('clock')
        self.last=now
        if now>=self.deadline:raise LookupCancelled('deadline')


def _empty_rows():
    return {k:[] for k in KINDS}


def _observations():
    return {'acquisition_started_at':None,'acquisition_completed_at':None,'database_before':None,'database_after':None,
        'transaction':'not-run','rows_seen':0,'row_limit_hit':False,'table_states':{k:'not-read' for k in KINDS},
        'query_gaps':[],'code_leases':[]}


def _relation_issues(kind,row):
    reasons=set(row['issues'])
    for column,field in COLUMNS[kind].items():
        if row['columns'][column] is None or row['payload'][field] is None or row['columns'][column]!=row['payload'][field]:
            reasons.add('identity-column-mismatch')
    return reasons


def derive_owner_lookup(selector,sanitized_rows,acquisition_observations,*,check=lambda:None):
    _check(check)
    selector,rows,observations=accept_inputs(selector,sanitized_rows,acquisition_observations)
    rows={k:sorted(v,key=encode) for k,v in rows.items()}
    gaps=set(observations['query_gaps']);notes=[];references={};groups={};usable={};invalid=0
    def note(k,i,reason):notes.append({'table':k,'ordinal':i,'reason':reason})
    for kind in KINDS:
        _check(check)
        grouped=defaultdict(list);seen=set()
        for ordinal,row in enumerate(rows[kind],1):
            _check(check)
            p=row['payload'];reasons=_relation_issues(kind,row)
            if reasons:invalid+=1
            times=[clock(p[k]) for k in CLOCKS if k in p and p[k] is not None]
            source_at=max(times) if times else None
            ref={'table':kind,'ordinal':ordinal,'source_at':source_at,'row_digest':digest(row)}
            token=encode(row)
            if token in seen:
                note(kind,ordinal,'duplicate-identical')
            else:
                seen.add(token)
                if not reasons:
                    grouped[p['id']].append((row,ref))
            if any(t>selector['as_of'] for t in times):reasons.add('source-after-as-of')
            if p.get('startedAt') is not None and p.get('completedAt') is not None and clock(p['startedAt'])>clock(p['completedAt']):reasons.add('source-clock-interval')
            if 'status' in p and p['status'] not in (None,'preparing','pending','queued','starting','running','waiting','completed','interrupted','failed','cancelled','rolled_back','unknown'):
                reasons.add('unsupported-status')
            required='requestedAt' if kind=='runs' else 'startedAt' if kind in ('run_attempts','provider_turns') else None
            if required and p[required] is None:reasons.add('source-clock-unknown')
            if kind=='runs' and p['requestedAt'] is not None and clock(p['requestedAt'])<selector['window_start']:reasons.add('outside-selection-window')
            for reason in sorted(reasons):note(kind,ordinal,reason)
            gaps.update(reasons)
        groups[kind]=grouped
        usable[kind]={identity:values[0][0]['payload'] for identity,values in grouped.items() if len(values)==1}
        references[kind]={identity:[ref for _,ref in values] for identity,values in grouped.items()}
        for identity,values in grouped.items():
            if len(values)>1:
                gaps.add('conflicting-identity')
                for _,ref in values:note(kind,ref['ordinal'],'conflicting-identity')
    if observations['row_limit_hit'] or observations['rows_seen']>MAX_ROWS:gaps.add('row-limit')
    if observations['rows_seen']>sum(len(v) for v in rows.values()):gaps.add('incomplete-query')
    if any(s!='read' for s in observations['table_states'].values()):gaps.add('incomplete-query')
    runs=usable['runs'];threads=usable['threads'];attempts=usable['run_attempts'];turns=usable['provider_turns'];providers=usable['provider_threads']
    run=runs.get(selector['orchestration_run_id']);thread=threads.get(selector['target_thread_id'])
    if len(runs)!=1 or run is None or run['threadId']!=selector['target_thread_id']:gaps.add('exact-run-unresolved')
    if len(threads)!=1 or thread is None or thread['projectId']!=selector['project_id']:gaps.add('project-membership-unresolved')
    for a in attempts.values():
        if a['runId']!=selector['orchestration_run_id']:gaps.add('attempt-run-mismatch')
    for t in turns.values():
        if t['runAttemptId'] not in attempts:gaps.add('turn-attempt-unresolved')
    referenced={a['providerThreadId'] for a in attempts.values()}|{t['providerThreadId'] for t in turns.values()}
    if run is not None:referenced.add(run['providerThreadId'])
    if any(p not in referenced for p in providers):gaps.add('unreferenced-provider')
    candidates=[]
    for aid,a in sorted(attempts.items()):
        _check(check)
        if a['runId']!=selector['orchestration_run_id']:continue
        matched=[t for t in turns.values() if t['runAttemptId']==aid]
        for t in sorted(matched,key=lambda t:t['id']) or [None]:
            pid=a['providerThreadId'];provider=providers.get(pid);reasons=set()
            if run is None or run['threadId']!=selector['target_thread_id']:reasons.add('exact-run-unresolved')
            if thread is None or thread['projectId']!=selector['project_id']:reasons.add('project-membership-unresolved')
            if t is None:reasons.add('provider-turn-unavailable')
            if provider is None:reasons.add('provider-thread-unresolved')
            if provider is not None and provider['appThreadId']!=selector['target_thread_id']:reasons.add('provider-thread-target-mismatch')
            instances=[r.get('providerInstanceId') for r in (run,a,provider) if r is not None]
            if len(instances)!=3 or None in instances:reasons.add('provider-instance-unknown')
            elif len(set(instances))!=1:reasons.add('provider-instance-conflict')
            if run is not None and run['providerThreadId'] is not None and run['providerThreadId']!=pid:reasons.add('run-provider-reference-mismatch')
            if t is not None and t['providerThreadId']!=pid:reasons.add('turn-provider-mismatch')
            driver=provider['driver'] if provider is not None else None
            if driver!='codex':reasons.add('unsupported-provider')
            session=None;native_turn=None
            ref=provider['nativeThreadRef'] if provider is not None else None
            if driver=='codex' and ref is not None and ref['strength']=='strong' and ref['driver']=='codex' and ref['nativeId'] is not None and ref['nativeId']==a['nativeThreadId']:
                session=a['nativeThreadId']
            else:reasons.add('native-session-unresolved')
            ref=t['nativeTurnRef'] if t is not None else None
            if driver=='codex' and ref is not None and ref['strength']=='strong' and ref['driver']=='codex' and ref['nativeId'] is not None and t['providerThreadId']==pid:
                native_turn=ref['nativeId']
            else:reasons.add('native-turn-unresolved')
            refs=[]
            for kind,identity in [('runs',selector['orchestration_run_id']),('threads',selector['target_thread_id']),('run_attempts',aid),('provider_threads',pid),('provider_turns',t['id'] if t else None)]:
                refs.extend(references[kind].get(identity,[]))
            candidate={'id':'owner-candidate-'+digest([selector['orchestration_run_id'],aid,pid,t['id'] if t else None])[:32],
                'child_thread_id':run['threadId'] if run else None,'orchestration_run_id':selector['orchestration_run_id'],
                'attempt_id':aid,'attempt_run_id':a['runId'],'provider_thread_id':pid,'provider_turn_id':t['id'] if t else None,
                'turn_attempt_id':t['runAttemptId'] if t else None,'native_session_id':session,'turn_session_id':session,
                'native_turn_id':native_turn,'provider_namespace':'codex' if driver=='codex' else None,
                'task_id_from_transport':None,'actor_binding_id':None,'original_source_refs':sorted(refs,key=encode),'reasons':sorted(reasons)}
            candidates.append(candidate);gaps.update(reasons)
    native_groups=defaultdict(list)
    for c in candidates:
        if c['native_turn_id'] is not None:native_groups[c['attempt_id'],c['native_turn_id']].append(c)
    for group in native_groups.values():
        if len(group)>1:
            gaps.add('native-turn-id-conflict')
            for c in group:c['reasons']=sorted(set(c['reasons'])|{'native-turn-id-conflict'})
    if not attempts:gaps.add('attempts-unavailable')
    if not candidates:gaps.add('owner-candidates-unavailable')
    if len(candidates)>1:gaps.add('multiple-owner-candidates')
    if len(candidates)>1:
        for c in candidates:c['reasons']=sorted(set(c['reasons'])|{'multiple-owner-candidates'})
    selected=candidates[0]['id'] if len(candidates)==1 and not gaps and not candidates[0]['reasons'] else None
    gaps.update(('retention-unproven','import-authentication-not-performed','task-actor-binding-unavailable'))
    result={'schema':'flightops-owner-lookup/1','selector':selector,
        'source':{'source_version':'flightops-owner-lookup-source/1','adapter_version':'flightops-owner-relations/1',
            'query_shape_version':'flightops-exact-run-query/1','acquisition_started_at':observations['acquisition_started_at'],
            'acquisition_completed_at':observations['acquisition_completed_at'],'as_of':selector['as_of'],'window_start':selector['window_start'],
            'selected_row_digest':digest(rows),'observations':observations},'rows':rows,'candidates':sorted(candidates,key=lambda c:c['id']),
        'dispositions':sorted(notes,key=lambda n:(n['table'],n['ordinal'],n['reason'])),
        'inventory':{'counts':{k:len(v) for k,v in rows.items()},'unique_counts':{k:len({encode(r) for r in v}) for k,v in rows.items()},
            'rows_seen':observations['rows_seen'],'retained_rows':sum(len(v) for v in rows.values()),'invalid_rows':invalid,
            'truncated':'row-limit' in gaps,'selected_candidate_id':selected},'gaps':sorted(gaps),'complete_through':None,'import_authentication':'not-performed'}
    _check(check)
    validate(result,OUTPUT)
    if len(encode(result))>MAX_BYTES:raise LookupError('byte-limit')
    return result


def _scalar(path,kind='text',limit=160):
    expr="json_extract(payload_json,'$."+path+"')"
    ty="json_type(payload_json,'$."+path+"')"
    valid="length(CAST(payload_json AS BLOB))<="+str(MAX_BYTES)
    return "CASE WHEN "+valid+" THEN CASE WHEN json_valid(payload_json) THEN CASE WHEN "+ty+"='"+kind+"' AND length(CAST("+expr+" AS BLOB))<="+str(limit)+" THEN "+expr+" END END END"


def _query(kind,values,limit):
    if kind not in KINDS:raise LookupError('sqlite-schema')
    expression=["CASE WHEN length(CAST(payload_json AS BLOB))<="+str(MAX_BYTES)+" THEN json_valid(payload_json) ELSE 0 END"]
    paths=list(FIELDS[kind])+[ref+'.'+key for ref in FIELDS[kind] if ref in REFS for key in ('strength','driver','nativeId')]
    duplicates=' OR '.join("json_type(json_remove(payload_json,'$."+path+"'),'$."+path+"') IS NOT NULL" for path in paths)
    expression.append("CASE WHEN length(CAST(payload_json AS BLOB))<="+str(MAX_BYTES)+" THEN CASE WHEN json_valid(payload_json) THEN CASE WHEN "+duplicates+" THEN 0 ELSE 1 END END END")
    for column in COLUMNS[kind]:expression.append("CASE WHEN typeof("+column+")='text' AND length(CAST("+column+" AS BLOB))<=160 THEN "+column+" END")
    for field in FIELDS[kind]:
        if field in REFS:
            expression.append("CASE WHEN length(CAST(payload_json AS BLOB))<="+str(MAX_BYTES)+" THEN CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$."+field+"') END END")
            expression.extend(_scalar(field+'.'+sub) for sub in ('strength','driver','nativeId'))
        elif field in CLOCKS:
            expression.append(_scalar(field,'text',40))
            expression.append("CASE WHEN length(CAST(payload_json AS BLOB))<="+str(MAX_BYTES)+" THEN CASE WHEN json_valid(payload_json) THEN CASE WHEN json_type(payload_json,'$."+field+"') IN ('integer','real') AND json_extract(payload_json,'$."+field+"') BETWEEN 0 AND 9007199254740991 THEN json_extract(payload_json,'$."+field+"') END END END")
            expression.append("CASE WHEN length(CAST(payload_json AS BLOB))<="+str(MAX_BYTES)+" THEN CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$."+field+"') END END")
        else:
            expression.append(_scalar(field))
            expression.append("CASE WHEN length(CAST(payload_json AS BLOB))<="+str(MAX_BYTES)+" THEN CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$."+field+"') END END")
    if kind=='runs':where='run_id=? AND thread_id=?'
    elif kind=='threads':where='thread_id=?'
    else:
        column={'run_attempts':'run_id','provider_turns':'run_attempt_id','provider_threads':'provider_thread_id'}[kind]
        where=column+' IN ('+','.join('?' for _ in values)+')'
    sql='SELECT '+','.join(expression)+' FROM '+PREFIX+kind+' WHERE '+where+' ORDER BY '+','.join(COLUMNS[kind])+' LIMIT ?'
    return sql,(*values,limit)


def _sanitize(kind,raw):
    it=iter(raw);valid=next(it);duplicates_safe=next(it);issues=set() if valid==1 else {'invalid-payload'}
    if duplicates_safe==0:issues.add('duplicate-selected-field')
    def ident(value):
        if type(value) is str and identifier(value):return value
        if value is not None:issues.add('invalid-field')
        return None
    columns={k:ident(next(it)) for k in COLUMNS[kind]};payload={}
    for field in FIELDS[kind]:
        if field in REFS:
            ty=next(it);values={k:ident(next(it)) for k in ('strength','driver','nativeId')}
            payload[field]=values if ty=='object' else None
            if ty not in (None,'null','object'):issues.add('unsupported-reference')
            if ty=='object' and any(v is None for v in values.values()):issues.add('unsupported-reference')
        elif field in CLOCKS:
            text,number,ty=next(it),next(it),next(it)
            value=text if ty=='text' else number if ty in ('integer','real') else None
            try:
                if value is not None:clock(value)
                elif ty not in (None,'null'):issues.add('invalid-field')
            except ContractError:
                value=None;issues.add('invalid-field')
            payload[field]=value
        else:
            value,ty=next(it),next(it);payload[field]=ident(value)
            if ty not in (None,'null','text') or ty=='text' and payload[field] is None:issues.add('invalid-field')
    row={'columns':columns,'payload':payload,'issues':[]};issues.update(_relation_issues(kind,row));row['issues']=sorted(issues)
    return row


def _authorize(action,table,column,database,trigger):
    if action==sqlite3.SQLITE_READ:return sqlite3.SQLITE_OK if table in TABLES or table=='sqlite_master' else sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_FUNCTION:return sqlite3.SQLITE_OK if column in ('json_valid','json_extract','json_type','json_remove','length','typeof') else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_TRANSACTION) else sqlite3.SQLITE_DENY


def _read_connection(connection,selector,guard,observations):
    rows=_empty_rows();cancelled=None;failure=None
    def progress():
        nonlocal cancelled
        try:guard();return 0
        except BaseException as exc:cancelled=exc;return 1
    try:
        guard();connection.execute('PRAGMA query_only=ON');connection.set_authorizer(_authorize)
        connection.set_progress_handler(progress,100);connection.execute('BEGIN')
        found={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ("+','.join('?' for _ in TABLES)+')',sorted(TABLES))}
        if found!=TABLES:raise LookupError('sqlite-schema')
        def select(kind,values):
            if observations['row_limit_hit'] or not values:return
            guard();remaining=MAX_ROWS-sum(len(v) for v in rows.values());sql,args=_query(kind,values,remaining+1)
            cursor=connection.execute(sql,args);observations['table_states'][kind]='read'
            for raw in cursor:
                guard();observations['rows_seen']+=1
                if remaining==0:
                    observations['row_limit_hit']=True;observations['table_states'][kind]='limited';observations['query_gaps'].append('row-limit');break
                row=_sanitize(kind,raw);rows[kind].append(row);remaining-=1
                if row['issues']:observations['query_gaps'].append('invalid-source-row')
                if len(encode(rows))>MAX_BYTES:raise LookupError('byte-limit')
            cursor.close()
        select('runs',(selector['orchestration_run_id'],selector['target_thread_id']))
        select('threads',(selector['target_thread_id'],))
        goodthreads=[r['payload'] for r in rows['threads'] if not _relation_issues('threads',r)]
        goodruns=[r['payload'] for r in rows['runs'] if not _relation_issues('runs',r)]
        exact_run=bool(goodruns) and len(goodruns)==len(rows['runs']) and len({encode(r) for r in goodruns})==1
        exact_run=exact_run and all(r['id']==selector['orchestration_run_id'] and r['threadId']==selector['target_thread_id'] for r in goodruns)
        if not exact_run:
            observations['query_gaps'].append('exact-run-unresolved')
        elif not goodthreads or any(t['projectId']!=selector['project_id'] for t in goodthreads):
            observations['query_gaps'].append('project-membership-unresolved')
        else:
            select('run_attempts',(selector['orchestration_run_id'],))
            attempts=[r['payload'] for r in rows['run_attempts'] if not _relation_issues('run_attempts',r)]
            aids=sorted({a['id'] for a in attempts})
            if aids:select('provider_turns',tuple(aids))
            else:observations['table_states']['provider_turns']='read'
            pids={a['providerThreadId'] for a in attempts if a['providerThreadId'] is not None}
            pids.update(r['payload']['providerThreadId'] for r in rows['runs'] if not _relation_issues('runs',r) and r['payload']['providerThreadId'] is not None)
            pids.update(r['payload']['providerThreadId'] for r in rows['provider_turns'] if not _relation_issues('provider_turns',r) and r['payload']['providerThreadId'] is not None)
            if pids:select('provider_threads',tuple(sorted(pids)))
            else:observations['table_states']['provider_threads']='read'
        guard()
    except sqlite3.Error as exc:
        if cancelled is not None:failure=cancelled
        elif getattr(exc,'sqlite_errorcode',None) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):failure=LookupError('sqlite-busy')
        else:failure=LookupError('sqlite-read')
    except BaseException as exc:failure=exc
    finally:
        for cleanup in (lambda:connection.set_progress_handler(None,0),connection.rollback,connection.close):
            try:cleanup()
            except BaseException as exc:
                if not isinstance(exc,Exception):failure=LookupCancelled()
                elif failure is None:failure=LookupError('sqlite-cleanup')
        observations['transaction']='read-rolled-back'
    if failure is not None:raise failure from None
    observations['query_gaps']=sorted(set(observations['query_gaps']))
    return rows


def _path_identity(path):
    p=Path(path)
    if str(p)!=DATABASE_PATH:raise LookupError('database-path')
    try:
        current=Path('/')
        for part in p.parts[1:]:
            current/=part
            if stat.S_ISLNK(os.lstat(current).st_mode):raise LookupError('database-path')
        info=os.stat(p,follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):raise LookupError('database-path')
        return {'device':info.st_dev,'inode':info.st_ino}
    except OSError:raise LookupError('database-unavailable') from None


class _CodeLease:
    def __init__(self,guard):
        self.entries=[];self.guard=guard
    def __enter__(self):
        try:
            for name in LEASE_FILES:
                self.guard()
                path=Path(__file__).parent/name
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                entry=[path,fd,None,None];self.entries.append(entry)
                before=os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size>2*MAX_BYTES:raise LookupError('code-lease')
                raw=bytearray()
                while True:
                    self.guard()
                    part=os.read(fd,min(65536,2*MAX_BYTES+1-len(raw)))
                    if not part:break
                    raw.extend(part)
                    if len(raw)>2*MAX_BYTES:raise LookupError('code-lease')
                entry[2]=before;entry[3]=hashlib.sha256(raw).hexdigest()
            self.verify()
            return self
        except BaseException as exc:
            self.__exit__(type(exc),exc,None)
            if not isinstance(exc,Exception):raise LookupCancelled() from None
            raise LookupError('code-lease') from None
    def verify(self):
        try:
            for path,fd,before,sha in self.entries:
                held=os.fstat(fd);now=os.stat(path,follow_symlinks=False)
                keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
                if before is None or any(getattr(before,k)!=getattr(held,k) or getattr(before,k)!=getattr(now,k) for k in keys):raise LookupError('code-lease')
        except OSError:raise LookupError('code-lease') from None
    def fields(self):
        return [{'module':p.name,'device':info.st_dev,'inode':info.st_ino,'bytes':info.st_size,'sha256':sha} for p,fd,info,sha in self.entries]
    def __exit__(self,typ,exc,tb):
        failure=None;cancelled=False
        for path,fd,before,sha in self.entries:
            try:os.close(fd)
            except Exception:failure=LookupError('code-lease')
            except BaseException:cancelled=True
        self.entries=[]
        if cancelled:raise LookupCancelled() from None
        if failure and exc is None:raise failure



def _close_fd(fd,active=None):
    try:os.close(fd)
    except Exception:
        if active is None:raise LookupError('publication-failed') from None
    except BaseException:raise LookupCancelled() from None


def _output_directory(path):
    target=Path(path)
    if not target.is_absolute() or not target.is_relative_to('/tmp') or '..' in target.parts or target.name in ('','.','..'):raise LookupError('output-path')
    forbidden=(Path(DATABASE_PATH).parent,Path(os.path.expanduser('~/.codex')),Path(__file__).parent)
    if any(target.is_relative_to(parent) for parent in forbidden):raise LookupError('output-path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in target.parent.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd,target.name
    except BaseException as exc:
        _close_fd(fd,exc)
        if not isinstance(exc,Exception):raise LookupCancelled() from None
        raise LookupError('output-path') from None


def _read_existing(fd,name):
    child=None
    try:
        child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        before=os.fstat(child)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_BYTES:raise LookupError('output-existing')
        raw=bytearray()
        while True:
            part=os.read(child,min(65536,MAX_BYTES+1-len(raw)))
            if not part:break
            raw.extend(part)
            if len(raw)>MAX_BYTES:raise LookupError('output-existing')
        after=os.fstat(child);entry=os.stat(name,dir_fd=fd,follow_symlinks=False)
        keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        if any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(entry,k) for k in keys):raise LookupError('output-existing')
        return bytes(raw),before
    except FileNotFoundError:return None,None
    finally:
        if child is not None:_close_fd(child,sys.exc_info()[1])


def _publish(path,value,guard,lease):
    raw=encode(value)
    if len(raw)>MAX_BYTES:raise LookupError('byte-limit')
    fd=None;child=None;temp=None;replaced=False
    try:
        fd,name=_output_directory(path)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        old,identity=_read_existing(fd,name)
        if old is not None:
            if accept_result(old)['selector']!=value['selector']:raise LookupError('output-existing')
        guard();lease.verify()
        temp='.owner-lookup-'+uuid4().hex
        child=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        view=memoryview(raw)
        while view:
            guard();n=os.write(child,view[:65536])
            if n<=0:raise LookupError('publication-failed')
            view=view[n:]
        os.fsync(child);os.close(child);child=None
        guard();lease.verify()
        check_old,check_identity=_read_existing(fd,name)
        if check_old!=old or (identity is not None and (check_identity.st_dev,check_identity.st_ino)!=(identity.st_dev,identity.st_ino)):raise LookupError('output-existing')
        lease.verify();guard()
        os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd);replaced=True;temp=None
        os.fsync(fd);guard()
    except LookupCancelled:raise
    except (OSError,ContractError):
        raise LookupError('publication-outcome-unknown' if replaced else 'publication-failed') from None
    finally:
        active=sys.exc_info()[1];cancelled=False;failed=False
        actions=[]
        if child is not None:actions.append(lambda:os.close(child))
        if fd is not None:
            if temp is not None:
                def remove_temp():
                    try:os.unlink(temp,dir_fd=fd)
                    except FileNotFoundError:pass
                actions.append(remove_temp)
            actions.append(lambda:os.close(fd))
        for action in actions:
            try:action()
            except Exception:failed=True
            except BaseException:cancelled=True
        if cancelled:raise LookupCancelled() from None
        if failed and active is None:raise LookupError('publication-outcome-unknown' if replaced else 'publication-failed')



def observe_exact_run(selector,explicit_known_database_path,output_path,check=lambda:None):
    selector=accept_selector(selector)
    if str(explicit_known_database_path)!=DATABASE_PATH:raise LookupError('database-path')
    guard=_Budget(check);guard()
    fd=None;published=False
    try:
        fd,name=_output_directory(output_path)
        previous,_=_read_existing(fd,name)
        if previous is not None and accept_result(previous)['selector']!=selector:raise LookupError('output-existing')
        os.close(fd);fd=None
        with _CodeLease(guard) as lease:
            guard();before=_path_identity(explicit_known_database_path)
            observations=_observations();observations['database_before']=before
            started=time.time()
            if not stamp(started) or started<selector['as_of']:raise LookupError('clock')
            observations['acquisition_started_at']=started
            try:
                connection=sqlite3.connect('file:'+quote(DATABASE_PATH,safe='/')+'?mode=ro',uri=True,timeout=0)
            except sqlite3.Error:raise LookupError('database-unavailable') from None
            rows=_read_connection(connection,selector,guard,observations)
            guard();after=_path_identity(explicit_known_database_path)
            if before!=after:raise LookupError('database-replaced')
            observations['database_after']=after
            ended=time.time()
            if not stamp(ended) or ended<started:raise LookupError('clock')
            observations['acquisition_completed_at']=ended
            lease.verify();observations['code_leases']=lease.fields()
            result=derive_owner_lookup(selector,rows,observations,check=guard)
            guard();lease.verify();_publish(output_path,result,guard,lease);published=True
            return result
    except LookupCancelled:raise
    except LookupError:
        if published:raise LookupError('publication-outcome-unknown') from None
        raise
    except (KeyboardInterrupt,SystemExit):raise LookupCancelled() from None
    except Exception:raise LookupError('database-unavailable') from None
    finally:
        if fd is not None:_close_fd(fd,sys.exc_info()[1])
