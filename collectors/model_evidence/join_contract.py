from copy import deepcopy

from .contract import MAX_BYTES, encode, require
from .observed_contract import accept_observed
from .schema import obj, arr, enum, nullable, IDENT, TIME, COUNT, BOOL, HASH, FULL_SHA, REPOSITORY, EVIDENCE, validate

NAME = {'type':'string','maxLength':256}
MODE = obj({'outcome':enum('pass','fail','unknown'),'reason':nullable({'type':'string','maxLength':1000}),'failed':arr(NAME)})
SOURCE = obj({'id':IDENT,'digest':HASH,'generated_at':nullable(TIME),'snapshot_at':TIME,'complete_through':nullable(TIME),
              'gaps':arr(IDENT),'origin':enum('synthetic','supplied')})
BASE = {'id':IDENT,'revision':COUNT,'evidence':EVIDENCE}
JOB = obj({'name':NAME,'app_id':nullable(COUNT),'app_slug':nullable(IDENT),'mechanism':enum('check_run','commit_status')})
HEAD = obj({**BASE,'repository':REPOSITORY,'pr':{**COUNT,'minimum':1},'sha':FULL_SHA,
    'anchor':nullable(TIME),'anchor_kind':enum('opened','ready','incomplete','unknown'),'observed':nullable(TIME),
    'read':nullable(TIME),'refresh_failed':nullable(TIME),'complete':BOOL,'sync':nullable(BOOL),
    'r0_status':enum('verified','supported','candidate','unknown'),
    'r0_receipt':nullable({'anyOf':[IDENT,{**COUNT,'minimum':1}]}),
    'r0_basis':enum('opened-event','push-events','workflow','unknown'),'policy_id':nullable(IDENT),
    'policy_source':enum('rules','observed','unknown'),'policy_read':nullable(TIME),'required':arr(NAME),'replaced':BOOL,
    'first_attempt':MODE,'first_completed':MODE,'latest':MODE,'first_decided':BOOL,'gaps':arr(NAME)})
POLICY = obj({**BASE,'repository':REPOSITORY,'sha':FULL_SHA,'source':enum('rules','observed','unknown'),
              'complete':BOOL,'required':arr(JOB)})
CI = obj({**BASE,'repository':REPOSITORY,'sha':FULL_SHA,'check_id':nullable({**COUNT,'minimum':1}),'suite_id':nullable({**COUNT,'minimum':1}),
    'app_id':nullable({**COUNT,'minimum':1}),'app_slug':nullable(IDENT),'name':NAME,'mechanism':enum('check_run','commit_status'),
    'workflow_run_id':nullable({**COUNT,'minimum':1}),'workflow_id':nullable(IDENT),'event':nullable(IDENT),
    'run_number':nullable(COUNT),'run_attempt':nullable({**COUNT,'minimum':1}),
    'started_at':nullable(TIME),'completed_at':nullable(TIME),'status':enum('COMPLETED','IN_PROGRESS','QUEUED','UNKNOWN'),
    'conclusion':nullable(IDENT),'history_complete':BOOL})
OPERATION = obj({**BASE,'run_id':IDENT,'attempt_id':IDENT,'native_session_id':IDENT,'native_turn_id':IDENT,'actor_id':IDENT,
    'invocation_id':IDENT,'completion_id':IDENT,'process_id':IDENT,'completion_invocation_id':IDENT,'completion_process_id':IDENT,
    'repository':REPOSITORY,'sha':FULL_SHA,
    'kind':enum('produce','publish'),'terminal':enum('success','failure','unknown'),
    'proof':enum('creation-witness','publication-witness','unqualified'),
    'before_sha':nullable(FULL_SHA),'parents':arr(FULL_SHA,8),'destination_repository':nullable(REPOSITORY),
    'destination_ref':nullable(IDENT),'invocation_digest':HASH,'completion_digest':HASH,
    'call_evidence':EVIDENCE,'completion_evidence':EVIDENCE,'repository_evidence':EVIDENCE})
ASSIGNMENT = obj({**BASE,'task_id':IDENT,'run_id':IDENT,'native_turn_id':IDENT,'actor_id':IDENT,
    'repository':REPOSITORY,'pr':{**COUNT,'minimum':1},'sha':FULL_SHA,'role':enum('implementation','review','rework'),
    'assigned_at':TIME,'resolved_at':nullable(TIME),'resolution':enum('open','completed','failed'),
    'reviewed_sha':nullable(FULL_SHA),'review_outcome':nullable(enum('accepted','changes_requested')),
    'trigger':enum('initial','ci-failure','review'),'trigger_id':nullable(IDENT),'supersedes':nullable(IDENT)})
INPUT = obj({'schema':enum('evidence-head-input/1'),'generated_at':TIME,'agent_evidence':{'type':'null'},
    'sources':arr(SOURCE,128),'heads':arr(HEAD),'policies':arr(POLICY),'ci_attempts':arr(CI),
    'operations':arr(OPERATION),'assignments':arr(ASSIGNMENT)})
RECORDS = obj({k:INPUT['properties'][k] for k in ('heads','policies','ci_attempts','operations','assignments')})
DISPOSITION = obj({'collection':IDENT,'id':IDENT,'revision':COUNT,'reason':IDENT})
CI_RESULT = obj({'head_id':IDENT,'first_attempt':enum('pass','fail','unknown'),'first_completed':enum('pass','fail','unknown'),
    'latest':enum('pass','fail','unknown'),'workflow_attempt_known':BOOL,'verified_r0_ci_eligible':BOOL,'reasons':arr(IDENT)})
EDGE = obj({'receipt_id':IDENT,'role':enum('produce','publish','implementation','review','rework'),
    'run_id':IDENT,'native_turn_id':IDENT,'head_ids':arr(IDENT),'state':enum('linked','unmatched','conflict'),
    'reasons':arr(IDENT),'basis':enum('synthetic','unverified-supplied')})
LINK = obj({'request_id':IDENT,'head_ids':arr(IDENT),'receipt_ids':arr(IDENT),
    'allocation':enum('exclusive','shared','unknown'),'reasons':arr(IDENT),
    'pr_linked_usage_eligible':BOOL,'whole_task_usage_eligible':BOOL})
DENOMINATORS = obj({k:COUNT for k in ('selected_projection_runs','selected_projection_turns','accepted_turns','captured_responses',
    'known_usage_response_observations','reconciled_main_turns','exact_pr_linked_responses','shared_pr_responses',
    'verified_r0_ci_heads','explicit_assignment_roles','independent_reviews','child_allocation_known_runs','retention_complete_sources',
    'pr_head_observations','r0_claimed_verified_heads','ci_first_attempt_known_heads','workflow_attempt_known_heads')})
OUTPUT = obj({'schema':enum('evidence-head-joins/1'),'generated_at':TIME,'input_digest':HASH,
    'authentication':enum('not-performed'),'merge_authority':enum(False),'agent_evidence':{'type':'null'},
    'sources':arr(SOURCE,128),'records':RECORDS,'dispositions':arr(DISPOSITION,25000),
    'ci':arr(CI_RESULT),'edges':arr(EDGE,10000),'request_links':arr(LINK),'denominators':DENOMINATORS,'gaps':arr(IDENT)})


def document(spec, name):
    result = deepcopy(spec)
    result['properties']['agent_evidence'] = {'$ref':'urn:flightops:agent-evidence:2'}
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:flightops:'+name,**result}


INPUT_JSON_SCHEMA = document(INPUT,'evidence-head-input:1')
OUTPUT_JSON_SCHEMA = document(OUTPUT,'evidence-head-joins:1')


def accepted(value, spec):
    require(isinstance(value,dict) and len(encode(value))<=MAX_BYTES,'join-size')
    stub = {**value,'agent_evidence':None}
    validate(stub,spec)
    result = deepcopy(value)
    result['agent_evidence'] = accept_observed(value['agent_evidence'])
    require(result['generated_at']>=result['agent_evidence']['generated_at'],'join-clock')
    sources = {s['id']:s for s in result['sources']}
    require(len(sources)==len(result['sources']),'join-source-duplicate')
    require(not set(sources)&{s['id'] for s in result['agent_evidence']['sources']},'join-source-collision')
    for s in sources.values():
        require(s['snapshot_at']<=result['generated_at'] and
                (s['complete_through'] is None or s['complete_through']<=s['snapshot_at']) and
                (s['generated_at'] is None or s['generated_at']<=s['snapshot_at']),'join-source-clock')
    def walk(node):
        if isinstance(node,dict):
            if set(node)==set(EVIDENCE['properties']):
                source = sources.get(node['source_id'])
                require(source is not None and node['source_digest']==source['digest'] and node['at']<=source['snapshot_at'],'join-provenance')
            else:
                for v in node.values():walk(v)
        elif isinstance(node,list):
            for v in node:walk(v)
    for key,v in result.items():
        if key!='agent_evidence':walk(v)
    return result


def accept_input(value):
    return accepted(value,INPUT)


def accept_output(value):
    return accepted(value,OUTPUT)
