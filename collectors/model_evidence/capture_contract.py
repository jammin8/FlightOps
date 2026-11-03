from copy import deepcopy
from dataclasses import asdict,dataclass

from .contract import ContractError,decode,encode,digest,require
from .schema import obj,arr,enum,nullable,IDENT,HASH,FULL_SHA,COUNT,TIME,BOOL,validate

EVENT_BYTES=16384
APPEND_BYTES=65536
REPLAY_BYTES=4*1024*1024
SEGMENT_BYTES=4*1024*1024
MAX_EVENTS=5000
LEDGER_GAPS=('manifest-unavailable','identity-custody-mismatch','identity-custody-unavailable','segment-chain-gap','segment-unavailable',
    'replay-byte-limit','replay-event-limit','invalid-event','partial-tail','segment-digest-mismatch','segment-count-mismatch',
    'segment-range-mismatch','event-epoch-mismatch','event-chain-gap','manifest-chain-gap','manifest-clock-mismatch',
    'ledger-entry-limit','unsafe-ledger-entry','unreferenced-ledger-entry','manifest-advanced','manifest-unavailable-after-read')
CHECKPOINT_GAPS=('pending-receipt','missing-children','retention-hole','unsupported-port','shared-allocation-unknown','inventory-unavailable','observation-failed','sequence-hole')
REPOSITORY={'type':'string','maxLength':160,'pattern':'[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*'}
REF={'type':'string','maxLength':160,'pattern':'refs/heads/[A-Za-z0-9][A-Za-z0-9_./-]*'}
EXIT={'type':'integer','minimum':-2147483648,'maximum':2147483647}
SOURCE_REF=obj({'artifact_id':IDENT,'digest':HASH,'ordinal':{**COUNT,'minimum':1},'source_at':nullable(TIME)})
REFS=arr(SOURCE_REF,64)
MEMBERSHIPS=arr(obj({'repository':REPOSITORY,'pr':{**COUNT,'minimum':1},'sha':nullable(FULL_SHA)}),64)
PROCESS={'anyOf':[{'type':'null'},obj({'kind':enum('transport-session'),'protocol':enum('unified-exec'),'session_id':{**COUNT,'minimum':1}}),
    obj({'kind':enum('owned-process'),'recorder_epoch':IDENT,'invocation_id':IDENT,'pid':{**COUNT,'minimum':1},'spawn_token':IDENT})]}

PAYLOADS={
 'assignment_intent':obj({'task_id':IDENT,'target_thread_id':IDENT,'revision':COUNT,'dispatcher_binding_id':IDENT,'role':enum('implementation','review','rework'),
    'task_kind':enum('implementation','bug-fix','review','integration','maintenance'),'repository':REPOSITORY,'pr':nullable({**COUNT,'minimum':1}),
    'input_sha':nullable(FULL_SHA),'reviewed_sha':nullable(FULL_SHA),'replaced':nullable(BOOL),'memberships':MEMBERSHIPS,
    'actor_binding_id':nullable(IDENT),'trigger_kind':enum('initial','ci-failure','review'),'trigger_id':nullable(IDENT),
    'supersedes_assignment_id':nullable(IDENT),'requested_owner_binding_id':nullable(IDENT)}),
 'dispatch_observed':obj({'transport_kind':enum('guarded-orch-thread-send'),'client_request_id':nullable(IDENT),'task_id_from_transport':nullable(IDENT),
    'child_thread_id':IDENT,'message_id':IDENT,'orchestration_run_id':nullable(IDENT),
    'delivery_state':enum('started','queued','steered','restarted'),'status':enum('preparing','queued','starting','running','waiting','completed','interrupted','failed','cancelled','rolled_back'),
    'request_observed_at':TIME,'response_observed_at':TIME}),
 'owner_bound':obj({'task_id_from_transport':nullable(IDENT),'child_thread_id':IDENT,'orchestration_run_id':IDENT,'attempt_id':IDENT,
    'attempt_run_id':IDENT,'turn_attempt_id':IDENT,'turn_session_id':IDENT,
    'provider_namespace':enum('codex'),'native_session_id':IDENT,'native_turn_id':IDENT,'actor_binding_id':nullable(IDENT),'original_source_refs':REFS}),
 'execution_started':obj({'operation_kind':enum('git-create','git-publish','inspect','unsupported'),'protocol':enum('unified-exec','startup-ack','unsupported'),
    'recorder_invocation_id':nullable(IDENT),'native_invocation_id':nullable(IDENT),'process_identity':PROCESS,
    'repository_binding_id':nullable(IDENT),'repository':nullable(REPOSITORY),'intended_object_sha':nullable(FULL_SHA),
    'before_sha':nullable(FULL_SHA),'tree_sha':nullable(FULL_SHA),'parent_shas':arr(FULL_SHA,8),
    'destination_repository':nullable(REPOSITORY),'intended_destination_ref':nullable(REF),'started_at':nullable(TIME)}),
 'execution_result':obj({'recorder_invocation_id':IDENT,'native_completion_id':nullable(IDENT),'transport_session_id':nullable({**COUNT,'minimum':1}),
    'protocol':enum('unified-exec','startup-ack','unsupported'),
    'process_identity':PROCESS,'terminal_state':enum('in_progress','success','failure','cancelled','unknown'),'exit_code':nullable(EXIT),
    'signal':nullable({**COUNT,'minimum':1,'maximum':128}),'completed_at':nullable(TIME),'result_source_refs':REFS}),
 'git_result':obj({'repository_binding_id':IDENT,'repository':REPOSITORY,'witness_kind':enum('object-construction','ref-publication','observation-only','unsupported'),
    'object_format':enum('sha1','unsupported'),'before_sha':nullable(FULL_SHA),'result_sha':nullable(FULL_SHA),'tree_sha':nullable(FULL_SHA),'parent_shas':arr(FULL_SHA,8),
    'destination_repository':nullable(REPOSITORY),'destination_ref':nullable(REF),'ref_before_sha':nullable(FULL_SHA),'ref_result_sha':nullable(FULL_SHA),
    'object_existed_before':nullable(BOOL),'dry_run':BOOL,'outcome':enum('created','reused','no-op','published','partial','failed','unsupported','observed'),
    'result_source_refs':REFS}),
 'assignment_resolution':obj({'revision':COUNT,'terminal_run_id':nullable(IDENT),'worker_terminal_state':enum('completed','failed','cancelled','in_progress','unknown'),
    'worker_started_at':nullable(TIME),'worker_completed_at':nullable(TIME),'result_repository':nullable(REPOSITORY),'result_pr':nullable({**COUNT,'minimum':1}),
    'result_sha':nullable(FULL_SHA),'reviewed_sha':nullable(FULL_SHA),
    'review_outcome':nullable(enum('accepted','changes_requested')),'resolution':enum('open','completed','failed','cancelled','unknown'),
    'trigger_id':nullable(IDENT),'resolved_at':nullable(TIME),'observed_at':TIME,'result_source_refs':REFS}),
 'coverage_checkpoint':obj({'scope_id':IDENT,'prior_checkpoint_id':nullable(IDENT),'first_sequence':{**COUNT,'minimum':1},'last_sequence':COUNT,
    'interval_start':TIME,'interval_end':TIME,'pending_assignment_ids':arr(IDENT,64),'pending_operation_ids':arr(IDENT,64),
    'explicit_gaps':arr(enum(*CHECKPOINT_GAPS),64),'expected_inventory_digest':nullable(HASH)})}

COMMON={'schema':enum('flightops-boundary-event/1'),'event_id':IDENT,'recorder_id':IDENT,'recorder_epoch':IDENT,
    'sequence':{**COUNT,'minimum':1},'previous_event_digest':nullable(HASH),'kind':enum(*PAYLOADS),'recorded_at':TIME,
    'effective_at':nullable(TIME),'boundary_id':IDENT,'assignment_id':nullable(IDENT),'operation_id':nullable(IDENT),
    'owner_binding_id':nullable(IDENT),'dependencies':arr(IDENT,64),'evidence_refs':REFS}
SEGMENT=obj({'name':{'type':'string','pattern':'seg-[0-9a-f]{64}\\.jsonl'},'digest':HASH,'previous_segment_digest':nullable(HASH),
    'first_sequence':{**COUNT,'minimum':1},'last_sequence':{**COUNT,'minimum':1},'event_count':{**COUNT,'minimum':1,'maximum':MAX_EVENTS}})
MANIFEST=obj({'schema':enum('flightops-boundary-ledger/1'),'recorder_id':IDENT,'recorder_epoch':IDENT,'segments':arr(SEGMENT,MAX_EVENTS),
    'last_sequence':COUNT,'last_event_digest':nullable(HASH),'last_source_at':nullable(TIME)})


class CaptureCancelled(BaseException):
    pass


@dataclass(frozen=True)
class CaptureDisposition:
    identity:str
    reason:str

    def fields(self):
        value=asdict(self)
        validate(value,obj({'identity':IDENT,'reason':enum('event-id-conflict','assignment-intent-missing-or-conflicting','event-dependency-missing-or-cyclic')}))
        return value


@dataclass(frozen=True)
class LedgerSnapshot:
    manifest:dict|None
    events:list
    gaps:list
    as_of:float|None

    def fields(self):
        validate(self.gaps,arr(enum(*LEDGER_GAPS),64));validate(self.as_of,nullable(TIME))
        if self.manifest is not None:manifest(self.manifest)
        require(type(self.events) is list and len(self.events)<=MAX_EVENTS,'capture-snapshot-count')
        rows=[];used=0
        for value in self.events:
            row=event(value);used+=len(encode(row));require(used<=REPLAY_BYTES,'capture-snapshot-size');rows.append(row)
        require(self.as_of==max((r['recorded_at'] for r in rows),default=None),'capture-snapshot-clock')
        return {'schema':'flightops-boundary-snapshot/1','manifest':deepcopy(self.manifest),'events':rows,
            'gaps':list(self.gaps),'as_of':self.as_of,'custody':'supplied-replay'}


def bounded_json(raw,limit):
    require(type(raw) is bytes and len(raw)<=limit,'capture-byte-limit')
    depth=0;quoted=False;escaped=False
    for byte in raw:
        if quoted:
            if escaped:escaped=False
            elif byte==92:escaped=True
            elif byte==34:quoted=False
        elif byte==34:quoted=True
        elif byte in (91,123):
            depth+=1;require(depth<=32,'capture-json-depth')
        elif byte in (93,125):depth-=1
    try:return decode(raw)
    except (ContractError,RecursionError):raise ContractError('capture-invalid-json') from None


def payload(kind,value):
    require(type(kind) is str and kind in PAYLOADS,'capture-kind')
    validate(value,PAYLOADS[kind])
    def walk(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k in ('destination_ref','intended_destination_ref') and x is not None:
                    require(not any(t in x for t in ('..','//','@{')) and not x.endswith(('/','.','.lock')),'capture-ref')
                walk(x)
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(value)
    if kind=='dispatch_observed':require(value['request_observed_at']<=value['response_observed_at'],'capture-dispatch-clock')
    if kind=='coverage_checkpoint':
        require(value['interval_start']<=value['interval_end'] and value['first_sequence']<=value['last_sequence'],'capture-coverage-clock')
    return deepcopy(value)


def event(value):
    require(type(value) is dict and type(value.get('kind')) is str and value['kind'] in PAYLOADS,'capture-event')
    validate(value,obj({**COMMON,'payload':PAYLOADS[value['kind']]}))
    payload(value['kind'],value['payload'])
    require(len(encode(value))<=EVENT_BYTES,'capture-event-size')
    require(value['effective_at'] is None or value['effective_at']<=value['recorded_at'],'capture-event-clock')
    for r in value['evidence_refs']:
        require(r['source_at'] is None or r['source_at']<=value['recorded_at'],'capture-source-clock')
    times={'source_at','started_at','completed_at','request_observed_at','response_observed_at','observed_at','worker_started_at','worker_completed_at','resolved_at','interval_start','interval_end'}
    def clocks(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k in times:require(x is None or x<=value['recorded_at'],'capture-source-clock')
                clocks(x)
        elif isinstance(v,list):
            for x in v:clocks(x)
    clocks(value['payload'])
    assignment=value['kind'] in ('assignment_intent','dispatch_observed','owner_bound','assignment_resolution')
    operation=value['kind'] in ('execution_started','execution_result','git_result')
    require(not assignment or value['assignment_id'] is not None,'capture-assignment-id')
    require(not operation or value['operation_id'] is not None,'capture-operation-id')
    if value['kind']=='execution_started':require(value['payload']['recorder_invocation_id'] is not None,'capture-invocation-id')
    return deepcopy(value)


def manifest(value):
    validate(value,MANIFEST)
    require(len(encode(value))<=REPLAY_BYTES,'capture-manifest-size')
    require(len({s['name'] for s in value['segments']})==len(value['segments']),'capture-segment-duplicate')
    require(all(s['name']=='seg-'+s['digest']+'.jsonl' and s['first_sequence']<=s['last_sequence'] for s in value['segments']),'capture-segment-identity')
    return deepcopy(value)


class TypedRecord:
    def __post_init__(self):
        payload(self.kind,asdict(self))

    def fields(self):
        return payload(self.kind,asdict(self))


@dataclass(frozen=True)
class AssignmentIntent(TypedRecord):
    kind='assignment_intent'
    task_id:str;target_thread_id:str;revision:int;dispatcher_binding_id:str;role:str;task_kind:str;repository:str;pr:int|None
    input_sha:str|None;reviewed_sha:str|None;replaced:bool|None;memberships:list
    actor_binding_id:str|None;trigger_kind:str;trigger_id:str|None;supersedes_assignment_id:str|None;requested_owner_binding_id:str|None


@dataclass(frozen=True)
class DispatchObservation(TypedRecord):
    kind='dispatch_observed'
    transport_kind:str;client_request_id:str|None;task_id_from_transport:str|None;child_thread_id:str;message_id:str
    orchestration_run_id:str|None;delivery_state:str;status:str;request_observed_at:float;response_observed_at:float


@dataclass(frozen=True)
class OwnerBindingObservation(TypedRecord):
    kind='owner_bound'
    task_id_from_transport:str|None;child_thread_id:str;orchestration_run_id:str;attempt_id:str;attempt_run_id:str;turn_attempt_id:str;turn_session_id:str;provider_namespace:str
    native_session_id:str;native_turn_id:str;actor_binding_id:str|None;original_source_refs:list


@dataclass(frozen=True)
class ExecutionStart(TypedRecord):
    kind='execution_started'
    operation_kind:str;protocol:str;recorder_invocation_id:str|None;native_invocation_id:str|None;process_identity:dict|None
    repository_binding_id:str|None;repository:str|None;intended_object_sha:str|None;before_sha:str|None;tree_sha:str|None
    parent_shas:list;destination_repository:str|None;intended_destination_ref:str|None;started_at:float|None


@dataclass(frozen=True)
class ExecutionResult(TypedRecord):
    kind='execution_result'
    recorder_invocation_id:str;native_completion_id:str|None;transport_session_id:int|None;process_identity:dict|None
    terminal_state:str;exit_code:int|None;signal:int|None;completed_at:float|None;result_source_refs:list
    protocol:str


@dataclass(frozen=True)
class GitResult(TypedRecord):
    kind='git_result'
    repository_binding_id:str;repository:str;witness_kind:str;object_format:str;before_sha:str|None;result_sha:str|None;tree_sha:str|None
    parent_shas:list;destination_repository:str|None;destination_ref:str|None;ref_before_sha:str|None;ref_result_sha:str|None
    object_existed_before:bool|None;dry_run:bool;outcome:str;result_source_refs:list


@dataclass(frozen=True)
class AssignmentResolution(TypedRecord):
    kind='assignment_resolution'
    revision:int;terminal_run_id:str|None;worker_terminal_state:str;worker_started_at:float|None;worker_completed_at:float|None
    result_repository:str|None;result_pr:int|None;result_sha:str|None;reviewed_sha:str|None;review_outcome:str|None;resolution:str;trigger_id:str|None;resolved_at:float|None
    observed_at:float;result_source_refs:list


@dataclass(frozen=True)
class CoverageCheckpoint(TypedRecord):
    kind='coverage_checkpoint'
    scope_id:str;prior_checkpoint_id:str|None;first_sequence:int;last_sequence:int;interval_start:float;interval_end:float
    pending_assignment_ids:list;pending_operation_ids:list;explicit_gaps:list;expected_inventory_digest:str|None
