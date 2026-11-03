from copy import deepcopy

from .contract import ContractError, encode, require
from .capture_contract import COMMON, PAYLOADS, MANIFEST, LEDGER_GAPS, LedgerSnapshot, bounded_json
from .owner_lookup_contract import OUTPUT as LOOKUP, SOURCE_REF, accept_result
from .schema import obj, arr, enum, nullable, IDENT, HASH, COUNT, TIME, BOOL, validate

MAX_BYTES=1048576
AUTH={'authentication':enum('not-performed'),'merge_authority':enum(False),'normalized_rows':arr({'type':'null'},0)}
KEY=obj({k:IDENT for k in ('recorder_id','recorder_epoch','assignment_id')})
EVENT={'anyOf':[obj({**deepcopy(COMMON),'kind':enum(k),'payload':deepcopy(v)}) for k,v in PAYLOADS.items()]}
SNAPSHOT=obj({'schema':enum('flightops-boundary-snapshot/1'),'manifest':nullable(deepcopy(MANIFEST)),
    'events':arr(EVENT,100),'gaps':arr(enum(*LEDGER_GAPS),64),'as_of':nullable(TIME),'custody':enum('supplied-replay')})
REFERENCE=obj({'kind':enum('event','selector','lookup'),'digest':HASH,**{k:nullable(IDENT) for k in
    ('recorder_id','recorder_epoch','assignment_id','event_id','selection_id')}})
IDENTITY=obj({'module':IDENT,'device':COUNT,'inode':COUNT,'bytes':COUNT})
BINDING=obj({'component':enum('capture','guarded-orch','owner-lookup'),'version':IDENT,'digest':HASH,
    'before_identity':IDENTITY,'after_identity':IDENTITY})
FAILURES=('timeout','cancelled','transport','malformed-response','source-changed','publication-ambiguous','unavailable')
CALL=obj({'schema':enum('flightops-primary-owner-call/1'),'producer_version':IDENT,'invocation_id':IDENT,
    'parent_invocation_id':nullable(IDENT),'stage':enum('dispatch','lookup'),'host':enum('root-mac','rig'),
    'route':enum('guarded-orch','existing-ssh-owner'),'request_ref':REFERENCE,'result_ref':nullable(REFERENCE),
    'request_nonce':IDENT,'result_nonce':nullable(IDENT),'elapsed_ms':TIME,'started_at':TIME,'observed_at':nullable(TIME),
    'terminal':enum('completed','failed','cancelled','unknown'),'exit_code':nullable({'type':'integer','minimum':-2147483648,'maximum':2147483647}),
    'source_bindings':arr(BINDING,16),'outcome':enum('persisted','unavailable','ambiguous'),
    'failure_code':nullable(enum(*FAILURES)),**deepcopy(AUTH)})
INPUT=obj({'schema':enum('flightops-primary-owner-input/1'),'primary_snapshots':arr(SNAPSHOT,8),
    'lookup_observations':arr(deepcopy(LOOKUP),8),'caller_transcripts':arr(CALL,8),'composed_at':TIME,**deepcopy(AUTH)})
EVENT_REF=obj({'snapshot_digest':HASH,'event_id':IDENT,'event_digest':HASH})
LOOKUP_REF=obj({'selection_id':IDENT,'digest':HASH})
CANDIDATE_REF=obj({'lookup':LOOKUP_REF,'candidate_digest':HASH})
ASSIGNMENT=obj({'key':KEY,'intent_event_refs':arr(EVENT_REF,100),'dispatch_event_refs':arr(EVENT_REF,100),
    'lookup_refs':arr(LOOKUP_REF,8),'relation_refs':arr(HASH,1000),'lifecycle_dispositions':arr(IDENT,128),
    'decision_state':enum('explicit-unverified','unknown')})
RELATION=obj({'id':HASH,'key':obj({'assignment':KEY,'selection_id':IDENT,'attempt_id':nullable(IDENT),
    'provider_turn_id':nullable(IDENT),'candidate_id':nullable(IDENT)}),'raw_candidate_refs':arr(CANDIDATE_REF,100),
    'source_row_refs':arr(deepcopy(SOURCE_REF),100),
    'relation_state':enum('structurally-matched','unresolved','conflict'),'reasons':arr(IDENT,256),'attribution_state':enum('unallocated')})
PROTOCOL_RESULT=obj({'invocation_id':IDENT,'call_digests':arr(HASH,8),'state':enum('consistent-supplied','refused'),
    'reasons':arr(IDENT,128),'authentication':enum('not-performed')})
DENOMINATOR_NAMES=('primary_occurrences','primary_unique','event_occurrences','event_unique','streams','assignments',
    'dispatch_occurrences','dispatch_unique','lookup_occurrences','lookup_unique','lookup_selections','lookup_row_occurrences',
    'lookup_rows_unique','invalid_lookup_rows','candidate_occurrences','candidate_unique','edges','matched_edges','conflicting_edges',
    'unresolved_edges','multi_owner_assignments','shared_run_groups','capped_lookup_occurrences','unread_lookup_tables',
    'unresolved_decisions','call_occurrences','call_unique','refused_invocations','authenticated_rows')
OUTPUT=obj({'schema':enum('flightops-primary-owner-composition/1'),'input':deepcopy(INPUT),'input_digest':HASH,'composed_at':TIME,
    'assignments':arr(ASSIGNMENT,1000),'relations':arr(RELATION,1000),'protocol_results':arr(PROTOCOL_RESULT,8),
    'dispositions':arr(obj({'scope':IDENT,'identity':HASH,'reason':IDENT}),5000),'denominators':obj({k:COUNT for k in DENOMINATOR_NAMES}),
    'gaps':arr(IDENT,512),**deepcopy(AUTH)})


class CompositionCancelled(BaseException):
    pass


def tick(check):
    try:check()
    except BaseException as error:
        if not isinstance(error,Exception):raise
        raise CompositionCancelled() from None


def bounded(value,check):
    tick(check)
    try:
        raw=value if type(value) is bytes else encode(value)
        result=bounded_json(raw,MAX_BYTES)
    except (ValueError,TypeError,RecursionError):
        raise ContractError('primary-owner-json') from None
    def walk(item):
        tick(check)
        if type(item) is dict:
            for child in item.values():walk(child)
        elif type(item) is list:
            for child in item:walk(child)
    walk(result)
    return result


def accept_input(value,*,check=lambda:None):
    b=bounded(value,check);validate(b,INPUT)
    require(sum(len(s['events']) for s in b['primary_snapshots'])<=100,'primary-owner-event-bound')
    require(sum(len(rows) for o in b['lookup_observations'] for rows in o['rows'].values())<=100,'primary-owner-row-bound')
    for s in b['primary_snapshots']:
        tick(check);require(s==LedgerSnapshot(s['manifest'],s['events'],s['gaps'],s['as_of']).fields(),'primary-owner-snapshot')
    for o in b['lookup_observations']:
        tick(check);require(o==accept_result(o),'primary-owner-lookup')
    tick(check)
    return b


def accept_output(value,*,check=lambda:None):
    b=bounded(value,check);validate(b,OUTPUT)
    from .primary_owner_composition import compose
    require(b==compose(b['input'],check=check),'primary-owner-recomputed-output')
    tick(check)
    return b


def document(spec,name):
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:flightops:'+name,**deepcopy(spec)}


INPUT_JSON_SCHEMA=document(INPUT,'primary-owner-input:1')
OUTPUT_JSON_SCHEMA=document(OUTPUT,'primary-owner-composition:1')
CALL_JSON_SCHEMA=document(CALL,'primary-owner-call:1')
