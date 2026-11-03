from collections import defaultdict
from copy import deepcopy
import hashlib

from .contract import digest,encode,require,stamp
from .native import timestamp
from .join_contract import accept_input
from .selected_ci_contract import accept_source,accept_result,REPOSITORY,SHA

TECHNICAL=('fast','integration')
TECHNICAL_APP=(15368,'github-actions')
EXCLUDED=('evaluate','review-gate')
FAILED={'FAILURE','ERROR','TIMED_OUT','STARTUP_FAILURE','ACTION_REQUIRED'}


def normalize(raw, *, generated_at):
    source=accept_source(raw)
    capture=timestamp(source['capture_time'])
    require(stamp(generated_at) and generated_at>=capture,'selected-ci-clock')
    dispositions=[]
    def note(kind,identity,reason):
        dispositions.append({'kind':kind,'identity':str(identity),'reason':reason})
    def clock(value,kind,identity):
        if value is None:
            note(kind,identity,'timestamp-unavailable');return None
        try:
            at=timestamp(value)
        except ValueError:
            note(kind,identity,'timestamp-invalid');return None
        require(at<=capture,'selected-ci-future-source-clock')
        return at
    for at in (source['head']['fetched'],source['pr_readiness']['fetched_at'],source['policy']['read'],source['policy']['fetched'],source['event_coverage']['polled']):
        require(at<=capture,'selected-ci-future-source-clock')
    for field in ('createdAt','mergedAt','closedAt'):
        clock(source['pr_readiness'][field],'readiness',field)
    require(all(b<=source['event_coverage']['polled'] for a,b in source['event_coverage']['coverage']),'selected-ci-coverage-clock')
    projection_digest=digest(source)
    sid='selected-ci-'+digest([REPOSITORY,8383,SHA])[:24]
    ev=lambda line:{'source_id':sid,'source_digest':projection_digest,'line':line,'at':capture}
    suites=source['head']['checkSuites'];suite_rows=suites['nodes']
    head_complete=(not suites['pageInfo']['hasNextPage'] and all(s['databaseId'] is not None for s in suite_rows)
        and suites['totalCount']==len({s['databaseId'] for s in suite_rows}))
    if not head_complete:note('head',SHA,'suite-count-or-pagination-incomplete')
    app_slugs=defaultdict(set)
    app_ids=defaultdict(set)
    for s in suite_rows:
        app=s['app'] or {}
        if app.get('databaseId') is not None:app_slugs[app['databaseId']].add(app.get('slug'))
        if app.get('slug') is not None:app_ids[app['slug']].add(app.get('databaseId'))
    suite_groups=defaultdict(list)
    for s in suite_rows:suite_groups[s['databaseId']].append(s)
    selected_suites=[]
    for identity,copies in sorted(suite_groups.items(),key=lambda item:encode(item[0])):
        distinct={digest({**s,'checkRuns':{**s['checkRuns'],'nodes':sorted(s['checkRuns']['nodes'],key=encode)}}):s for s in copies}
        metadata={digest({k:s.get(k) for k in ('databaseId','createdAt','app','workflowRun')}) for s in copies}
        complete=head_complete and all(not s['checkRuns']['pageInfo']['hasNextPage'] and
            all(c['databaseId'] is not None for c in s['checkRuns']['nodes']) and
            s['checkRuns']['totalCount']==len({c['databaseId'] for c in s['checkRuns']['nodes']}) for s in copies)
        if len(copies)>1:
            note('suite',identity,'exact-duplicate' if len(distinct)==1 else 'suite-identity-conflict' if len(metadata)>1 else 'check-connection-copies-differ')
        for s in sorted(distinct.values(),key=encode):selected_suites.append((s,len(metadata)>1,complete))
    rows=[];bad_jobs=set();groups=defaultdict(list)
    for suite,suite_conflict,complete in selected_suites:
        app=suite['app'] or {};app_id,slug=app.get('databaseId'),app.get('slug')
        suite_id=suite['databaseId'];clock(suite['createdAt'],'suite',suite_id)
        identity_valid=app_id is not None and slug is not None and suite_id is not None and not suite_conflict
        app_conflict=len(app_slugs[app_id])>1 or len(app_ids[slug])>1
        if app_conflict:note('suite',suite_id,'app-identity-conflict')
        connection=suite['checkRuns']
        if not complete:note('suite',suite_id,'check-count-or-pagination-incomplete')
        workflow=suite.get('workflowRun') or {}
        if workflow:clock(workflow['createdAt'],'workflow',workflow['databaseId'])
        for check in connection['nodes']:
            key=(REPOSITORY,SHA,app_id,slug,'check_run',check['databaseId'])
            groups[key].append((suite,check,identity_valid and not app_conflict,complete))
    for key,copies in sorted(groups.items(),key=lambda item:encode(item[0])):
        distinct=defaultdict(list)
        for s,c,valid,whole in copies:
            distinct[digest([{k:s.get(k) for k in ('databaseId','createdAt','app','workflowRun')},c])].append((s,c,valid,whole))
        identity='check-'+digest(key)[:32]
        if len(copies)>1:note('check',identity,'exact-duplicate' if len(distinct)==1 else 'check-identity-conflict')
        conflicted=len(distinct)>1
        for fingerprint in sorted(distinct):
            same=distinct[fingerprint]
            suite,check=same[0][:2]
            valid,whole=all(row[2] for row in same),all(row[3] for row in same)
            app=suite['app'] or {};job=(check['name'],app.get('databaseId'),app.get('slug'))
            start=clock(check['startedAt'],'check',identity)
            end=clock(check['completedAt'],'check',identity)
            interval_valid=start is not None and (check['status']!='COMPLETED' or end is not None)
            if start is not None and end is not None and end<start:
                interval_valid=False;note('check',identity,'invalid-interval')
            if any(at is not None and at>source['head']['fetched'] for at in (start,end)):
                interval_valid=False;note('check',identity,'check-clock-after-head-read')
            if not valid or check['databaseId'] is None:
                note('check',identity,'stable-identity-unavailable-or-conflicting')
            if conflicted or not valid or check['databaseId'] is None or not interval_valid:
                bad_jobs.add(job);continue
            if not whole:bad_jobs.add(job)
            workflow=suite.get('workflowRun') or {}
            if check['name'] in TECHNICAL and (app.get('databaseId'),app.get('slug'))!=TECHNICAL_APP:
                note('check',identity,'outside-technical-app-domain')
            rows.append({'id':identity,'revision':0,'evidence':None,'repository':REPOSITORY,'sha':SHA,
                'check_id':check['databaseId'],'suite_id':suite['databaseId'],'app_id':app.get('databaseId'),'app_slug':app.get('slug'),
                'name':check['name'],'mechanism':'check_run','workflow_run_id':workflow.get('databaseId'),
                'workflow_id':None,'event':workflow.get('event'),'run_number':workflow.get('runNumber'),'run_attempt':None,
                'started_at':start,'completed_at':end,'status':check['status'] if check['status'] in ('COMPLETED','IN_PROGRESS','QUEUED') else 'UNKNOWN',
                'conclusion':check['conclusion'],'history_complete':False})
    rows.sort(key=lambda r:r['id'])
    for r in rows:
        r['history_complete']=(r['name'],r['app_id'],r['app_slug']) not in bad_jobs and not source['source_gaps']
    valid_checks=len(rows)
    for status in sorted(source['head']['status']['contexts'],key=encode):
        identity='status-'+digest([REPOSITORY,SHA,status['context']])[:32]
        copies=[s for s in source['head']['status']['contexts'] if s['context']==status['context']]
        if len(copies)>1:
            note('status',identity,'exact-duplicate' if len({digest(s) for s in copies})==1 else 'status-state-conflict')
            if len({digest(s) for s in copies})>1:continue
        if any(r['id']==identity for r in rows):continue
        at=clock(status['createdAt'],'status',identity)
        rows.append({'id':identity,'revision':0,'evidence':None,'repository':REPOSITORY,'sha':SHA,
            'check_id':None,'suite_id':None,'app_id':None,'app_slug':None,'name':status['context'],'mechanism':'commit_status',
            'workflow_run_id':None,'workflow_id':None,'event':None,'run_number':None,'run_attempt':None,
            'started_at':None,'completed_at':at,'status':('IN_PROGRESS' if status['state']=='PENDING' else 'COMPLETED') if at is not None else 'UNKNOWN',
            'conclusion':status['state'],'history_complete':False})
        note('status',identity,'latest-state-not-historical-attempts')
    rows.sort(key=lambda r:r['id'])
    for line,row in enumerate(rows,1):row['evidence']=ev(line)
    protected=set();required=[];policy=source['policy']
    if policy['base_branch']!=source['pr_readiness']['baseRefName']:
        note('policy','captured-policy','captured-base-branch-conflict')
    for rule in policy['rules']['branchProtectionRules']['nodes']:
        protected.update(rule['requiredStatusCheckContexts'])
        protected.update(r['context'] for r in rule['requiredStatusChecks'])
        if rule['pattern']!=source['pr_readiness']['baseRefName']:
            note('policy',rule['pattern'],'captured-branch-pattern-unresolved');continue
        for r in rule['requiredStatusChecks']:
            mechanisms={x['mechanism'] for x in rows if x['name']==r['context']}
            if not mechanisms:note('policy',r['context'],'required-context-mechanism-unavailable')
            for mechanism in sorted(mechanisms):
                required.append({'name':r['context'],'app_id':None,'app_slug':(r['app'] or {}).get('slug'),'mechanism':mechanism})
    required=sorted({digest(r):r for r in required}.values(),key=encode)
    note('policy','captured-policy','historical-applicability-unavailable')
    if policy['read']>source['head']['fetched']:note('policy','captured-policy','policy-after-head-read')
    policy_row={'id':'policy-'+digest([REPOSITORY,SHA,'rules',policy['read'],policy['fetched']])[:32],'revision':0,'evidence':ev(len(rows)+1),
        'repository':REPOSITORY,'sha':SHA,'source':'rules','complete':False,'required':required}
    timeline=source['pr_readiness']['timelineItems']
    timeline_complete=(not timeline['pageInfo']['hasNextPage'] and timeline['totalCount']==timeline['original_node_count']==len(timeline['nodes'])
        and timeline['excluded_non_readiness_nodes']==0)
    if not timeline_complete:note('timeline','selected-pr','count-or-pagination-incomplete')
    for event in timeline['nodes']:clock(event['createdAt'],'timeline',event['__typename'])
    for opening in source['opening_receipts']:
        at=clock(opening['at'],'opening',opening['id'])
        require(at==opening['t'],'selected-opening-clock-conflict')
        require(opening['base_ref']==source['pr_readiness']['baseRefName'],'selected-opening-base-conflict')
    note('readiness','selected-pr','opening-draft-state-unavailable')
    note('head',SHA,'existing-head-contract-replaced-state-unavailable')
    jobs=[]
    def outcome(row):
        if row['status']!='COMPLETED' or row['completed_at'] is None:return 'unknown'
        return 'pass' if row['conclusion']=='SUCCESS' else 'fail' if row['conclusion'] in FAILED else 'unknown'
    def boundary_outcome(rows,field,latest=False):
        if not rows:return 'unknown'
        bound=(max if latest else min)(r[field] for r in rows)
        outcomes={outcome(r) for r in rows if r[field]==bound}
        return next(iter(outcomes)) if len(outcomes)==1 else 'unknown'
    for name in TECHNICAL:
        app_id,slug=TECHNICAL_APP
        captured=[r for r in rows if r['mechanism']=='check_run' and r['name']==name and (r['app_id'],r['app_slug'])==TECHNICAL_APP]
        done=[r for r in captured if r['status']=='COMPLETED' and r['completed_at'] is not None]
        complete=bool(captured) and all(r['history_complete'] for r in captured)
        jobs.append({'name':name,'app_id':captured[0]['app_id'] if captured and len({r['app_id'] for r in captured})==1 else None,
            'app_slug':slug,'valid_observations':len(captured),
            'earliest_started_outcome':boundary_outcome(captured,'started_at') if complete else 'unknown',
            'earliest_completed_outcome':boundary_outcome(done,'completed_at') if complete else 'unknown',
            'latest_completed_outcome':boundary_outcome(done,'completed_at',True) if complete else 'unknown',
            'captured_history_complete':complete})
    source_gaps=['historical-policy-unavailable','first-ready-unavailable','head-replaced-state-unavailable']
    if not timeline_complete:source_gaps.append('timeline-count-incomplete')
    source_row={'id':sid,'digest':projection_digest,'generated_at':None,'snapshot_at':capture,'complete_through':None,
                'gaps':source_gaps,'origin':'supplied'}
    result={'schema':'flightops-selected-ci-normalized/1','generated_at':generated_at,
        'input_sha256':hashlib.sha256(raw).hexdigest(),'projection_sha256':projection_digest,
        'authentication':'not-performed','merge_authority':False,'observations':deepcopy(source),'sources':[source_row],
        'heads':[],'policies':[policy_row],'ci_attempts':rows,'operations':[],'assignments':[],
        'dispositions':sorted({digest(d):d for d in dispositions}.values(),key=lambda d:(d['kind'],d['identity'],d['reason'])),
        'assessment':{'suites_retained':len(suite_rows),'check_rows_retained':sum(len(s['checkRuns']['nodes']) for s in suite_rows),
            'distinct_check_identities':sum(k[-1] is not None for k in groups),'emitted_valid_checks':valid_checks,
            'commit_status_rows_retained':len(source['head']['status']['contexts']),'protected_contexts':sorted(protected),
            'technical_contexts':list(TECHNICAL),'excluded_from_technical_domain':list(EXCLUDED),
            'timeline_complete':timeline_complete,'first_ready':'unknown','historical_policy_applicability':'unknown',
            'protected_merge_compliance':'unknown','workflow_attempt_known':False,'stable_workflow_id_known':False,
            'technical_jobs':jobs,'head_bridge':'unavailable-missing-replaced-state'}}
    return accept_result(result)


def join_input(result, agent, *, generated_at):
    normalized=accept_result(result)
    require(generated_at>=normalized['generated_at'],'selected-ci-join-clock')
    return accept_input({'schema':'evidence-head-input/1','generated_at':generated_at,'agent_evidence':deepcopy(agent),
        **{k:deepcopy(normalized[k]) for k in ('sources','heads','policies','ci_attempts','operations','assignments')}})
