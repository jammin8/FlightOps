from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote
import time

from .contract import ContractError, digest, identifier, require, stamp

PREFIX = 'orchestration_v2_projection_'
TABLES = {PREFIX + k for k in ('threads', 'runs', 'run_attempts', 'provider_threads', 'provider_turns')}
FIELDS = {
    'threads': ('id', 'projectId', 'lineage', 'createdAt', 'updatedAt'),
    'runs': ('id', 'threadId', 'ordinal', 'providerInstanceId', 'providerThreadId', 'modelSelection', 'status', 'requestedAt', 'startedAt', 'completedAt'),
    'run_attempts': ('id', 'runId', 'attemptOrdinal', 'nativeThreadId', 'providerInstanceId', 'providerThreadId', 'status', 'startedAt', 'completedAt'),
    'provider_threads': ('id', 'appThreadId', 'driver', 'providerInstanceId', 'nativeThreadRef', 'forkedFrom'),
    'provider_turns': ('id', 'providerThreadId', 'runAttemptId', 'nativeTurnRef', 'status', 'startedAt', 'completedAt', 'turnTokenUsage'),
}


class AcquisitionCancelled(Exception):
    pass


def connection(path, timeout_s):
    require(Path(path).is_absolute(), 'sqlite-path')
    c = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True, timeout=timeout_s)
    c.execute('PRAGMA query_only=ON')
    def authorize(action, table, column, db, trigger):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if table in TABLES or table == 'sqlite_master' else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION) else sqlite3.SQLITE_DENY
    c.set_authorizer(authorize)
    return c


def read_projection(path, projects, window, *, max_rows=500, max_bytes=4*1024*1024, timeout_s=5, check=lambda: None):
    require(isinstance(projects, (list, tuple)) and 0 < len(projects) <= 5 and all(identifier(p) for p in projects), 'project-scope')
    start, end = window['start'], window['end']
    require(stamp(start) and stamp(end) and start <= end and end-start <= 90*86400, 'window')
    require(type(max_rows) is int and 1 <= max_rows <= 500 and type(max_bytes) is int and 1 <= max_bytes <= 4*1024*1024, 'projection-limits')
    require(type(timeout_s) in (int, float) and 0 < timeout_s <= 30, 'projection-timeout')
    deadline = time.monotonic() + timeout_s
    def guard():
        check()
        if time.monotonic() >= deadline:
            raise AcquisitionCancelled()
    cancellation = None
    def progress():
        nonlocal cancellation
        try:
            guard()
            return 0
        except BaseException as exc:
            cancellation = exc
            return 1
    try:
        c = connection(path, timeout_s)
    except sqlite3.Error as exc:
        raise ContractError('projection-unavailable') from exc
    result = {k: [] for k in FIELDS}
    gaps, used = set(), 0
    try:
        c.set_progress_handler(progress, 1000)
        c.execute('BEGIN')
        found = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN (" + ','.join('?' for _ in TABLES) + ')', sorted(TABLES))}
        require(found == TABLES, 'projection-schema')
        def select(kind, where, args):
            nonlocal used
            guard()
            expr = ','.join("CASE WHEN json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<="+str(max_bytes)+" THEN json_extract(payload_json,'$."+f+"') ELSE NULL END" for f in FIELDS[kind])
            identity_column={'threads':'thread_id','runs':'run_id','run_attempts':'attempt_id','provider_threads':'provider_thread_id','provider_turns':'provider_turn_id'}[kind]
            relations={'threads':{'project_id':'projectId'},'runs':{'thread_id':'threadId'},
                       'run_attempts':{'run_id':'runId','provider_thread_id':'providerThreadId'},
                       'provider_threads':{'thread_id':'appThreadId'},
                       'provider_turns':{'run_attempt_id':'runAttemptId','provider_thread_id':'providerThreadId'}}[kind]
            columns=','.join([identity_column,*relations])
            cursor = c.execute('SELECT ' + columns + ',' + expr + ' FROM ' + PREFIX + kind + ' WHERE ' + where + ' ORDER BY '+identity_column+' LIMIT ?', (*args, max_rows+1))
            records = []
            for index, row in enumerate(cursor):
                guard()
                if index == max_rows:
                    gaps.add('projection-row-limit'); break
                item = dict(zip(FIELDS[kind], row[1+len(relations):]))
                if row[0]!=item.get('id') or any(row[i+1]!=item.get(field) for i,field in enumerate(relations.values())):
                    gaps.add('projection-identity-mismatch'); continue
                try:
                    for field in ('lineage', 'modelSelection', 'nativeThreadRef', 'nativeTurnRef', 'forkedFrom', 'turnTokenUsage'):
                        if field in item and item[field] is not None:
                            item[field] = json.loads(item[field])
                            if not isinstance(item[field],dict):raise ValueError('unsupported metadata object')
                except (ValueError,TypeError):
                    gaps.add('invalid-projection-record'); continue
                used += len(json.dumps(item).encode())
                require(used <= max_bytes, 'projection-byte-limit')
                records.append(item)
            return records
        marks = ','.join('?' for _ in projects)
        project_where = 'thread_id IN (SELECT thread_id FROM '+PREFIX+'threads WHERE project_id IN ('+marks+'))'
        def iso(t):
            try:
                return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
            except (ValueError,OverflowError,OSError) as exc:
                raise ContractError('calendar-window') from exc
        result['runs'] = select('runs', project_where+' AND requested_at <= ? AND (completed_at IS NULL OR completed_at >= ?)', (*projects, iso(end), iso(start)))
        tids = sorted({r['threadId'] for r in result['runs']})
        if tids:
            tm = ','.join('?' for _ in tids)
            result['threads'] = select('threads', 'project_id IN ('+marks+') AND thread_id IN ('+tm+')', (*projects,*tids))
            lineage_parents = sorted({t.get('lineage',{}).get('parentThreadId') for t in result['threads'] if isinstance(t.get('lineage'),dict) and t['lineage'].get('parentThreadId')})
            children = select('threads', 'project_id IN ('+marks+") AND json_extract(payload_json,'$.lineage.parentThreadId') IN ("+tm+')', (*projects,*tids))
            parents = []
            if lineage_parents:
                lm = ','.join('?' for _ in lineage_parents)
                parents = select('threads', 'project_id IN ('+marks+') AND thread_id IN ('+lm+')', (*projects,*lineage_parents))
            by_id = {t['id']:t for t in result['threads']+children+parents}
            if len(by_id)>max_rows: gaps.add('projection-row-limit')
            result['threads'] = list(by_id.values())[:max_rows]
            rids = [r['id'] for r in result['runs']]
            rm = ','.join('?' for _ in rids)
            result['run_attempts'] = select('run_attempts', 'run_id IN ('+rm+')', tuple(rids))
            aids = [a['id'] for a in result['run_attempts']]
            if aids:
                am = ','.join('?' for _ in aids)
                result['provider_turns'] = select('provider_turns', 'run_attempt_id IN ('+am+')', tuple(aids))
            pids = sorted({a['providerThreadId'] for a in result['run_attempts'] if a['providerThreadId']})
            if pids:
                pm = ','.join('?' for _ in pids)
                result['provider_threads'] = select('provider_threads', 'provider_thread_id IN ('+pm+')', tuple(pids))
        guard()
    except sqlite3.Error as exc:
        if cancellation is not None:
            raise cancellation from exc
        if time.monotonic() >= deadline:
            raise AcquisitionCancelled() from exc
        check()
        raise ContractError('projection-read-error') from exc
    finally:
        c.set_progress_handler(None, 0)
        c.rollback(); c.close()
    return {**result, 'gaps': sorted(gaps | {'projection-retention-unproven'}), 'digest': digest(result)}
