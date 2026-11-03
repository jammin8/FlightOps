from copy import deepcopy
from datetime import datetime
import os

from .contract import ContractError, bounded_numeric, decode, encode, require, stamp
from .schema import obj, arr, enum, nullable, IDENT, TIME, COUNT, BOOL, HASH, validate

DATABASE_PATH = os.environ.get('FLIGHTOPS_ORCH_DATABASE') or os.path.expanduser('~/.orch/userdata/statev2.sqlite')
PROJECTS = frozenset(('15422c94-4afe-4fff-b056-f3b240bc63ce', '93db29b0-fce2-415c-9c1b-48a687f8302f',
    '175492b5-29c1-4884-9919-3b56a5d0f1c9', '851362d8-b65b-4e0e-8781-53c2aadaa12e', 'mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914'))
MAX_ROWS = 100
MAX_BYTES = 1048576
SECONDS = 5
KINDS = ('runs', 'threads', 'run_attempts', 'provider_turns', 'provider_threads')
COLUMNS = {'threads': {'thread_id': 'id', 'project_id': 'projectId'},
    'runs': {'run_id': 'id', 'thread_id': 'threadId'},
    'run_attempts': {'attempt_id': 'id', 'run_id': 'runId', 'provider_thread_id': 'providerThreadId'},
    'provider_threads': {'provider_thread_id': 'id', 'thread_id': 'appThreadId'},
    'provider_turns': {'provider_turn_id': 'id', 'run_attempt_id': 'runAttemptId', 'provider_thread_id': 'providerThreadId'}}
FIELDS = {'threads': ('id', 'projectId'),
    'runs': ('id', 'threadId', 'providerThreadId', 'providerInstanceId', 'status', 'requestedAt', 'startedAt', 'completedAt'),
    'run_attempts': ('id', 'runId', 'providerThreadId', 'providerInstanceId', 'nativeThreadId', 'status', 'startedAt', 'completedAt'),
    'provider_threads': ('id', 'appThreadId', 'driver', 'providerInstanceId', 'nativeThreadRef'),
    'provider_turns': ('id', 'providerThreadId', 'runAttemptId', 'nativeTurnRef', 'status', 'startedAt', 'completedAt')}
CLOCKS = frozenset(('requestedAt', 'startedAt', 'completedAt'))
REFS = frozenset(('nativeThreadRef', 'nativeTurnRef'))
ISSUES = ('duplicate-selected-field', 'invalid-payload', 'invalid-field', 'identity-column-mismatch', 'unsupported-reference')
QUERY_GAPS = ('exact-run-unresolved', 'row-limit', 'incomplete-query', 'project-membership-unresolved', 'invalid-source-row')
ERRORS = ('selector', 'json', 'contract', 'database-path', 'database-unavailable', 'database-replaced', 'sqlite-schema',
    'sqlite-busy', 'sqlite-read', 'sqlite-cleanup', 'code-lease', 'clock', 'byte-limit', 'output-path', 'output-existing',
    'publication-failed', 'publication-outcome-unknown', 'cli-arguments', 'selector-file')
LEASE_FILES = ('owner_lookup.py', 'owner_lookup_contract.py', 'owner_lookup_cli.py', 'contract.py', 'schema.py')


class LookupError(ContractError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in ERRORS else 'contract'
        super().__init__('owner-lookup-' + self.code)


class LookupCancelled(BaseException):
    def __init__(self, code='cancelled'):
        super().__init__('owner-lookup-' + ('deadline' if code=='deadline' else 'cancelled'))


def clock(value):
    if stamp(value):
        return value
    require(type(value) is str and len(value)<=40 and value.endswith(('Z', '+00:00')), 'owner-clock')
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError, OSError):
        raise LookupError('clock') from None
    require(stamp(at), 'owner-clock')
    return at


SELECTOR = obj({'schema': enum('flightops-owner-lookup-selector/1'),
    **{k: IDENT for k in ('selection_id', 'target_thread_id', 'orchestration_run_id', 'dispatch_event_id', 'assignment_id', 'recorder_id', 'recorder_epoch')},
    'project_id': enum(*sorted(PROJECTS)), 'dispatch_response_observed_at': TIME, 'as_of': TIME, 'window_start': TIME})
REFERENCE = obj({'strength': nullable(IDENT), 'driver': nullable(IDENT), 'nativeId': nullable(IDENT)})
CLOCK = {'anyOf': [TIME, {'type':'string','maxLength':40}, {'type':'null'}]}
ROW_SPECS = {}
for kind in KINDS:
    payload = {k: nullable(REFERENCE) if k in REFS else CLOCK if k in CLOCKS else nullable(IDENT) for k in FIELDS[kind]}
    ROW_SPECS[kind] = obj({'columns': obj({k: nullable(IDENT) for k in COLUMNS[kind]}),
        'payload': obj(payload), 'issues': arr(enum(*ISSUES), 16)})
ROWS = obj({k: arr(ROW_SPECS[k], MAX_ROWS) for k in KINDS})
IDENTITY = obj({'device': COUNT, 'inode': COUNT})
LEASE = obj({'module': enum(*LEASE_FILES), 'device': COUNT, 'inode': COUNT, 'bytes': COUNT, 'sha256': HASH})
OBSERVATIONS = obj({'acquisition_started_at': nullable(TIME), 'acquisition_completed_at': nullable(TIME),
    'database_before': nullable(IDENTITY), 'database_after': nullable(IDENTITY),
    'transaction': enum('not-run', 'read-rolled-back'), 'rows_seen': {**COUNT, 'maximum': MAX_ROWS+1},
    'row_limit_hit': BOOL, 'table_states': obj({k: enum('not-read', 'read', 'limited') for k in KINDS}),
    'query_gaps': arr(enum(*QUERY_GAPS), 16), 'code_leases': arr(LEASE, len(LEASE_FILES))})
SOURCE_REF = obj({'table': enum(*KINDS), 'ordinal': {**COUNT,'minimum':1}, 'source_at': nullable(TIME), 'row_digest': HASH})
CANDIDATE = obj({'id': IDENT, **{k: nullable(IDENT) for k in ('child_thread_id', 'orchestration_run_id', 'attempt_id', 'attempt_run_id',
    'provider_thread_id', 'provider_turn_id', 'turn_attempt_id', 'native_session_id', 'turn_session_id', 'native_turn_id', 'provider_namespace')},
    'task_id_from_transport': {'type':'null'}, 'actor_binding_id': {'type':'null'}, 'original_source_refs': arr(SOURCE_REF, MAX_ROWS), 'reasons': arr(IDENT)})
OUTPUT = obj({'schema': enum('flightops-owner-lookup/1'), 'selector': SELECTOR,
    'source': obj({'source_version': enum('flightops-owner-lookup-source/1'), 'adapter_version': enum('flightops-owner-relations/1'),
        'query_shape_version': enum('flightops-exact-run-query/1'), 'acquisition_started_at': nullable(TIME),
        'acquisition_completed_at': nullable(TIME), 'as_of': TIME, 'window_start': TIME,
        'selected_row_digest': HASH, 'observations': OBSERVATIONS}), 'rows': ROWS, 'candidates': arr(CANDIDATE, MAX_ROWS),
    'dispositions': arr(obj({'table': enum(*KINDS), 'ordinal': {**COUNT,'minimum':1}, 'reason': IDENT}), 1000),
    'inventory': obj({'counts': obj({k: COUNT for k in KINDS}), 'unique_counts': obj({k: COUNT for k in KINDS}),
        'rows_seen': COUNT, 'retained_rows': COUNT, 'invalid_rows': COUNT, 'truncated': BOOL,
        'selected_candidate_id': nullable(IDENT)}), 'gaps': arr(IDENT, 256),
    'complete_through': {'type':'null'}, 'import_authentication': enum('not-performed')})


def bounded(value):
    try:
        raw = value if type(value) is bytes else encode(value)
        require(len(raw)<=MAX_BYTES, 'owner-bytes')
        quoted = escaped = False
        depth = 0
        for byte in raw:
            if quoted:
                if escaped: escaped=False
                elif byte==92: escaped=True
                elif byte==34: quoted=False
            elif byte==34: quoted=True
            elif byte in (91,123):
                depth+=1;require(depth<=24, 'owner-depth')
            elif byte in (93,125): depth-=1
        return decode(raw)
    except (ValueError, TypeError, RecursionError):
        raise LookupError('json') from None


def accept_selector(value):
    try:
        value=bounded(value);validate(value,SELECTOR)
        require(value['as_of']>=86400 and value['window_start']==value['as_of']-86400
            and value['as_of']>=value['dispatch_response_observed_at'], 'owner-selector-clock')
        return value
    except ContractError:
        raise LookupError('selector') from None


def accept_inputs(selector, rows, observations):
    selector=accept_selector(selector)
    try:
        value=bounded({'rows':rows,'observations':observations})
        validate(value,obj({'rows':ROWS,'observations':OBSERVATIONS}))
        rows,observations=value['rows'],value['observations']
        count=sum(len(r) for r in rows.values())
        require(count<=MAX_ROWS and count<=observations['rows_seen']<=MAX_ROWS+1,'owner-row-limit')
        a,b=observations['acquisition_started_at'],observations['acquisition_completed_at']
        require(a is None or b is None or a<=b,'owner-acquisition-clock')
        for table in rows.values():
            for row in table:
                for field,v in row['payload'].items():
                    if field in CLOCKS and v is not None:clock(v)
        return selector,rows,observations
    except ContractError:
        raise LookupError('contract') from None


def accept_result(value):
    try:
        value=bounded(value);validate(value,OUTPUT)
        from .owner_lookup import derive_owner_lookup
        require(value==derive_owner_lookup(value['selector'],value['rows'],value['source']['observations']), 'owner-derived')
        return deepcopy(value)
    except ContractError:
        raise LookupError('contract') from None
