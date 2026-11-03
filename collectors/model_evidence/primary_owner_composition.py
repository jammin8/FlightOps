from collections import defaultdict
from copy import deepcopy
import hashlib

from .contract import digest,encode,require
from .schema import validate
from .primary_owner_contract import accept_input,OUTPUT,MAX_BYTES,tick

BUDGETS={('dispatch','root-mac','guarded-orch'):5000,('lookup','root-mac','existing-ssh-owner'):15000,
    ('lookup','rig','existing-ssh-owner'):5000}
VERSIONS={'capture':'flightops-boundary-event/1','guarded-orch':'flightops-guarded-orch-call/1','owner-lookup':'flightops-owner-lookup-source/1'}
PRODUCER='flightops-primary-owner-protocol/1'


def key(row):return tuple(row[k] for k in ('recorder_id','recorder_epoch','assignment_id'))


def fields(value):return dict(zip(('recorder_id','recorder_epoch','assignment_id'),value))


def unique(rows,check):
    values={}
    for row in rows:
        tick(check);values[encode(row)]=row
    def order(item):tick(check);return item
    return [deepcopy(values[k]) for k in sorted(values,key=order)]


def reference(value,kind):
    s=value['selector'] if kind!='event' else value
    return {'kind':kind,'digest':digest(value['selector'] if kind=='selector' else value),
        **{k:s[k] for k in ('recorder_id','recorder_epoch','assignment_id')},
        'event_id':s['event_id'] if kind=='event' else s['dispatch_event_id'],
        'selection_id':None if kind=='event' else s['selection_id']}


def snapshot_reasons(s,check):
    reasons=set(s['gaps']);events=s['events'];m=s['manifest']
    if m is None:return reasons|{'manifest-unavailable'}
    seq=0;previous=None;last_segment=None;offset=0
    for segment in m['segments']:
        tick(check)
        if segment['previous_segment_digest']!=last_segment:reasons.add('segment-chain-gap')
        last_segment=segment['digest'];count=segment['event_count'];part=events[offset:offset+count];offset+=count
        if len(part)!=count:reasons.add('segment-count-mismatch')
        if part and (part[0]['sequence'],part[-1]['sequence'])!=(segment['first_sequence'],segment['last_sequence']):reasons.add('segment-range-mismatch')
        if hashlib.sha256(b''.join(encode(e) for e in part)).hexdigest()!=segment['digest']:reasons.add('segment-digest-mismatch')
        for e in part:
            tick(check)
            if (e['recorder_id'],e['recorder_epoch'])!=(m['recorder_id'],m['recorder_epoch']):reasons.add('event-epoch-mismatch')
            if e['sequence']!=seq+1 or e['previous_event_digest']!=previous:reasons.add('event-chain-gap')
            seq=e['sequence'];previous=digest(e)
    if offset!=len(events):reasons.add('segment-count-mismatch')
    if (seq,previous)!=(m['last_sequence'],m['last_event_digest']):reasons.add('manifest-chain-gap')
    if s['as_of']!=m['last_source_at']:reasons.add('manifest-clock-mismatch')
    return reasons


def index_input(b,check):
    snapshots=unique(b['primary_snapshots'],check);lookups=unique(b['lookup_observations'],check)
    all_events=[];refs=defaultdict(list);groups=defaultdict(list);stream_gaps=defaultdict(set);stream_heads=defaultdict(set)
    for s in snapshots:
        tick(check);sg=snapshot_reasons(s,check)
        if s['as_of'] is not None and s['as_of']>b['composed_at']:sg.add('primary-after-composed-at')
        m=s['manifest']
        if m:stream_heads[(m['recorder_id'],m['recorder_epoch'],m['last_sequence'])].add(m['last_event_digest'])
        for e in s['events']:
            tick(check);all_events.append(e);stream_gaps[(e['recorder_id'],e['recorder_epoch'])].update(sg)
            refs[digest(e)].append({'snapshot_digest':digest(s),'event_id':e['event_id'],'event_digest':digest(e)})
    events=unique(all_events,check);identities=defaultdict(list);sequences=defaultdict(list)
    for e in events:
        tick(check);identities[(e['recorder_id'],e['recorder_epoch'],e['event_id'])].append(e)
        sequences[(e['recorder_id'],e['recorder_epoch'],e['sequence'])].append(e)
        if e['assignment_id'] is not None:groups[key(e)].append(e)
    tainted=set()
    for s in snapshots:
        if snapshot_reasons(s,check)&{'event-epoch-mismatch','segment-digest-mismatch'}:
            tainted.update(key(e) for e in s['events'] if e['assignment_id'] is not None)
    for values in list(identities.values())+list(sequences.values()):
        tick(check)
        if len(values)>1:tainted.update(key(e) for e in values if e['assignment_id'] is not None)
    for stream,heads in stream_heads.items():
        if len(heads)>1:
            tainted.update(k for k in groups if k[:2]==stream[:2])
    selections=defaultdict(list)
    for o in lookups:
        tick(check);selections[o['selector']['selection_id']].append(o)
    return {'snapshots':snapshots,'lookups':lookups,'events':events,'refs':refs,'groups':groups,
        'streams':{(e['recorder_id'],e['recorder_epoch']) for e in events}|{(s['manifest']['recorder_id'],s['manifest']['recorder_epoch']) for s in snapshots if s['manifest'] is not None},
        'identities':identities,'tainted':tainted,'stream_gaps':stream_gaps,'selections':selections}


def one(rows,check):
    values=unique(rows,check)
    return values[0] if len(values)==1 else None


def assignment_facts(index,check):
    facts={};intents={};decisions={}
    def event_refs(rows):return unique([r for e in rows for r in index['refs'][digest(e)]],check)
    for k,rows in sorted(index['groups'].items()):
        tick(check);reasons=set();is_=[e for e in rows if e['kind']=='assignment_intent'];ds=[e for e in rows if e['kind']=='dispatch_observed']
        a=one([e['payload'] for e in is_],check);d=one([e['payload'] for e in ds],check)
        if a is None:reasons.add('intent-missing-or-conflicting')
        else:intents[k]=(a,min(e['recorded_at'] for e in is_))
        if d is None:reasons.add('dispatch-missing-or-conflicting')
        if k in index['tainted']:reasons.add('event-identity-conflict')
        if index['stream_gaps'][k[:2]]:reasons.add('snapshot-incomplete')
        if a and d and (a['target_thread_id']!=d['child_thread_id'] or d['client_request_id'] not in (None,k[2])):reasons.add('dispatch-target-mismatch')
        if a and ds and any(e['payload']['request_observed_at']<min(i['recorded_at'] for i in is_) for e in ds):reasons.add('dispatch-before-intent')
        rs=[e for e in rows if e['kind']=='assignment_resolution'];terminals=[e['payload'] for e in rs if e['payload']['resolution'] in ('completed','failed','cancelled')]
        decision=one(terminals,check)
        if not terminals:reasons.add('decision-unavailable')
        if terminals and decision is None:reasons.add('terminal-conflict')
        for e in rows:
            tick(check)
            for dep in e['dependencies']:
                targets=index['identities'].get((k[0],k[1],dep),[])
                if len(targets)!=1 or targets[0]['sequence']>=e['sequence']:reasons.add('event-dependency-unresolved')
        for e in rs:
            p=e['payload'];tick(check)
            if p['revision']>0 and not any(old['kind']=='assignment_resolution' and old['event_id'] in e['dependencies'] and old['payload']['revision']+1==p['revision'] for old in rows):reasons.add('resolution-predecessor-unresolved')
            if p['worker_started_at'] is not None and p['worker_completed_at'] is not None and p['worker_started_at']>p['worker_completed_at']:reasons.add('worker-clock-conflict')
            if p['worker_completed_at'] is not None and p['observed_at']<p['worker_completed_at']:reasons.add('result-clock-conflict')
            if p['resolved_at'] is not None and p['resolved_at']>p['observed_at']:reasons.add('decision-clock-conflict')
            if a and (p['trigger_id']!=a['trigger_id'] or p['result_repository'] not in (None,a['repository']) or a['pr'] is not None and p['result_pr'] not in (None,a['pr'])):reasons.add('decision-target-mismatch')
            if d and p['terminal_run_id'] not in (None,d['orchestration_run_id']):reasons.add('decision-run-mismatch')
            if p in terminals and (p['worker_terminal_state']!=p['resolution'] or p['resolved_at'] is None):reasons.add('explicit-decision-unresolved')
            if a and a['role']=='review' and p['resolution']=='completed' and (p['review_outcome'] is None or p['reviewed_sha'] is None or p['reviewed_sha']!=a['reviewed_sha']):reasons.add('review-decision-unresolved')
        decisions[k]=decision
        facts[k]={'key':fields(k),'intent_event_refs':event_refs(is_),'dispatch_event_refs':event_refs(ds),
            'lookup_refs':[],'relation_refs':[],'lifecycle_dispositions':reasons,'decision_state':'unknown'}
    for k,(a,at) in sorted(intents.items()):
        tick(check);reasons=facts[k]['lifecycle_dispositions'];prior=a['supersedes_assignment_id']
        if a['revision']==0 and prior is not None or a['revision']>0 and prior is None:reasons.add('revision-predecessor-unresolved')
        if prior is not None:
            old=intents.get((k[0],k[1],prior))
            if old is None or a['revision']!=old[0]['revision']+1 or at<=old[1] or any(a[f]!=old[0][f] for f in ('task_id','repository','pr','input_sha','reviewed_sha')):reasons.add('supersession-unresolved')
        if a['trigger_kind']=='initial':
            if a['trigger_id'] is not None or a['role']=='rework':reasons.add('trigger-unresolved')
        else:
            target=(k[0],k[1],a['trigger_id']);old=intents.get(target);decision=decisions.get(target)
            if a['trigger_kind']!='review' or old is None or old[0]['role']!='review' or any(a[f]!=old[0][f] for f in ('task_id','repository','pr')) or a['input_sha'] is None or a['input_sha']!=old[0]['reviewed_sha'] or decision is None or decision['review_outcome']!='changes_requested' or decision['resolved_at'] is None or decision['resolved_at']>at:reasons.add('trigger-unresolved')
        for field in ('supersedes_assignment_id','trigger_id'):
            seen=set();cursor=k
            while cursor in intents:
                tick(check)
                if cursor in seen:reasons.add('assignment-dependency-cycle');break
                seen.add(cursor);item=intents[cursor][0]
                if field=='trigger_id' and item['trigger_kind']!='review':break
                cursor=(cursor[0],cursor[1],item[field])
    changed=True
    while changed:
        tick(check);changed=False
        for k,(a,at) in intents.items():
            for field in ('supersedes_assignment_id','trigger_id'):
                tick(check)
                if a[field] is None or field=='trigger_id' and a['trigger_kind']!='review':continue
                parent=facts.get((k[0],k[1],a[field]))
                if parent is None or parent['lifecycle_dispositions']:
                    if 'dependency-unresolved' not in facts[k]['lifecycle_dispositions']:
                        facts[k]['lifecycle_dispositions'].add('dependency-unresolved');changed=True
    for k,f in facts.items():
        tick(check)
        if decisions.get(k) is not None and not f['lifecycle_dispositions']:f['decision_state']='explicit-unverified'
        f['lifecycle_dispositions'].update(('actor-independence-unproven','primary-project-unavailable'))
    return facts,intents


def lookup_match(o,index,intents,check):
    s=o['selector'];k=key(s);reasons=set();events=index['identities'].get((k[0],k[1],s['dispatch_event_id']),[])
    d=events[0] if len(events)==1 else None;a=intents.get(k)
    if d is None or d['kind']!='dispatch_observed' or d['assignment_id']!=k[2]:reasons.add('dispatch-event-unresolved')
    else:
        p=d['payload']
        if (p['child_thread_id'],p['orchestration_run_id'],p['response_observed_at'])!=(s['target_thread_id'],s['orchestration_run_id'],s['dispatch_response_observed_at']):reasons.add('selector-dispatch-mismatch')
        if a is None or a[0]['target_thread_id']!=p['child_thread_id'] or p['client_request_id'] not in (None,k[2]):reasons.add('intent-dispatch-unresolved')
    if k in index['tainted']:reasons.add('primary-identity-conflict')
    if len(index['selections'][s['selection_id']])!=1:reasons.add('lookup-selection-conflict')
    threads=o['rows']['threads'];valid=[r for r in threads if not r['issues'] and r['columns']['thread_id']==r['payload']['id']==s['target_thread_id'] and r['columns']['project_id']==r['payload']['projectId']==s['project_id']]
    if not valid or any(r not in valid for r in threads):reasons.add('project-relation-unresolved')
    if a and a[0]['requested_owner_binding_id'] is not None:
        owners=index['identities'].get((k[0],k[1],a[0]['requested_owner_binding_id']),[])
        if len(owners)!=1 or owners[0]['kind']!='owner_bound' or key(owners[0])!=k:reasons.add('requested-owner-unresolved')
    tick(check)
    return reasons


def _protocol(b,index,intents,check):
    groups=defaultdict(list)
    for c in unique(b['caller_transcripts'],check):groups[c['invocation_id']].append(c)
    chosen={i:rows[0] for i,rows in groups.items() if len(rows)==1};reasons={i:set() for i in groups}
    def resolve(ref):
        tick(check)
        if ref is None:return None
        if ref['kind']=='event':
            rows=index['identities'].get((ref['recorder_id'],ref['recorder_epoch'],ref['event_id']),[])
            return rows[0] if len(rows)==1 and reference(rows[0],'event')==ref else None
        rows=[o for o in index['lookups'] if reference(o,ref['kind'])==ref]
        return rows[0] if len(rows)==1 else None
    bound={};nonce_groups=defaultdict(set);logical=defaultdict(set)
    for ident,rows in sorted(groups.items()):
        tick(check);r=reasons[ident]
        if len(rows)!=1:r.add('invocation-conflict');continue
        c=rows[0];q=resolve(c['request_ref']);v=resolve(c['result_ref']);bound[ident]=(q,v)
        if c['producer_version']!=PRODUCER:r.add('producer-version-mismatch')
        budget=BUDGETS.get((c['stage'],c['host'],c['route']))
        if budget is None:r.add('host-route-mismatch')
        elif c['elapsed_ms']>budget:r.add('deadline-exceeded')
        if c['observed_at'] is None:r.add('observation-clock-unavailable')
        elif c['observed_at']<c['started_at']:r.add('caller-clock-reversed')
        if c['terminal']!='completed' or c['exit_code']!=0 or c['outcome']!='persisted' or c['failure_code'] is not None:r.add('terminal-result-unavailable')
        if c['result_nonce']!=c['request_nonce']:r.add('nonce-response-mismatch')
        if q is None or v is None:r.add('reference-unresolved')
        components=set();binding_groups=defaultdict(set)
        for binding in c['source_bindings']:
            tick(check);components.add(binding['component'])
            binding_groups[(binding['component'],binding['before_identity']['module'])].add(digest(binding))
            if binding['version']!=VERSIONS[binding['component']]:r.add('source-version-mismatch')
            if binding['before_identity']!=binding['after_identity']:r.add('source-lease-changed')
        if any(len(rows)>1 for rows in binding_groups.values()):r.add('source-binding-conflict')
        expected={'capture','guarded-orch'} if c['stage']=='dispatch' else {'owner-lookup'}
        if components!=expected or not c['source_bindings']:r.add('source-bindings-unavailable')
        if c['stage']=='dispatch':
            if c['parent_invocation_id'] is not None:r.add('dispatch-parent-invalid')
            if q is None or v is None or c['request_ref']['kind']!='event' or c['result_ref']['kind']!='event' or q['kind']!='assignment_intent' or v['kind']!='dispatch_observed' or key(q)!=key(v):r.add('dispatch-reference-mismatch')
            elif q['payload']['target_thread_id']!=v['payload']['child_thread_id'] or c['started_at']>v['payload']['request_observed_at'] or c['observed_at'] is None or c['observed_at']<v['payload']['response_observed_at']:r.add('dispatch-clock-or-target-mismatch')
            chain=ident
        else:
            if q is None or v is None or c['request_ref']['kind']!='selector' or c['result_ref']['kind']!='lookup' or q['selector']!=v['selector']:r.add('lookup-reference-mismatch')
            else:
                r.update(lookup_match(v,index,intents,check));leases=v['source']['observations']['code_leases']
                projected=[{'module':x['module'],'device':x['device'],'inode':x['inode'],'bytes':x['bytes']} for x in leases]
                bindings=c['source_bindings']
                if not leases or len(bindings)!=len(leases) or len({x['before_identity']['module'] for x in bindings})!=len(leases):r.add('source-digest-unbound')
                for binding in bindings:
                    matches=[x for x in leases if x['module']==binding['before_identity']['module']]
                    if len(matches)!=1 or binding['before_identity'] not in projected or binding['digest']!=matches[0]['sha256']:r.add('source-digest-mismatch')
                if c['host']=='rig':
                    at=v['source']['acquisition_completed_at']
                    if at is None or c['observed_at'] is None or at>c['observed_at']:r.add('source-clock-unavailable')
            parent=chosen.get(c['parent_invocation_id'])
            if parent is None:r.add('parent-unresolved')
            elif c['host']=='root-mac':
                if parent['stage']!='dispatch' or parent['host']!='root-mac':r.add('lookup-parent-mismatch')
                elif v is not None and 'selector' in v:
                    dispatch=resolve(parent['result_ref']);s=v['selector']
                    if dispatch is None or 'payload' not in dispatch or reference(dispatch,'event')!={**c['request_ref'],'kind':'event','digest':digest(dispatch),'selection_id':None} or c['started_at']<s['dispatch_response_observed_at']:r.add('lookup-parent-binding-mismatch')
                logical[(c['request_ref']['recorder_id'],c['request_ref']['recorder_epoch'],c['request_ref']['event_id'])].add(ident)
            else:
                if parent['stage']!='lookup' or parent['host']!='root-mac' or parent['request_ref']!=c['request_ref'] or parent['result_ref']!=c['result_ref'] or parent['request_nonce']!=c['request_nonce']:r.add('rig-parent-binding-mismatch')
            chain=parent['invocation_id'] if c['host']=='rig' and parent is not None and parent['stage']=='lookup' and parent['host']=='root-mac' else ident
        nonce_groups[c['request_nonce']].add(chain)
    for ids in logical.values():
        if len(ids)>1:
            for ident in ids:reasons[ident].add('duplicate-logical-lookup')
    for chains in nonce_groups.values():
        if len(chains)>1:
            for ident,c in chosen.items():
                if c['request_nonce'] in nonce_groups and nonce_groups[c['request_nonce']]==chains:reasons[ident].add('nonce-cross-call-reuse')
    rig_groups=defaultdict(list)
    for ident,c in chosen.items():
        if c['stage']=='lookup' and c['host']=='rig':rig_groups[c['parent_invocation_id']].append(ident)
    for parent,children in rig_groups.items():
        if len(children)>1:
            for ident in children:reasons[ident].add('duplicate-rig-lookup')
    for ident,c in chosen.items():
        tick(check)
        if c['stage']=='lookup' and c['host']=='root-mac' and not any(d['stage']=='lookup' and d['host']=='rig' and d['parent_invocation_id']==ident for d in chosen.values()):reasons[ident].add('rig-call-unavailable')
    changed=True
    while changed:
        tick(check);changed=False
        for ident,c in chosen.items():
            if c['stage']=='lookup' and c['host']=='root-mac':
                children=rig_groups.get(ident,[])
                if (len(children)!=1 or reasons[children[0]]) and 'rig-call-refused' not in reasons[ident]:
                    reasons[ident].add('rig-call-refused');changed=True
            parent=c['parent_invocation_id']
            if parent is not None and (parent not in reasons or reasons[parent]) and 'parent-refused' not in reasons[ident]:reasons[ident].add('parent-refused');changed=True
    return [{'invocation_id':i,'call_digests':sorted(digest(c) for c in rows),'state':'refused' if reasons[i] else 'consistent-supplied',
        'reasons':sorted(reasons[i]),'authentication':'not-performed'} for i,rows in sorted(groups.items())]


def validate_protocol(value,*,check=lambda:None):
    b=accept_input(value,check=check);index=index_input(b,check);_,intents=assignment_facts(index,check)
    return _protocol(b,index,intents,check)


def compose(value,*,check=lambda:None):
    b=accept_input(value,check=check);index=index_input(b,check);facts,intents=assignment_facts(index,check)
    relations={};gaps={'authentication-not-performed','child-inventory-unknown','retention-unproven','usage-unallocated','global-inventory-unknown'};shared=defaultdict(set)
    for s in index['snapshots']:
        gaps.update(snapshot_reasons(s,check))
        if s['as_of'] is not None and s['as_of']>b['composed_at']:gaps.add('primary-after-composed-at')
        for e in s['events']:
            if e['kind']=='coverage_checkpoint':
                gaps.update(e['payload']['explicit_gaps'])
                if e['payload']['pending_assignment_ids'] or e['payload']['pending_operation_ids']:gaps.add('checkpoint-pending-identities')
    for o in index['lookups']:
        tick(check);s=o['selector'];k=key(s);lr={'selection_id':s['selection_id'],'digest':digest(o)}
        if k not in facts:facts[k]={'key':fields(k),'intent_event_refs':[],'dispatch_event_refs':[],'lookup_refs':[],'relation_refs':[],'lifecycle_dispositions':{'intent-unavailable','decision-unavailable','primary-project-unavailable'},'decision_state':'unknown'}
        facts[k]['lookup_refs'].append(lr);base_reasons=lookup_match(o,index,intents,check)
        shared[(s['project_id'],s['target_thread_id'],s['orchestration_run_id'])].add(k)
        gaps.update(o['gaps'])
        for c in o['candidates'] or [None]:
            tick(check);reasons=set(base_reasons)
            if c is None:reasons.add('candidate-unavailable')
            else:
                reasons.update(c['reasons'])
                if c['child_thread_id']!=s['target_thread_id'] or c['orchestration_run_id']!=s['orchestration_run_id'] or c['attempt_run_id']!=s['orchestration_run_id'] or c['turn_attempt_id'] is not None and c['turn_attempt_id']!=c['attempt_id']:reasons.add('candidate-scope-mismatch')
                if any(c[f] is None for f in ('attempt_id','provider_turn_id','native_session_id','native_turn_id')):reasons.add('native-relation-unavailable')
                a=intents.get(k)
                if a and a[0]['requested_owner_binding_id'] is not None and 'requested-owner-unresolved' not in reasons:
                    bound=index['identities'][(k[0],k[1],a[0]['requested_owner_binding_id'])][0]['payload']
                    if any(bound[f]!=c[f] for f in ('child_thread_id','orchestration_run_id','attempt_id','attempt_run_id','turn_attempt_id','turn_session_id','native_session_id','native_turn_id','provider_namespace','task_id_from_transport','actor_binding_id')):reasons.add('requested-owner-payload-mismatch')
            rk={'assignment':fields(k),'selection_id':s['selection_id'],'attempt_id':c['attempt_id'] if c else None,
                'provider_turn_id':c['provider_turn_id'] if c else None,'candidate_id':c['id'] if c else None};rid=digest(rk)
            if rid not in relations:relations[rid]={'id':rid,'key':rk,'raw_candidate_refs':[],'source_row_refs':[],'relation_state':'unresolved','reasons':set(),'attribution_state':'unallocated'}
            r=relations[rid];r['reasons'].update(reasons)
            if c:
                r['raw_candidate_refs'].append({'lookup':lr,'candidate_digest':digest(c)});r['source_row_refs'].extend(c['original_source_refs'])
            facts[k]['relation_refs'].append(rid)
    shared_groups=[members for members in shared.values() if len(members)>1]
    for r in relations.values():
        tick(check);r['raw_candidate_refs']=unique(r['raw_candidate_refs'],check);r['source_row_refs']=unique(r['source_row_refs'],check)
        if any(key(r['key']['assignment']) in members for members in shared_groups):r['reasons'].add('shared-run-membership-unallocated')
        unresolved={'multiple-owner-candidates','shared-run-membership-unallocated'}
        significant=r['reasons']-unresolved
        r['relation_state']='conflict' if any('conflict' in x or 'mismatch' in x for x in significant) else 'unresolved' if significant else 'structurally-matched'
        r['reasons']=sorted(r['reasons'])
    for k,f in facts.items():
        tick(check);f['lookup_refs']=unique(f['lookup_refs'],check);f['relation_refs']=sorted(set(f['relation_refs']));f['lifecycle_dispositions']=sorted(f['lifecycle_dispositions'])
        if not f['lookup_refs']:f['lifecycle_dispositions']=sorted(set(f['lifecycle_dispositions'])|{'lookup-unavailable'})
    protocol=_protocol(b,index,intents,check)
    gaps.add('supplied-protocol-only')
    for k,f in facts.items():
        tick(check)
        calls=[c for c in b['caller_transcripts'] if key(c['request_ref'])==k]
        if not calls:f['lifecycle_dispositions']=sorted(set(f['lifecycle_dispositions'])|{'caller-protocol-unavailable'})
    rawrows=[r for o in b['lookup_observations'] for rows in o['rows'].values() for r in rows]
    candidates=[c for o in b['lookup_observations'] for c in o['candidates']]
    events=[e for s in b['primary_snapshots'] for e in s['events']]
    denominators={'primary_occurrences':len(b['primary_snapshots']),'primary_unique':len(index['snapshots']),
        'event_occurrences':len(events),'event_unique':len(index['events']),'streams':len(index['streams']),
        'assignments':len(facts),'dispatch_occurrences':sum(e['kind']=='dispatch_observed' for e in events),
        'dispatch_unique':sum(e['kind']=='dispatch_observed' for e in index['events']),'lookup_occurrences':len(b['lookup_observations']),
        'lookup_unique':len(index['lookups']),'lookup_selections':len(index['selections']),'lookup_row_occurrences':len(rawrows),
        'lookup_rows_unique':len(unique(rawrows,check)),'invalid_lookup_rows':sum(bool(r['issues']) for r in rawrows),
        'candidate_occurrences':len(candidates),'candidate_unique':len(unique(candidates,check)),'edges':len(relations),
        **{name:sum(r['relation_state']==state for r in relations.values()) for name,state in [('matched_edges','structurally-matched'),('conflicting_edges','conflict'),('unresolved_edges','unresolved')]},
        'multi_owner_assignments':sum(len(f['relation_refs'])>1 for f in facts.values()),'shared_run_groups':len(shared_groups),
        'capped_lookup_occurrences':sum(o['inventory']['truncated'] for o in b['lookup_observations']),
        'unread_lookup_tables':sum(v!='read' for o in b['lookup_observations'] for v in o['source']['observations']['table_states'].values()),
        'unresolved_decisions':sum(f['decision_state']=='unknown' for f in facts.values()),'call_occurrences':len(b['caller_transcripts']),
        'call_unique':len(unique(b['caller_transcripts'],check)),'refused_invocations':sum(p['state']=='refused' for p in protocol),'authenticated_rows':0}
    dispositions=[]
    for scope,rows,id_fn,reason_fn in [('assignment',list(facts.values()),lambda x:digest(x['key']),lambda x:x['lifecycle_dispositions']),
            ('relation',list(relations.values()),lambda x:x['id'],lambda x:x['reasons']),
            ('protocol',protocol,lambda x:digest(x['invocation_id']),lambda x:x['reasons'])]:
        for row in rows:
            tick(check)
            for reason in reason_fn(row):dispositions.append({'scope':scope,'identity':id_fn(row),'reason':reason})
    result={'schema':'flightops-primary-owner-composition/1','input':deepcopy(b),'input_digest':digest(b),'composed_at':b['composed_at'],
        'assignments':unique(list(facts.values()),check),'relations':unique(list(relations.values()),check),'protocol_results':protocol,
        'dispositions':unique(dispositions,check),'denominators':denominators,'gaps':sorted(gaps),'authentication':'not-performed',
        'merge_authority':False,'normalized_rows':[]}
    tick(check);validate(result,OUTPUT);require(len(encode(result))<=MAX_BYTES,'primary-owner-output-bound');tick(check)
    return result
