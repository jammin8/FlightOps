from copy import deepcopy

from .contract import decode,encode,require
from .schema import obj,arr,nullable,enum,COUNT,BOOL,TIME,HASH,IDENT,validate
from .join_contract import CI,POLICY,HEAD,SOURCE

MAX_BYTES=1048576
REPOSITORY='example-org/example-backend'
SHA='fed2741d5176e43566e948c07ef64f2f56879ce1'
TEXT={'type':'string','maxLength':256,'pattern':'^[^\\r\\n]*$'}
DATE=nullable({'type':'string','maxLength':40})
ID=nullable({**COUNT,'minimum':1})


def optional(properties,names):
    spec=obj(properties)
    spec['required']=[k for k in properties if k not in names]
    return spec


PAGE=obj({'hasNextPage':BOOL})
APP=obj({'slug':nullable(IDENT),'databaseId':ID})
CHECK=obj({'databaseId':ID,'name':TEXT,'status':enum('COMPLETED','IN_PROGRESS','QUEUED','WAITING','REQUESTED'),
    'conclusion':nullable(enum('ACTION_REQUIRED','CANCELLED','FAILURE','NEUTRAL','SKIPPED','STALE','STARTUP_FAILURE','SUCCESS','TIMED_OUT')),
    'startedAt':DATE,'completedAt':DATE,'isRequired':nullable(BOOL)})
CHECKS=obj({'totalCount':COUNT,'pageInfo':PAGE,'nodes':arr(CHECK,50)})
WORKFLOW=obj({'databaseId':ID,'event':nullable(IDENT),'createdAt':DATE,'runNumber':nullable(COUNT),'workflow':obj({'name':TEXT})})
SUITE=optional({'databaseId':ID,'createdAt':DATE,'app':nullable(APP),'workflowRun':nullable(WORKFLOW),'checkRuns':CHECKS},('workflowRun',))
STATUS=obj({'context':TEXT,'state':enum('ERROR','FAILURE','PENDING','SUCCESS'),'createdAt':DATE,'isRequired':nullable(BOOL),
    'creator':nullable(obj({'__typename':TEXT,'login':TEXT}))})
EVENT=obj({'__typename':enum('MergedEvent','ClosedEvent','ReopenedEvent','ReadyForReviewEvent','ConvertToDraftEvent'),'createdAt':DATE})
READINESS=obj({'number':enum(8383),'createdAt':DATE,'isDraft':BOOL,'state':enum('OPEN','CLOSED','MERGED'),
    'mergedAt':DATE,'closedAt':DATE,'fetched_at':TIME,'baseRefName':TEXT,
    'timelineItems':obj({'totalCount':COUNT,'pageInfo':PAGE,'original_node_count':COUNT,'nodes':arr(EVENT,100),'excluded_non_readiness_nodes':COUNT})})
PROTECTION=obj({'pattern':TEXT,'requiresStatusChecks':BOOL,'requiredStatusCheckContexts':arr(TEXT,100),
    'requiredStatusChecks':arr(obj({'context':TEXT,'app':nullable(obj({'slug':nullable(TEXT)}))}),100)})
RULESET=obj({'enforcement':TEXT,'target':TEXT,'conditions':obj({'refName':obj({'include':arr(TEXT,100),'exclude':arr(TEXT,100)})}),
    'rules':obj({'pageInfo':PAGE,'nodes':arr(obj({'type':TEXT}),100)})})
POLICY_SOURCE=obj({'shape':enum(2),'fetched':TIME,'read':TIME,'repository':enum(REPOSITORY),'base_branch':TEXT,
    'rules':obj({'branchProtectionRules':obj({'pageInfo':PAGE,'nodes':arr(PROTECTION,100)}),
                 'rulesets':obj({'pageInfo':PAGE,'nodes':arr(RULESET,100)})})})
OPENING=obj({'id':TEXT,'repo':enum('backend'),'type':enum('PullRequestEvent'),'at':DATE,'t':TIME,
    'action':enum('opened'),'number':enum(8383),'head_sha':enum(SHA),'base_ref':TEXT})
INPUT=obj({'schema':enum('flightops-selected-ci-source-facts/1'),'repository':enum(REPOSITORY),'pr_number':enum(8383),'sha':enum(SHA),
    'head':obj({'oid':enum(SHA),'fetched':TIME,'checkSuites':obj({'totalCount':COUNT,'pageInfo':PAGE,'nodes':arr(SUITE,15)}),
                'status':obj({'contexts':arr(STATUS,100)})}),
    'pr_readiness':READINESS,'policy':POLICY_SOURCE,'opening_receipts':arr(OPENING,1),
    'event_coverage':obj({'polled':TIME,'coverage':arr({'type':'array','items':TIME,'maxItems':2},100),'gaps':arr(TEXT,100)}),
    'source_gaps':arr(TEXT,100),'capture_time':{'type':'string','maxLength':40},
    'source_clock_semantics':enum('Capture time does not replace original read/fetched/event/policy clocks'),
    'limits':enum('One selected head; no model/producer/assignment link, caller authentication or cohort qualification'),
    'authentication':enum('Source custody only; adapter interpretation independently pending')})
DISPOSITION=obj({'kind':TEXT,'identity':TEXT,'reason':TEXT})
JOB_RESULT=obj({'name':TEXT,'app_id':ID,'app_slug':nullable(TEXT),'valid_observations':COUNT,
    'earliest_started_outcome':enum('pass','fail','unknown'),'earliest_completed_outcome':enum('pass','fail','unknown'),
    'latest_completed_outcome':enum('pass','fail','unknown'),'captured_history_complete':BOOL})
ASSESSMENT=obj({'suites_retained':COUNT,'check_rows_retained':COUNT,'distinct_check_identities':COUNT,
    'emitted_valid_checks':COUNT,'commit_status_rows_retained':COUNT,'protected_contexts':arr(TEXT,100),
    'technical_contexts':arr(TEXT,100),'excluded_from_technical_domain':arr(TEXT,100),
    'timeline_complete':BOOL,'first_ready':enum('unknown'),'historical_policy_applicability':enum('unknown'),
    'protected_merge_compliance':enum('unknown'),'workflow_attempt_known':enum(False),'stable_workflow_id_known':enum(False),
    'technical_jobs':arr(JOB_RESULT,100),'head_bridge':enum('unavailable-missing-replaced-state')})
OUTPUT=obj({'schema':enum('flightops-selected-ci-normalized/1'),'generated_at':TIME,'input_sha256':HASH,
    'projection_sha256':HASH,'authentication':enum('not-performed'),'merge_authority':enum(False),
    'observations':INPUT,'sources':arr(SOURCE,1),'heads':arr(HEAD,0),'policies':arr(POLICY,1),'ci_attempts':arr(CI,1000),
    'operations':{'type':'array','items':{'type':'null'},'maxItems':0},'assignments':{'type':'array','items':{'type':'null'},'maxItems':0},
    'dispositions':arr(DISPOSITION,5000),'assessment':ASSESSMENT})
INPUT_JSON_SCHEMA={'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:flightops:selected-ci-source:1',**deepcopy(INPUT)}
OUTPUT_JSON_SCHEMA={'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:flightops:selected-ci-normalized:1',**deepcopy(OUTPUT)}


def checked(value,spec):
    if spec.get('type')=='object':
        require(type(value) is dict,'selected-schema-object')
        require(set(spec['required'])<=set(value)<=set(spec['properties']),'selected-schema-properties')
        for key,v in value.items():checked(v,spec['properties'][key])
    elif spec.get('type')=='array':
        require(type(value) is list and len(value)<=spec['maxItems'],'selected-schema-array')
        for v in value:checked(v,spec['items'])
    else:
        validate(value,spec)


def accept_source(raw):
    require(isinstance(raw,bytes) and len(raw)<=MAX_BYTES,'selected-ci-size')
    value=decode(raw)
    checked(value,INPUT)
    for interval in value['event_coverage']['coverage']:
        require(len(interval)==2 and interval[0]<=interval[1],'selected-coverage-interval')
    return value


def accept_result(value):
    require(isinstance(value,dict) and len(encode(value))<=MAX_BYTES,'selected-ci-result-size')
    checked(value,OUTPUT)
    return deepcopy(value)
