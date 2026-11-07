from collections import defaultdict
from copy import deepcopy

from .contract import ContractError, digest, require, stamp
from .join_contract import HEAD, accept_input, accept_output
from .schema import validate

COLLECTIONS = ('heads','policies','ci_attempts','operations','assignments')
FAILED = {'FAILURE','ERROR','TIMED_OUT','STARTUP_FAILURE','ACTION_REQUIRED'}


def normalize_pr_record(record, repository, evidence, *, policy_id=None, policy_read=None):
    require(isinstance(record,dict) and type(record.get('number')) is int and record['number']>0,'pr-record')
    require(isinstance(record.get('heads'),list),'pr-heads')
    require(isinstance(record.get('pr'),str) and record['pr'].rsplit('#',1)[-1]==str(record['number']),'pr-number-binding')
    result = []
    r0, decided = record.get('r0') or {}, record.get('first_decided') or {}
    for h in record['heads']:
        is_r0 = h['sha']==r0.get('sha')
        row = {'id':'head-'+digest([repository,record['number'],h['sha']])[:32], 'revision':0,'evidence':deepcopy(evidence),
            'repository':repository,'pr':record['number'],'sha':h['sha'],'anchor':record['anchor'],
            'anchor_kind':record['anchor_kind'] or 'unknown','observed':h['observed'],'read':h['read'],
            'refresh_failed':h['refresh_failed'],'complete':h['complete'],'sync':h['sync'],
            'r0_status':r0.get('status','unknown') if is_r0 else 'unknown',
            'r0_receipt':r0.get('receipt') if is_r0 else None,
            'r0_basis':{'opened event':'opened-event','push events':'push-events'}.get(r0.get('evidence'),'workflow') if is_r0 else 'unknown',
            'policy_id':policy_id,'policy_source':record['policy_source'],'policy_read':policy_read,
            'required':deepcopy(record['required']),'replaced':h['replaced'],'first_attempt':deepcopy(h['first_attempt']),
            'first_completed':deepcopy(h['first_completed']),'latest':deepcopy(h['latest']),
            'first_decided':h['sha']==decided.get('sha'), 'gaps':sorted(set(record['gaps']+record['incomplete']+h['why']))}
        validate(row,HEAD)
        result.append(row)
    return result


def empty_input(agent, *, generated_at):
    return {'schema':'evidence-head-input/1','generated_at':generated_at,'agent_evidence':deepcopy(agent),
            'sources':[],**{k:[] for k in COLLECTIONS}}


def deduplicate(rows, collection, dispositions):
    groups = defaultdict(list)
    for row in rows:groups[row['id']].append(row)
    result = []
    for key,copies in sorted(groups.items()):
        revision = max(r['revision'] for r in copies)
        latest = {digest(r):r for r in copies if r['revision']==revision}
        for r in {digest(r):r for r in copies if r['revision']!=revision}.values():
            dispositions.append({'collection':collection,'id':key,'revision':r['revision'],'reason':'superseded-revision'})
        if len(latest)!=1:
            dispositions.append({'collection':collection,'id':key,'revision':revision,'reason':'conflicting-duplicate'})
            continue
        chosen = next(iter(latest.values()))
        if collection=='ci_attempts' and any(r['status']=='COMPLETED' and
                (r['conclusion']!=chosen['conclusion'] or r['check_id']!=chosen['check_id']) for r in copies):
            dispositions.append({'collection':collection,'id':key,'revision':revision,'reason':'terminal-check-revision-conflict'})
            continue
        invariant = {
            'operations':('run_id','native_turn_id','actor_id','repository','sha','kind','attempt_id','native_session_id','invocation_id','completion_id','process_id'),
            'assignments':('run_id','native_turn_id','actor_id','repository','sha','pr','task_id','role'),
            'ci_attempts':('repository','sha','check_id','suite_id','app_id','app_slug','workflow_run_id','event','mechanism','name','run_attempt'),
            'heads':('repository','sha','pr'),'policies':('repository','sha')}[collection]
        if any(any(r.get(k)!=chosen.get(k) for k in invariant) for r in copies):
            dispositions.append({'collection':collection,'id':key,'revision':revision,'reason':'identity-revision-conflict'})
            continue
        result.append(chosen)
    return result


def head_key(row):
    return row['repository'],row['sha']


def ci_outcome(row):
    if row['status']!='COMPLETED' or row['completed_at'] is None:return 'unknown'
    return 'pass' if row['conclusion']=='SUCCESS' else 'fail' if row['conclusion'] in FAILED else 'unknown'


def check_scope(row):
    return tuple(row[k] for k in ('repository','sha','name','app_id','app_slug','mechanism'))


def ci_for_head(head, records, sources, conflicts):
    reasons = set()
    policies = [p for p in records['policies'] if p['id']==head['policy_id'] and head_key(p)==head_key(head)]
    attempts = [a for a in records['ci_attempts'] if head_key(a)==head_key(head)]
    outcomes = {k:[] for k in ('first_attempt','first_completed','latest')}
    if len(policies)!=1 or not policies[0]['required']:
        reasons.add('policy-missing-or-ambiguous')
    else:
        policy = policies[0]
        if not policy['complete'] or policy['source']!='rules':reasons.add('policy-incomplete')
        for job in policy['required']:
            if check_scope({**head,**job}) in conflicts:
                reasons.add('check-history-conflict');continue
            rows = [a for a in attempts if all(a[k]==job[k] for k in ('name','app_id','app_slug','mechanism'))]
            if (not rows or job['mechanism']!='check_run' or any(a['check_id'] is None or a['suite_id'] is None or
                a['app_id'] is None or a['workflow_run_id'] is None or a['event'] is None or not a['history_complete'] for a in rows)):
                reasons.add('check-history-or-identity-unknown')
                continue
            if len({a['check_id'] for a in rows})!=len(rows):
                reasons.add('check-identity-conflict');continue
            ordered = sorted(rows,key=lambda a:a['check_id'])
            done = [a for a in ordered if a['status']=='COMPLETED' and a['conclusion'] not in ('CANCELLED',None)]
            outcomes['first_attempt'].append(ci_outcome(ordered[0]))
            outcomes['first_completed'].append(ci_outcome(done[0]) if done else 'unknown')
            outcomes['latest'].append(ci_outcome(ordered[-1]))
    if head['read'] is None or not head['complete'] or head['refresh_failed'] is not None or head['gaps']:
        reasons.add('head-history-incomplete')
    if head['policy_source']!='rules' or head['policy_read'] is None:reasons.add('policy-clock-or-source-unknown')
    used = [head,*policies,*attempts]
    if any(sources[r['evidence']['source_id']]['gaps'] for r in used):reasons.add('source-history-incomplete')
    result = {}
    for mode,states in outcomes.items():
        result[mode] = ('unknown' if reasons or not states else 'fail' if 'fail' in states else
                        'pass' if all(s=='pass' for s in states) else 'unknown')
        if result[mode]!=head[mode]['outcome']:reasons.add('ci-summary-disagreement')
    if 'ci-summary-disagreement' in reasons:
        result = {k:'unknown' for k in outcomes}
    r0 = head['r0_status']=='verified' and head['r0_basis']=='opened-event' and head['r0_receipt'] is not None and head['anchor_kind']=='opened'
    if not r0:reasons.add('verified-r0-missing')
    return {'head_id':head['id'],**result,'workflow_attempt_known':bool(attempts) and 'check-history-conflict' not in reasons and all(a['run_attempt'] is not None for a in attempts),
            'verified_r0_ci_eligible':r0 and not reasons and result['first_attempt'] in ('pass','fail'), 'reasons':sorted(reasons)}


def derive(bundle):
    b = accept_input(bundle)
    agent = b['agent_evidence']
    dispositions = []
    records = {k:deduplicate(b[k],k,dispositions) for k in COLLECTIONS}
    conflicted_ids = {d['id'] for d in dispositions if d['collection']=='ci_attempts' and
        d['reason'] in ('terminal-check-revision-conflict','conflicting-duplicate','identity-revision-conflict')}
    ci_conflicts = {check_scope(row) for row in b['ci_attempts'] if row['id'] in conflicted_ids}
    sources = {s['id']:s for s in b['sources']}
    for collection,rows in records.items():
        for row in rows:
            require(row['repository']==row['repository'].lower(),'canonical-repository')
            for key in ('anchor','observed','read','refresh_failed','policy_read','started_at','completed_at','assigned_at','resolved_at'):
                if row.get(key) is not None:require(row[key]<=row['evidence']['at'],'receipt-clock')
            if collection=='ci_attempts' and row['started_at'] is not None and row['completed_at'] is not None:
                require(row['started_at']<=row['completed_at'],'ci-clock')
    heads = defaultdict(list)
    for h in records['heads']:heads[head_key(h)].append(h)
    for collection in ('policies','ci_attempts'):
        for row in records[collection]:
            if not heads[head_key(row)]:
                dispositions.append({'collection':collection,'id':row['id'],'revision':row['revision'],'reason':'exact-pr-head-missing'})
    conflicting_heads = {h['id'] for hs in heads.values() for h in hs if sum(x['pr']==h['pr'] for x in hs)>1}
    runs = {r['id']:r for r in agent['runs']}
    actors = defaultdict(set)
    for row in records['operations']+records['assignments']:
        actors[(row['run_id'],row['native_turn_id'])].add(row['actor_id'])
    edges = []
    def edge(row, role, reasons, target):
        source = sources[row['evidence']['source_id']]
        if not target:reasons.add('exact-pr-head-missing')
        if any(h['id'] in conflicting_heads for h in target):reasons.add('conflicting-pr-head')
        run = runs.get(row['run_id'])
        if run is None or row['native_turn_id'] not in {t['native_turn_id'] for t in run['turns']}:
            reasons.add('owner-turn-unresolved')
        if len(actors[(row['run_id'],row['native_turn_id'])])!=1:reasons.add('actor-binding-conflict')
        return {'receipt_id':row['id'],'role':role,'run_id':row['run_id'],'native_turn_id':row['native_turn_id'],
            'head_ids':sorted(h['id'] for h in target),'state':'linked' if not reasons else 'conflict' if any('conflict' in r for r in reasons) else 'unmatched',
            'reasons':sorted(reasons),'basis':'synthetic' if source['origin']=='synthetic' else 'unverified-supplied'}
    invocation_groups = defaultdict(set)
    producers = defaultdict(set)
    for op in records['operations']:
        invocation_groups[(op['native_session_id'],op['invocation_id'])].add(digest({k:v for k,v in op.items() if k not in ('id','revision','evidence')}))
        if op['kind']=='produce':producers[head_key(op)].add(op['actor_id'])
    for op in records['operations']:
        reasons = set()
        run = runs.get(op['run_id'])
        if run is None or run['attempt_id']!=op['attempt_id'] or run['native_session_id']!=op['native_session_id']:
            reasons.add('operation-owner-mismatch')
        if run is not None and (run['started_at'] is not None and op['call_evidence']['at']<run['started_at'] or
                run['ended_at'] is not None and op['completion_evidence']['at']>run['ended_at']):
            reasons.add('operation-owner-clock-conflict')
        if len(invocation_groups[(op['native_session_id'],op['invocation_id'])])!=1:reasons.add('invocation-binding-conflict')
        if op['terminal']!='success':reasons.add('terminal-success-unproven')
        if op['completion_invocation_id']!=op['invocation_id'] or op['completion_process_id']!=op['process_id']:
            reasons.add('completion-identity-mismatch')
        if not op['call_evidence']['at']<=op['completion_evidence']['at']<=op['evidence']['at']:
            reasons.add('completion-binding-clock')
        if op['kind']=='produce':
            if op['proof']!='creation-witness' or op['before_sha'] is None or op['before_sha']==op['sha'] or op['before_sha'] not in op['parents']:
                reasons.add('creation-proof-missing')
            if len(producers[head_key(op)])!=1:reasons.add('producer-conflict')
        elif op['proof']!='publication-witness' or op['destination_repository']!=op['repository'] or op['destination_ref'] is None:
            reasons.add('publication-proof-missing')
        elif not op['destination_ref'].startswith('refs/heads/') or any(s in op['destination_ref'] for s in ('..','//','@{')) or op['destination_ref'].endswith(('/','.','.lock')):
            reasons.add('publication-ref-unsupported')
        edges.append(edge(op,op['kind'],reasons,heads[head_key(op)]))
    assignments = {a['id']:a for a in records['assignments']}
    superseded, valid_successors = set(), set()
    for a in assignments.values():
        if a['supersedes'] in assignments:
            old = assignments[a['supersedes']]
            if old['task_id']==a['task_id'] and old['repository']==a['repository'] and old['pr']==a['pr'] and old['assigned_at']<a['assigned_at']:
                superseded.add(old['id'])
                valid_successors.add(a['id'])
    ci_by_id = {c['id']:c for c in records['ci_attempts']}
    for a in assignments.values():
        reasons = set()
        for field in ('supersedes','trigger_id'):
            seen, cursor = set(), a
            while cursor is not None:
                if cursor['id'] in seen:
                    reasons.add('assignment-dependency-cycle');break
                seen.add(cursor['id'])
                if field=='trigger_id' and cursor['trigger']!='review':break
                cursor = assignments.get(cursor[field])
        target = [h for h in heads[head_key(a)] if h['pr']==a['pr']]
        if a['resolution']!='completed' or a['resolved_at'] is None:reasons.add('assignment-unresolved')
        if a['resolved_at'] is not None and a['resolved_at']<a['assigned_at']:reasons.add('assignment-clock')
        owner = runs.get(a['run_id'])
        if owner is not None and (owner['started_at'] is not None and a['assigned_at']<owner['started_at'] or
                owner['ended_at'] is not None and a['resolved_at'] is not None and a['resolved_at']>owner['ended_at']):
            reasons.add('assignment-owner-clock-conflict')
        if a['id'] in superseded:reasons.add('assignment-superseded')
        if a['supersedes'] is not None and a['id'] not in valid_successors:reasons.add('supersession-unresolved')
        if a['role']=='review':
            producer_actors = {o['actor_id'] for o in records['operations'] if o['kind']=='produce' and head_key(o)==head_key(a)
                and any(e['receipt_id']==o['id'] and e['state']=='linked' for e in edges)}
            if not producer_actors:reasons.add('review-producer-unknown')
            if a['actor_id'] in producer_actors:reasons.add('same-actor-review')
            if a['reviewed_sha']!=a['sha'] or a['review_outcome'] is None:reasons.add('review-target-unproven')
        if a['trigger']=='ci-failure':
            trigger = ci_by_id.get(a['trigger_id'])
            if trigger is None or head_key(trigger)!=head_key(a) or ci_outcome(trigger)!='fail' or trigger['completed_at']>a['assigned_at']:
                reasons.add('ci-trigger-unresolved')
        elif a['trigger']=='review':
            trigger = assignments.get(a['trigger_id'])
            if (trigger is None or trigger['role']!='review' or trigger['review_outcome']!='changes_requested' or
                head_key(trigger)!=head_key(a) or trigger['pr']!=a['pr'] or trigger['resolved_at'] is None or trigger['resolved_at']>a['assigned_at']):
                reasons.add('review-trigger-unresolved')
        elif a['trigger_id'] is not None:reasons.add('unexpected-trigger')
        if a['role']=='rework' and a['trigger']=='initial':reasons.add('rework-trigger-missing')
        edges.append(edge(a,a['role'],reasons,target))
    while True:
        changed = False
        by_id = {e['receipt_id']:e for e in edges}
        for e in edges:
            a = assignments.get(e['receipt_id'])
            if e['state']=='linked' and a is not None and a['trigger']=='review':
                parent = by_id.get(a['trigger_id'])
                if parent is None or parent['state']!='linked':
                    e['state']='unmatched';e['reasons']=sorted(set(e['reasons'])|{'review-trigger-unqualified'});changed=True
        if not changed:break
    eligible = [e for e in edges if e['state']=='linked']
    request_links = []
    for q in agent['requests']:
        matches = [e for e in eligible if e['run_id']==q['original_run_id'] and e['native_turn_id']==q['original_native_turn_id']]
        ids = sorted({h for e in matches for h in e['head_ids']})
        reasons = set()
        if not matches:reasons.add('explicit-role-or-operation-missing')
        if q['conflict']:reasons.add('request-owner-conflict')
        if len(ids)>1:reasons.add('shared-pr-membership')
        if not q['usage']['complete']:reasons.add('usage-incomplete')
        run = runs.get(q['original_run_id'])
        child_known = run is not None and run['expected_children']==[]
        if not child_known:reasons.add('child-allocation-unknown')
        reconciled_run = run is not None and run['status']=='complete' and all(
            any(rec['run_id']==run['id'] and rec['native_turn_id']==t['native_turn_id'] and rec['state']=='match'
                for rec in agent['reconciliation']) for t in run['turns'])
        if not reconciled_run:reasons.add('main-turn-reconciliation-incomplete')
        linked = bool(ids) and not q['conflict'] and q['usage']['complete'] and len(ids)==1
        request_links.append({'request_id':q['id'],'head_ids':ids,'receipt_ids':sorted({e['receipt_id'] for e in matches}),
            'allocation':'shared' if len(ids)>1 else 'exclusive' if linked else 'unknown','reasons':sorted(reasons),
            'pr_linked_usage_eligible':linked,'whole_task_usage_eligible':linked and child_known and reconciled_run})
    ci = [ci_for_head(h,records,sources,ci_conflicts) for h in records['heads']]
    r0_receipts = defaultdict(set)
    for h in records['heads']:
        if h['r0_status']=='verified' and h['r0_receipt'] is not None:
            r0_receipts[(h['repository'],h['r0_receipt'])].add((h['pr'],h['sha']))
    for h,c in zip(records['heads'],ci):
        if len(r0_receipts[(h['repository'],h['r0_receipt'])])>1:
            c['verified_r0_ci_eligible']=False;c['reasons']=sorted(set(c['reasons'])|{'r0-receipt-conflict'})
    denominators = {'selected_projection_runs':agent['inventory']['run_count'],'selected_projection_turns':agent['inventory']['turn_count'],
        'accepted_turns':sum(len(r['turns']) for r in agent['runs']),'captured_responses':len(agent['requests']),
        'known_usage_response_observations':sum(q['usage']['complete'] and not q['conflict'] for q in agent['requests']),
        'reconciled_main_turns':sum(r['state']=='match' for r in agent['reconciliation']),
        'exact_pr_linked_responses':sum(r['pr_linked_usage_eligible'] for r in request_links),
        'shared_pr_responses':sum(r['allocation']=='shared' for r in request_links),
        'verified_r0_ci_heads':sum(c['verified_r0_ci_eligible'] for c in ci),
        'explicit_assignment_roles':sum(e['state']=='linked' and e['role'] in ('implementation','review','rework') for e in edges),
        'independent_reviews':sum(e['state']=='linked' and e['role']=='review' for e in edges),
        'child_allocation_known_runs':sum(r['expected_children']==[] for r in agent['runs']),
        'retention_complete_sources':sum(s['complete_through'] is not None and s['complete_through']>=agent['as_of'] and not s['gaps'] for s in agent['sources']),
        'pr_head_observations':len(records['heads']),
        'r0_claimed_verified_heads':sum(h['r0_status']=='verified' for h in records['heads']),
        'ci_first_attempt_known_heads':sum(c['first_attempt'] in ('pass','fail') for c in ci),
        'workflow_attempt_known_heads':sum(c['workflow_attempt_known'] for c in ci)}
    gaps = set(agent['inventory']['gaps']) | {'authentication-not-performed'}
    if not records['heads']:gaps.add('pr-head-facts-missing')
    if not records['operations']:gaps.add('operation-receipts-missing')
    if not records['assignments']:gaps.add('assignment-receipts-missing')
    if dispositions:gaps.add('receipt-conflicts-or-supersession')
    normalized_sources = sorted(b['sources'],key=lambda s:s['id'])
    dispositions = sorted(dispositions,key=lambda r:(r['collection'],r['id'],r['revision'],r['reason']))
    result = {'schema':'evidence-head-joins/1','generated_at':b['generated_at'],
        'input_digest':digest([b['generated_at'],agent,normalized_sources,records,dispositions]),
        'authentication':'not-performed','merge_authority':False,'agent_evidence':agent,
        'sources':normalized_sources, 'records':records,'dispositions':dispositions,
        'ci':ci,'edges':sorted(edges,key=lambda e:e['receipt_id']),'request_links':sorted(request_links,key=lambda r:r['request_id']),
        'denominators':denominators,'gaps':sorted(gaps)}
    return accept_output(result)


def refresh(previous, bundle, *, now):
    require(stamp(now),'join-refresh-clock')
    old = accept_output(previous['result']) if previous is not None else None
    require(old is None or now>=old['generated_at'],'join-refresh-clock')
    try:
        current = derive(bundle)
        require(current['generated_at']<=now and (old is None or current['generated_at']>=old['generated_at']),'join-refresh-regressed')
        require(old is None or current['agent_evidence']['as_of']>=old['agent_evidence']['as_of'],'join-refresh-regressed')
        if old is not None:
            clocks = {s['id']:s['snapshot_at'] for s in current['sources']+current['agent_evidence']['sources']}
            require(all(s['id'] in clocks and clocks[s['id']]>=s['snapshot_at'] for s in old['sources']+old['agent_evidence']['sources']), 'join-source-regressed')
        return {'schema':'evidence-head-state/1','result':current,'received_at':now,'attempted_at':now,'error':None}
    except (ContractError,TypeError,KeyError,ValueError,RecursionError):
        return {'schema':'evidence-head-state/1','result':old,'received_at':previous['received_at'] if old is not None else None,
                'attempted_at':now,'error':'input-unavailable' if bundle is None else 'input-invalid-or-regressed'}
