from collections import deque
from copy import deepcopy
from dataclasses import dataclass,replace
import time
from uuid import uuid4

from .contract import ContractError,digest,require,stamp
from .capture_contract import AssignmentIntent,DispatchObservation,OwnerBindingObservation,ExecutionStart,ExecutionResult,GitResult,AssignmentResolution,CoverageCheckpoint,event
from .capture_projection import project


class _Handle:
    __slots__=('recorder','identity','assignment','invocation','attempted','session','last_result','protocol')

    def __init__(self,recorder,identity,assignment=None,invocation=None):
        self.recorder=recorder;self.identity=identity;self.assignment=assignment;self.invocation=invocation
        self.attempted=False;self.session=None;self.last_result=None
        self.protocol=None


class _Receipt:
    __slots__=('port','handle','record')

    def __init__(self,port,handle,record):
        self.port=port;self.handle=handle;self.record=record


@dataclass(frozen=True)
class ExecReply:
    protocol:str
    session_id:int|None
    exit_code:int|None
    signal:int|None
    source_at:float|None
    native_completion_id:str|None
    source_refs:list

    def __post_init__(self):
        require(self.protocol in ('unified-exec','startup-ack','unsupported'),'capture-exec-protocol')
        ExecutionResult('validation:invocation',self.native_completion_id,self.session_id,None,'unknown',self.exit_code,self.signal,self.source_at,self.source_refs,self.protocol)


class BoundaryPort:
    def dispatch(self,descriptor,handle):
        raise ContractError('capture-dispatch-port-unavailable')

    def receipt(self,handle,record):
        require(isinstance(record,(DispatchObservation,OwnerBindingObservation,ExecutionResult,GitResult,AssignmentResolution)),'capture-port-record')
        record.fields()
        return _Receipt(self,handle,record)


class FixturePort(BoundaryPort):
    def __init__(self,dispatches=()):
        require(all(type(d) is DispatchObservation for d in dispatches),'capture-fixture-dispatch-type')
        self.dispatches=deque(dispatches);self.dispatch_calls=0;self.descriptors=[]

    def dispatch(self,descriptor,handle):
        self.dispatch_calls+=1;self.descriptors.append(deepcopy(descriptor))
        require(bool(self.dispatches),'capture-fixture-dispatch-exhausted')
        return self.receipt(handle,self.dispatches.popleft())

    def execution(self,handle,reply,*,continuation_session=None):
        require(type(reply) is ExecReply and isinstance(handle,_Handle),'capture-fixture-exec-type')
        if continuation_session is not None:
            require(handle.session==continuation_session and type(continuation_session) is int,'capture-continuation-mismatch')
        state='unknown';session=reply.session_id if reply.session_id is not None else continuation_session
        if reply.protocol==handle.protocol=='unified-exec':
            if reply.session_id is not None and reply.exit_code is None and reply.signal is None:state='in_progress'
            elif reply.session_id is None and reply.signal is not None and reply.exit_code is None:state='cancelled'
            elif reply.session_id is None and reply.signal is None and reply.exit_code is not None:state='success' if reply.exit_code==0 else 'failure'
        if handle.session is not None and state in ('success','failure','cancelled'):
            require(continuation_session is not None,'capture-continuation-binding-missing')
        process={'kind':'transport-session','protocol':'unified-exec','session_id':session} if session is not None and reply.protocol==handle.protocol=='unified-exec' else None
        record=ExecutionResult(handle.invocation,reply.native_completion_id,session,process,state,
            reply.exit_code,reply.signal,reply.source_at if state in ('success','failure','cancelled') else None,reply.source_refs,reply.protocol)
        return self.receipt(handle,record)


class BoundaryRecorder:
    def __init__(self,ledger,port,*,wall_clock=time.time,monotonic_clock=time.monotonic,ids=lambda:uuid4().hex,crash=lambda _:None):
        require(isinstance(port,BoundaryPort),'capture-code-port-registration-required')
        self.ledger=ledger;self.port=port;self.wall_clock=wall_clock;self.monotonic_clock=monotonic_clock;self.ids=ids;self.crash=crash
        self.assignments={};self.operations={};self.last_monotonic=None
        self.issued=set()

    def _identifier(self,namespace):
        identity=namespace+':'+self.ids()
        require(identity not in self.issued,'capture-generated-id-conflict')
        self.issued.add(identity)
        return identity

    def _handle(self,handle,operations=False):
        records=self.operations if operations else self.assignments
        require(type(handle) is _Handle and handle.recorder is self and records.get(handle.identity) is handle,'capture-opaque-handle')
        return handle

    def _record(self,kind,payload,*,assignment=None,operation=None,effective_at=None,dependencies=()):
        now=self.wall_clock();mono=self.monotonic_clock()
        require(stamp(now) and stamp(mono) and (self.last_monotonic is None or mono>=self.last_monotonic),'capture-clock')
        snapshot=self.ledger.snapshot();require(snapshot['manifest'] is not None and not snapshot['gaps'],'capture-ledger-incomplete')
        m=snapshot['manifest'];identity=self._identifier('event')
        row=event({'schema':'flightops-boundary-event/1','event_id':identity,'recorder_id':m['recorder_id'],'recorder_epoch':m['recorder_epoch'],
            'sequence':m['last_sequence']+1,'previous_event_digest':m['last_event_digest'],'kind':kind,'recorded_at':now,'effective_at':effective_at,
            'boundary_id':self._identifier('boundary'),'assignment_id':assignment,'operation_id':operation,
            'owner_binding_id':identity if kind=='owner_bound' else None,'dependencies':list(dependencies),'evidence_refs':[],'payload':payload})
        self.ledger.append([row]);self.last_monotonic=mono
        return row

    def prepare_assignment(self,intent):
        require(type(intent) is AssignmentIntent,'capture-intent-type')
        identity=self._identifier('assignment')
        self._record('assignment_intent',intent.fields(),assignment=identity)
        handle=_Handle(self,identity);self.assignments[identity]=handle
        return handle

    def dispatch(self,handle):
        handle=self._handle(handle);require(not handle.attempted,'capture-dispatch-already-attempted')
        snapshot=self.ledger.snapshot();require(not snapshot['gaps'],'capture-ledger-incomplete')
        intent=next(e['payload'] for e in snapshot['events'] if e['assignment_id']==handle.identity and e['kind']=='assignment_intent')
        handle.attempted=True;self.crash('before-action')
        try:receipt=self.port.dispatch({'thread_id':intent['target_thread_id'],'client_request_id':handle.identity},handle)
        except Exception:raise ContractError('capture-dispatch-result-unavailable') from None
        self.crash('after-action-before-receipt')
        return self.observe_dispatch(handle,receipt)

    def _receipt(self,handle,receipt,record_type):
        require(type(receipt) is _Receipt and receipt.port is self.port and receipt.handle is handle and type(receipt.record) is record_type,'capture-port-receipt-binding')
        return receipt.record

    def observe_dispatch(self,handle,receipt):
        handle=self._handle(handle);record=self._receipt(handle,receipt,DispatchObservation)
        require(handle.attempted,'capture-dispatch-not-attempted')
        self._record(record.kind,record.fields(),assignment=handle.identity,effective_at=record.response_observed_at)
        return self.facts()

    def observe_owner_binding(self,handle,receipt):
        handle=self._handle(handle);record=self._receipt(handle,receipt,OwnerBindingObservation)
        self._record(record.kind,record.fields(),assignment=handle.identity)
        return self.facts()

    def begin_operation(self,assignment,descriptor):
        assignment=self._handle(assignment)
        require(type(descriptor) is ExecutionStart and descriptor.recorder_invocation_id is None,'capture-operation-descriptor')
        identity=self._identifier('operation');invocation=self._identifier('invocation')
        record=replace(descriptor,recorder_invocation_id=invocation)
        self._record(record.kind,record.fields(),assignment=assignment.identity,operation=identity,effective_at=record.started_at)
        handle=_Handle(self,identity,assignment.identity,invocation);handle.protocol=descriptor.protocol;self.operations[identity]=handle
        return handle

    def observe_execution(self,handle,receipt):
        handle=self._handle(handle,True);record=self._receipt(handle,receipt,ExecutionResult)
        require(record.recorder_invocation_id==handle.invocation,'capture-invocation-mismatch')
        if handle.session is not None and record.terminal_state in ('success','failure','cancelled'):
            require(record.transport_session_id is not None,'capture-continuation-binding-missing')
        if handle.session is not None and record.transport_session_id is not None:
            require(handle.session==record.transport_session_id,'capture-continuation-mismatch')
        fields=record.fields()
        if handle.last_result==fields:return self.facts()
        self._record(record.kind,fields,assignment=handle.assignment,operation=handle.identity,effective_at=record.completed_at)
        if record.transport_session_id is not None:handle.session=record.transport_session_id
        handle.last_result=fields
        return self.facts()

    def observe_git_result(self,handle,receipt):
        handle=self._handle(handle,True);record=self._receipt(handle,receipt,GitResult)
        self._record(record.kind,record.fields(),assignment=handle.assignment,operation=handle.identity)
        return self.facts()

    def resolve_assignment(self,handle,receipt,*,dependencies=()):
        handle=self._handle(handle);record=self._receipt(handle,receipt,AssignmentResolution)
        self._record(record.kind,record.fields(),assignment=handle.identity,effective_at=record.resolved_at,dependencies=dependencies)
        return self.facts()

    def checkpoint(self,record):
        require(type(record) is CoverageCheckpoint,'capture-checkpoint-type')
        return self._record(record.kind,record.fields())

    def facts(self):
        snapshot=self.ledger.snapshot()
        return project(snapshot['events'],ledger_gaps=snapshot['gaps'])

    def seal_snapshot(self,output_path):
        return self.ledger.seal_snapshot(output_path)
