from collections import defaultdict
from copy import deepcopy

from .contract import ContractError,encode,digest,require
from .capture_contract import CaptureDisposition,LEDGER_GAPS,event,bounded_json,MAX_EVENTS,REPLAY_BYTES
from .schema import validate,arr,enum


def import_supplied(raw,*,check=lambda:None):
    check()
    value=bounded_json(raw,REPLAY_BYTES)
    require(type(value) is dict and set(value)=={'schema','label','events'},'capture-import-fields')
    require(value['schema']=='flightops-boundary-import/1' and value['label'] in ('SYNTHETIC','supplied'),'capture-import-schema')
    require(type(value['events']) is list and len(value['events'])<=MAX_EVENTS,'capture-import-count')
    return project([event(v) for v in value['events']],check=check)


def git_dispositions(start,result,execution):
    reasons={'git-observation-unverified'}
    if result['object_format']!='sha1' or result['witness_kind']=='unsupported':reasons.add('unsupported-git-witness')
    if start is None:return sorted(reasons|{'operation-start-missing'})
    if (result['repository'],result['repository_binding_id'])!=(start['repository'],start['repository_binding_id']):reasons.add('git-repository-mismatch')
    if execution!='success':reasons.add('execution-success-unproven')
    if result['dry_run']:reasons.add('git-dry-run')
    if result['outcome'] in ('reused','no-op','partial','failed','unsupported'):reasons.add('git-'+result['outcome'])
    if result['object_existed_before'] is True:reasons.add('object-preexisting')
    if result['witness_kind']=='object-construction':
        if result['result_sha'] is None:reasons.add('creation-result-unavailable')
        if start['intended_object_sha'] is not None and start['intended_object_sha']!=result['result_sha']:reasons.add('git-result-mismatch')
        if start['operation_kind']!='git-create' or result['outcome']!='created':reasons.add('creation-outcome-unproven')
        if result['before_sha'] is None or result['before_sha']==result['result_sha'] or result['before_sha'] not in result['parent_shas']:reasons.add('creation-parent-unproven')
        if result['tree_sha'] is None or (result['before_sha'],result['tree_sha'],result['parent_shas'])!=(start['before_sha'],start['tree_sha'],start['parent_shas']):reasons.add('git-object-shape-mismatch')
        if result['object_existed_before'] is not False:reasons.add('first-creation-unknown')
    elif result['witness_kind']=='ref-publication':
        if start['operation_kind']!='git-publish' or result['outcome']!='published':reasons.add('publication-outcome-unproven')
        if result['result_sha'] is None or result['result_sha']!=start['intended_object_sha'] or result['ref_result_sha']!=result['result_sha']:reasons.add('publication-result-mismatch')
        if (result['destination_repository'],result['destination_ref'])!=(start['destination_repository'],start['intended_destination_ref']) or result['destination_ref'] is None:reasons.add('publication-destination-mismatch')
        if result['ref_before_sha']==result['ref_result_sha']:reasons.add('publication-no-op')
        if result['ref_before_sha']!=start['before_sha']:reasons.add('publication-before-mismatch')
    else:reasons.add('observation-only')
    return sorted(reasons)


def project(values,*,ledger_gaps=(),check=lambda:None):
    check()
    require(type(values) is list and len(values)<=MAX_EVENTS,'capture-project-count')
    validate(list(ledger_gaps),arr(enum(*LEDGER_GAPS),64))
    events=[];used=2
    for value in values:
        check();row=event(value);used+=len(encode(row));require(used<=REPLAY_BYTES,'capture-project-size');events.append(row)
    assignments=defaultdict(list);operations=defaultdict(list);ids=defaultdict(list);dispositions=[]
    tainted_assignments=set();tainted_operations=set();by_id={}
    def note(identity,reason):dispositions.append(CaptureDisposition(identity,reason).fields())
    for e in events:
        check()
        ids[e['event_id']].append(e)
        if e['assignment_id'] is not None:assignments[e['assignment_id']].append(e)
        if e['operation_id'] is not None:operations[e['operation_id']].append(e)
    for identity,copies in ids.items():
        if len({digest(e) for e in copies})>1:
            note(identity,'event-id-conflict')
            tainted_assignments.update(e['assignment_id'] for e in copies if e['assignment_id'] is not None)
            tainted_operations.update(e['operation_id'] for e in copies if e['operation_id'] is not None)
        else:by_id[identity]=copies[0]
    pending={identity:set(e['dependencies']) for identity,e in by_id.items()}
    reverse=defaultdict(set)
    for identity,deps in pending.items():
        for dependency in deps:reverse[dependency].add(identity)
    ready=[identity for identity,deps in pending.items() if not deps]
    consumed=set()
    while ready:
        check()
        identity=ready.pop();consumed.add(identity)
        for child in reverse[identity]:
            pending[child].discard(identity)
            if not pending[child] and child not in consumed:ready.append(child)
    for identity in set(by_id)-consumed:
        e=by_id[identity];note(identity,'event-dependency-missing-or-cyclic')
        if e['assignment_id'] is not None:tainted_assignments.add(e['assignment_id'])
        if e['operation_id'] is not None:tainted_operations.add(e['operation_id'])
    streams=defaultdict(list)
    for e in by_id.values():streams[e['recorder_id'],e['recorder_epoch']].append(e)
    gaps=set(ledger_gaps)
    for stream in streams.values():
        previous=None;sequence=0
        for e in sorted(stream,key=lambda r:(r['sequence'],r['event_id'])):
            if e['sequence']!=sequence+1 or e['previous_event_digest']!=previous:gaps.add('source-sequence-gap')
            previous=digest(e);sequence=e['sequence']
    intents={};intent_events={}
    for identity,rows in assignments.items():
        candidates=[e for e in rows if e['kind']=='assignment_intent']
        distinct={digest(e['payload']):e for e in candidates}
        if len(distinct)!=1:
            note(identity,'assignment-intent-missing-or-conflicting');tainted_assignments.add(identity)
        else:
            selected=min(candidates,key=lambda e:(e['recorded_at'],e['event_id']));intents[identity]=selected['payload'];intent_events[identity]=selected
    superseded=set();valid_successors=set()
    for identity,a in intents.items():
        prior=a['supersedes_assignment_id']
        if prior in intents:
            old=intents[prior]
            if all(a[k]==old[k] for k in ('task_id','repository','pr','input_sha','reviewed_sha')) and intent_events[prior]['recorded_at']<intent_events[identity]['recorded_at']:
                superseded.add(prior);valid_successors.add(identity)
    assignment_facts=[]
    for identity,rows in sorted(assignments.items()):
        check()
        rows=sorted(rows,key=lambda e:(e['recorder_id'],e['recorder_epoch'],e['sequence'],e['event_id'],digest(e)))
        a=intents.get(identity);reasons=set()
        if identity in tainted_assignments:reasons.add('assignment-conflict')
        if identity in superseded:reasons.add('assignment-superseded')
        if a is not None:
            if a['supersedes_assignment_id'] is not None and identity not in valid_successors:reasons.add('supersession-unresolved')
            for field in ('supersedes_assignment_id','trigger_id'):
                seen=set();cursor=identity
                while cursor in intents:
                    if cursor in seen:reasons.add('assignment-dependency-cycle');break
                    seen.add(cursor);item=intents[cursor]
                    if field=='trigger_id' and item['trigger_kind']!='review':break
                    cursor=item[field]
            if a['trigger_kind']=='initial' and a['trigger_id'] is not None:reasons.add('unexpected-trigger')
            if a['role']=='rework' and a['trigger_kind']=='initial':reasons.add('rework-trigger-missing')
            if a['trigger_kind']!='initial':
                trigger=intents.get(a['trigger_id'])
                if trigger is None:reasons.add('trigger-unresolved')
                elif any(a[k]!=trigger[k] for k in ('repository','pr')) or a['input_sha']!=(trigger['reviewed_sha'] if trigger['role']=='review' else trigger['input_sha']):reasons.add('trigger-target-mismatch')
                elif a['trigger_kind']=='review':
                    resolutions=[e['payload'] for e in assignments[a['trigger_id']] if e['kind']=='assignment_resolution']
                    if trigger['role']!='review' or not any(r['review_outcome']=='changes_requested' and r['resolution']=='completed' for r in resolutions):reasons.add('review-trigger-unresolved')
                else:reasons.add('ci-trigger-unresolved')
        dispatches=[e['payload'] for e in rows if e['kind']=='dispatch_observed']
        if len({digest(d) for d in dispatches})>1:reasons.add('dispatch-observation-conflict')
        dispatch=dispatches[0] if len({digest(d) for d in dispatches})==1 else None
        if dispatch is None:reasons.add('dispatch-unobserved')
        elif a is not None and dispatch['child_thread_id']!=a['target_thread_id']:reasons.add('dispatch-thread-mismatch')
        if dispatch is not None and dispatch['client_request_id'] is not None and dispatch['client_request_id']!=identity:reasons.add('dispatch-client-request-mismatch')
        owners=[e['payload'] for e in rows if e['kind']=='owner_bound']
        if len({digest(o) for o in owners})>1:reasons.add('owner-binding-conflict')
        owner=owners[0] if len({digest(o) for o in owners})==1 else None
        matched=False
        if owner is not None and dispatch is not None:
            matched=(owner['child_thread_id']==dispatch['child_thread_id'] and a is not None and owner['child_thread_id']==a['target_thread_id'] and
                (dispatch['task_id_from_transport'] is not None or dispatch['orchestration_run_id'] is not None) and
                (dispatch['task_id_from_transport'] is None or owner['task_id_from_transport']==dispatch['task_id_from_transport']) and
                (dispatch['orchestration_run_id'] is None or dispatch['orchestration_run_id']==owner['orchestration_run_id']) and
                owner['attempt_run_id']==owner['orchestration_run_id'] and owner['turn_attempt_id']==owner['attempt_id'] and
                owner['turn_session_id']==owner['native_session_id'] and bool(owner['original_source_refs']))
        if a is not None and a['requested_owner_binding_id'] is not None:
            requested=by_id.get(a['requested_owner_binding_id'])
            if requested is None or requested['kind']!='owner_bound' or requested['payload']!=owner:
                reasons.add('requested-owner-binding-unresolved');matched=False
        if not matched:reasons.add('owner-binding-unresolved')
        resolutions=[e for e in rows if e['kind']=='assignment_resolution']
        terminal=[e for e in resolutions if e['payload']['resolution'] in ('completed','failed','cancelled')]
        terminal_values={(e['payload']['resolution'],e['payload']['result_sha'],e['payload']['reviewed_sha'],e['payload']['review_outcome'],e['payload']['terminal_run_id']) for e in terminal}
        if len(terminal_values)>1:reasons.add('assignment-terminal-conflict')
        for r in resolutions:
            p=r['payload']
            if p['revision']>0 and not any(old['event_id'] in r['dependencies'] and old['payload']['revision']+1==p['revision'] for old in resolutions):reasons.add('resolution-revision-unresolved')
            if owner is None or p['terminal_run_id']!=owner['orchestration_run_id']:reasons.add('resolution-owner-mismatch')
            if p['worker_started_at'] is not None and p['worker_completed_at'] is not None and p['worker_started_at']>p['worker_completed_at']:reasons.add('worker-clock-conflict')
            if p['resolved_at'] is not None and p['resolved_at']>p['observed_at']:reasons.add('decision-clock-conflict')
            if a is not None and p['trigger_id']!=a['trigger_id']:reasons.add('resolution-trigger-mismatch')
            if a is not None and (p['result_repository'] is not None and p['result_repository']!=a['repository'] or p['result_pr'] is not None and a['pr'] is not None and p['result_pr']!=a['pr']):reasons.add('resolution-target-mismatch')
            if a is not None and a['role']=='review' and p['resolution']=='completed' and (p['review_outcome'] is None or p['reviewed_sha'] is None or p['reviewed_sha']!=a['reviewed_sha']):reasons.add('explicit-review-decision-missing')
            if p['resolution']=='completed' and (p['worker_terminal_state']!='completed' or p['resolved_at'] is None):reasons.add('explicit-work-decision-missing')
            if dispatch is not None and p['worker_started_at'] is not None and dispatch['request_observed_at']<p['worker_started_at']:reasons.add('v1-dispatch-clock-not-worker-clock')
            if p['worker_completed_at'] is not None and p['observed_at']>p['worker_completed_at']:reasons.add('v1-result-observed-after-worker')
            if a is not None and a['input_sha'] is not None and p['result_sha'] is not None and p['result_sha']!=a['input_sha']:reasons.add('v1-input-result-heads-differ')
        if not terminal:reasons.add('assignment-decision-unresolved')
        fatal={'assignment-conflict','assignment-terminal-conflict','dispatch-observation-conflict','dispatch-thread-mismatch',
            'owner-binding-conflict','owner-binding-unresolved','resolution-revision-unresolved','resolution-owner-mismatch',
            'worker-clock-conflict','decision-clock-conflict','resolution-trigger-mismatch','explicit-review-decision-missing','explicit-work-decision-missing'}
        fatal.update(('dispatch-client-request-mismatch','requested-owner-binding-unresolved','resolution-target-mismatch'))
        decision_known=len(terminal_values)==1 and not reasons&fatal
        if a is not None and len(a['memberships'])>1:reasons.add('shared-pr-membership-allocation-unknown')
        if a is not None and a['replaced'] is None:reasons.add('head-replacement-unknown-v1-unrepresentable')
        reasons.update(('source-authentication-not-performed','child-inventory-unknown','usage-unbound','actor-independence-unproven'))
        assignment_facts.append({'assignment_id':identity,'intent':deepcopy(a),'dispatches':deepcopy(dispatches),'owners':deepcopy(owners),
            'owner_state':'structurally-matched-unverified' if matched else 'unknown','run_key':'codex:'+owner['attempt_id'] if matched else None,
            'resolutions':[deepcopy(e['payload']) for e in resolutions],'reasons':sorted(reasons),
            'decision_state':'explicit-unverified' if decision_known else 'unknown',
            'consistent':not bool(reasons&fatal or reasons&{'supersession-unresolved','assignment-dependency-cycle','trigger-unresolved','trigger-target-mismatch','review-trigger-unresolved','ci-trigger-unresolved'}),
            'known':{k:a is not None and a[k] is not None for k in ('repository','pr','input_sha','replaced','actor_binding_id')}})
    fact_by_id={a['assignment_id']:a for a in assignment_facts};dependents=defaultdict(list)
    for a in assignment_facts:
        if a['intent'] is not None and a['intent']['trigger_kind']=='review':dependents[a['intent']['trigger_id']].append(a)
    queue=list(fact_by_id)
    for trigger_id in set(dependents)-set(fact_by_id):queue.append(trigger_id)
    while queue:
        check();trigger_id=queue.pop();trigger=fact_by_id.get(trigger_id)
        for a in dependents[trigger_id]:
            valid=trigger is not None and trigger['consistent'] and trigger['decision_state']=='explicit-unverified'
            valid=valid and any(r['resolution']=='completed' and r['review_outcome']=='changes_requested' and r['resolved_at'] is not None and r['resolved_at']<=intent_events[a['assignment_id']]['recorded_at'] for r in trigger['resolutions'])
            if not valid:
                if a['consistent']:queue.append(a['assignment_id'])
                a['consistent']=False;a['reasons']=sorted(set(a['reasons'])|{'review-trigger-unqualified'})
    operation_facts=[]
    for identity,rows in sorted(operations.items()):
        check()
        rows=sorted(rows,key=lambda e:(e['recorder_id'],e['recorder_epoch'],e['sequence'],e['event_id'],digest(e)))
        starts={digest(e['payload']):e['payload'] for e in rows if e['kind']=='execution_started'}
        start=next(iter(starts.values())) if len(starts)==1 else None
        reasons=set();results=[e['payload'] for e in rows if e['kind']=='execution_result']
        if len({e['assignment_id'] for e in rows if e['assignment_id'] is not None})>1:reasons.add('operation-assignment-conflict')
        if start is None:reasons.add('operation-start-missing-or-conflicting')
        if len({(e['recorder_id'],e['recorder_epoch']) for e in rows})>1:reasons.add('operation-cross-source-ambiguous')
        terminals=set();sessions=set()
        for p in results:
            if start is None or p['recorder_invocation_id']!=start['recorder_invocation_id']:reasons.add('invocation-mismatch')
            if p['transport_session_id'] is not None:sessions.add(p['transport_session_id'])
            if p['terminal_state'] in ('success','failure','cancelled'):terminals.add((p['terminal_state'],p['exit_code'],p['signal'],p['completed_at']))
            if p['terminal_state']=='success' and p['exit_code']!=0 or p['terminal_state']=='failure' and p['exit_code'] in (None,0):reasons.add('terminal-shape-conflict')
            if p['terminal_state']=='success' and p['signal'] is not None:reasons.add('terminal-shape-conflict')
            if p['terminal_state']=='cancelled' and (p['signal'] is None or p['exit_code'] is not None):reasons.add('terminal-shape-conflict')
            if start is not None and (start['protocol']!=p['protocol'] or p['protocol']!='unified-exec') and p['terminal_state'] in ('success','failure','cancelled'):reasons.add('unsupported-terminal-protocol')
            if start is not None and start['started_at'] is not None and p['completed_at'] is not None and p['completed_at']<start['started_at']:reasons.add('execution-clock-conflict')
            process=p['process_identity']
            if process is not None and process['kind']=='owned-process':
                if start is None or process['invocation_id']!=start['recorder_invocation_id'] or any(e['recorder_epoch']!=process['recorder_epoch'] for e in rows):reasons.add('process-binding-conflict')
                if start is None or start['process_identity']!=process:reasons.add('process-binding-conflict')
            if process is not None and process['kind']=='transport-session' and process['session_id']!=p['transport_session_id']:reasons.add('process-binding-conflict')
        if len(sessions)>1:reasons.add('transport-session-conflict')
        if sessions and any(p['terminal_state'] in ('success','failure','cancelled') and p['transport_session_id'] is None for p in results):reasons.add('continuation-binding-missing')
        if len(terminals)>1:reasons.add('operation-terminal-conflict')
        if identity in tainted_operations:reasons.add('operation-event-conflict')
        state=next(iter(terminals))[0] if len(terminals)==1 and not reasons else 'in_progress' if not terminals and results and results[-1]['terminal_state']=='in_progress' and not reasons else 'unknown'
        if any(e['assignment_id'] is None for e in rows):reasons.add('operation-assignment-unknown')
        if not terminals:reasons.add('terminal-unobserved')
        if not any(p['process_identity'] is not None and p['process_identity']['kind']=='owned-process' for p in results):reasons.add('process-identity-unknown')
        git=[e['payload'] for e in rows if e['kind']=='git_result']
        witnesses=[{'observation':deepcopy(g),'dispositions':git_dispositions(start,g,state)} for g in git]
        if len({digest(g) for g in git})>1:reasons.add('git-observation-conflict')
        operation_facts.append({'operation_id':identity,'start':deepcopy(start),'results':deepcopy(results),'terminal_state':state,
            'git_observations':witnesses,'reasons':sorted(reasons|{'source-authentication-not-performed'}),
            'known':{'invocation':start is not None,'process':'process-binding-conflict' not in reasons and any(p['process_identity'] is not None and p['process_identity']['kind']=='owned-process' for p in results)}})
    checkpoints=[deepcopy(e) for e in events if e['kind']=='coverage_checkpoint']
    gaps.update(('fleet-inventory-unknown','retention-unproven','child-inventory-unknown','usage-unbound','source-authentication-not-performed'))
    for e in checkpoints:
        p=e['payload'];gaps.update(p['explicit_gaps'])
        if p['pending_assignment_ids'] or p['pending_operation_ids']:gaps.add('checkpoint-pending-identities')
        if p['prior_checkpoint_id'] is not None:
            prior=by_id.get(p['prior_checkpoint_id'])
            if prior is None or prior['kind']!='coverage_checkpoint' or prior['payload']['scope_id']!=p['scope_id'] or prior['recorder_id']!=e['recorder_id'] or prior['recorder_epoch']!=e['recorder_epoch'] or prior['sequence']>=e['sequence']:gaps.add('checkpoint-predecessor-missing-or-invalid')
        seqs={v['sequence'] for v in events if (v['recorder_id'],v['recorder_epoch'])==(e['recorder_id'],e['recorder_epoch'])}
        if p['last_sequence']-p['first_sequence']+1>MAX_EVENTS or not all(n in seqs for n in range(p['first_sequence'],min(p['last_sequence']+1,p['first_sequence']+MAX_EVENTS))):gaps.add('checkpoint-sequence-gap')
    return {'schema':'flightops-boundary-projection/1','custody':'supplied-replay','authentication':'not-performed','merge_authority':False,
        'events':deepcopy(events),'assignments':assignment_facts,'operations':operation_facts,'coverage_checkpoints':checkpoints,
        'field_knownness':[{'event_id':e['event_id'],'payload':{k:v is not None for k,v in e['payload'].items()}} for e in events],
        'head_observations':[{'assignment_id':a['assignment_id'],'repository':a['intent']['repository'],'pr':a['intent']['pr'],
            'sha':a['intent']['input_sha'],'replaced':a['intent']['replaced'],'replacement_known':a['intent']['replaced'] is not None,
            'bridge':'unavailable-v1-unknown-replacement' if a['intent']['replaced'] is None else 'source-unverified'} for a in assignment_facts if a['intent'] is not None],
        'dispositions':sorted(dispositions,key=lambda d:(d['identity'],d['reason'])),'gaps':sorted(gaps),
        'as_of':max((e['recorded_at'] for e in events),default=None),'normalized_rows':[],
        'denominators':{'selected_events':len(events),'selected_assignment_identities':len(assignments),'selected_operation_identities':len(operations),
            'unresolved_assignment_decisions':sum(a['decision_state']=='unknown' for a in assignment_facts),
            'unknown_process_identities':sum(not o['known']['process'] for o in operation_facts),'authenticated_rows':0}}
