from collections import defaultdict

from .contract import ContractError, DIMENSIONS, digest, identifier, provenance, require, scoped, stamp
from .exporter import canonical_requests, derive_health
from .native import parse_snapshot, timestamp, usage, ORCH_USAGE
from .observed_contract import accept_observed, setting
from .usage import BUCKETS, normalize


def reconcile(runs, requests, expected):
    result = []
    for r in sorted(runs, key=lambda r:r['id']):
        for t in r['turns']:
            qs = [q for q in requests if q['original_run_id']==r['id'] and q['original_native_turn_id']==t['native_turn_id']]
            b = {k:sum(q['usage']['buckets'][k] for q in qs) if qs and all(q['usage']['known'][k] and not q['conflict'] for q in qs) else None for k in BUCKETS}
            observed = {'buckets':b,'known':{k:v is not None for k,v in b.items()},
                        'input_total':sum(q['usage']['input_total'] for q in qs) if qs and all(q['usage']['input_total'] is not None and not q['conflict'] for q in qs) else None,
                        'output_total':b['output'],'complete':all(v is not None for v in b.values()),
                        'semantics':'cache-inside-input/reasoning-inside-output'}
            exp = expected.get((r['id'],t['native_turn_id']), normalize('codex',{}))
            matches = {k:b[k]==exp['buckets'][k] if b[k] is not None and exp['known'][k] else None for k in BUCKETS}
            state = 'mismatch' if False in matches.values() else 'match' if all(v is True for v in matches.values()) and t['usage_status']=='complete' else 'unknown'
            result.append({'run_id':r['id'],'native_turn_id':t['native_turn_id'],'expected':exp,'observed':observed,
                           'matches':matches,'state':state,'request_count':len(qs)})
    return result


def ordered_at_same_time(observations, marks, gaps):
    equal = defaultdict(list)
    for o in observations:
        key = tuple((k,o['settings'][k]['value'],o['settings'][k]['basis']) for k in DIMENSIONS)
        equal[key].append(o)
    before, unresolved = [], []
    for copies in equal.values():
        orders = []
        for o in copies:
            local = [row['line'] for sid,row in marks if sid==o['evidence']['source_id']]
            line = o['evidence']['line']
            order = (True if local and all(line < n for n in local) else
                     False if local and all(line > n for n in local) else None)
            orders.append(order)
        if all(order is True for order in orders):
            before.extend(copies)
        elif not all(order is False for order in orders):
            gaps.add('native-settings-order-unknown')
            unresolved.extend(copies)
    return before, unresolved


def uncertain_settings(unresolved):
    uncertain = {}
    for o in unresolved:
        for key,v in o['settings'].items():
            if v['value'] is not None:
                uncertain.setdefault(key,[]).append(v)
    return uncertain


def session_settings_before_start(observations, starts, session, gaps):
    eligible, uncertain_session = [], {}
    session_obs = [o for o in observations if o['native_session_id']==session and o['native_turn_id'] is None]
    if not starts:
        return eligible, uncertain_session
    occurrences = {digest([row['type'],row['at'],row['payload']]) for _,row in starts}
    if len(occurrences)!=1:
        gaps.add('native-start-occurrence-ambiguous')
        lower=min(row['at'] for _,row in starts)
        upper=max(row['at'] for _,row in starts)
        eligible=[o for o in session_obs if o['at'] < lower]
        unresolved=[o for o in session_obs if lower <= o['at'] <= upper]
    else:
        start_at = starts[0][1]['at']
        eligible = [o for o in session_obs if o['at'] < start_at]
        before, unresolved = ordered_at_same_time([o for o in session_obs if o['at'] == start_at], starts, gaps)
        eligible.extend(before)
    return eligible, uncertain_settings(unresolved)


def turn_settings_before_response(turn_obs, at, copies, gaps):
    before, unresolved = ordered_at_same_time([o for o in turn_obs if o['at'] == at], copies, gaps)
    return [o for o in turn_obs if o['at'] < at] + before, uncertain_settings(unresolved)


def select_reported_setting(candidates, uncertain_session, gaps):
    if not candidates:
        return None
    newest = max(c['evidence']['at'] for c in candidates)
    latest = [c for c in candidates if c['evidence']['at']==newest]
    if len({c['value'] for c in latest})!=1:
        gaps.add('native-settings-conflict')
        return None
    if uncertain_session:
        uncertain_at = max(c['evidence']['at'] for c in uncertain_session)
        if uncertain_at > newest:
            return None
        if uncertain_at == newest:
            independent = [c for c in latest if c['basis']=='native-turn-context']
            if not independent:
                return None
            if any(c['evidence']['at']==newest and c['value']!=independent[0]['value'] for c in uncertain_session):
                gaps.add('native-settings-conflict')
                return None
            return independent[0]
    return latest[0]


def derive_observed(projection, snapshots, *, projects, window, as_of, acquired_at, origin='metadata-replay', check=lambda:None):
    require(origin in ('metadata-replay','synthetic-replay'), 'replay-origin')
    require(stamp(as_of) and stamp(acquired_at) and window['start'] <= window['end']==as_of<=acquired_at, 'adapter-clock')
    require(window['end']-window['start']<=90*86400, 'adapter-window')
    require(all(isinstance(projection.get(k),list) and len(projection[k])<=500 for k in ('threads','runs','run_attempts','provider_threads','provider_turns')), 'projection-records')
    require(isinstance(snapshots,dict) and sum(len(v) for v in snapshots.values())<=32, 'snapshot-limit')
    require(sum(len(s.get('records',[])) for ss in snapshots.values() for s in ss)<=10000, 'metadata-record-limit')
    gaps = set(projection.get('gaps', [])) | {'projection-retention-unproven','native-retention-unproven'}
    sources = {'projection':{'id':'projection','digest':digest({k:projection[k] for k in ('threads','runs','run_attempts','provider_threads','provider_turns')}),
                            'snapshot_at':acquired_at,'history_start':None,'history_end':None,'complete_through':None,
                            'gaps':['projection-retention-unproven']}}
    errors, runs, observations, links, requests, expected = [], {}, [], [], [], {}
    snapshot_facts = {}
    def unique(rows):
        grouped = defaultdict(list)
        for row in rows:
            structured=('lineage','modelSelection','nativeThreadRef','nativeTurnRef','turnTokenUsage','forkedFrom')
            if (isinstance(row,dict) and identifier(row.get('id')) and
                    all(row.get(k) is None or isinstance(row[k],dict) for k in structured)):
                grouped[row['id']].append(row)
            else:
                gaps.add('invalid-projection-record')
        for key, rows in list(grouped.items()):
            if len(rows)!=1:
                del grouped[key]; gaps.add('duplicate-projection-identity')
        return {key:rows[0] for key,rows in grouped.items()}
    thread_map, run_map, providers = (unique(projection[k]) for k in ('threads','runs','provider_threads'))
    attempts, turns = unique(projection['run_attempts']), unique(projection['provider_turns'])
    for index, a in enumerate(attempts.values()):
        check()
        try:
            run, provider = run_map[a['runId']], providers[a['providerThreadId']]
            thread = thread_map[run['threadId']]
            require(thread['projectId'] in projects, 'project-membership')
            require(run['id']==a['runId'] and provider.get('appThreadId')==run['threadId'], 'projection-join')
            require(provider.get('driver')=='codex', 'unsupported-provider')
            require(run.get('providerThreadId') in (None,provider['id']), 'run-provider-reference-mismatch')
            instances=[row.get('providerInstanceId') for row in (run,a,provider)]
            require(None not in instances, 'provider-instance-unknown')
            require(len(set(instances))==1, 'provider-instance-conflict')
            if provider.get('forkedFrom') is not None:gaps.add('provider-fork-parent-unknown')
            ref = provider.get('nativeThreadRef') or {}
            session = a.get('nativeThreadId')
            require(ref.get('strength')=='strong' and ref.get('driver')=='codex' and session==ref.get('nativeId') and identifier(session), 'native-session-join')
            require(not a.get('startedAt') or timestamp(a['startedAt'])<=as_of, 'attempt-after-as-of')
            completion_after_asof=bool(a.get('completedAt') and timestamp(a['completedAt'])>as_of)
            if completion_after_asof:gaps.add('projection-after-as-of')
            rid = scoped('codex',a['id'])
            rturns = [t for t in turns.values() if t.get('runAttemptId')==a['id']]
            require(rturns, 'missing-provider-turns')
            turn_by_native = {}
            for t in rturns:
                ref = t.get('nativeTurnRef') or {}
                require(t.get('providerThreadId')==provider['id'] and ref.get('strength')=='strong' and ref.get('driver')=='codex', 'native-turn-join')
                tid = ref.get('nativeId'); require(identifier(tid) and tid not in turn_by_native, 'native-turn-id')
                turn_by_native[tid] = t
            parsed = []
            for si, snap in enumerate(snapshots.get(session, [])):
                check()
                sid = 'native-'+digest([session,si,snap['digest']])[:24]
                try:
                    rows, sgaps = parse_snapshot(snap,session,set(turn_by_native),as_of)
                    if snap.get('operations_omitted'):
                        sgaps = sorted(set(sgaps)|{'unsupported-operation-binding'})
                    times = []
                    for raw in snap['records']:
                        try:
                            at = timestamp(raw.get('timestamp'))
                            if at <= as_of: times.append(at)
                        except ContractError:
                            pass
                    require(times, 'empty-native-snapshot')
                    snapshot_facts[sid]={'source_id':sid,'native_session_id':session,'bytes_read':snap.get('bytes'),
                                         'device':snap.get('device'),'inode':snap.get('inode'),
                                         'last_line':max(row['line'] for row in rows)}
                    sources[sid] = {'id':sid,'digest':snap['digest'],'snapshot_at':max(times),
                                    'history_start':min(times),'history_end':max(times),'complete_through':None,
                                    'gaps':sorted(set(sgaps)|{'native-retention-unproven'})}
                    gaps.update(sgaps)
                    parsed.extend((sid,row) for row in rows)
                except (ContractError,KeyError,TypeError):
                    gaps.add('invalid-native-snapshot')
            if not parsed:
                gaps.add('native-source-missing')
            pe = {'source_id':'projection','line':index+1,'at':acquired_at}
            sel = run.get('modelSelection') or {}
            options = sel.get('options') or []
            require(isinstance(options,list), 'requested-options')
            opts = {}
            for o in options:
                require(isinstance(o,dict) and o.get('id') not in opts, 'requested-options')
                opts[o.get('id')] = o.get('value')
            def dim(value,basis,ev,requested=False):
                if value is None:
                    return setting(None,sources)
                return setting({'value':value,'basis':basis,'evidence':ev},sources,requested=requested)
            requested = {key:dim(value,'orch-request',pe,True) for key,value in
                         {'provider':None,'instance':sel.get('instanceId'),'model_id':sel.get('model'),
                          'effort':opts.get('reasoningEffort'),'service_tier':opts.get('serviceTier')}.items()}
            out_turns = []
            for tid,t in sorted(turn_by_native.items()):
                check()
                native = [(sid,row) for sid,row in parsed if row['payload'].get('turn_id')==tid]
                starts = [(sid,row) for sid,row in native if row['payload'].get('type')=='task_started']
                ends = [(sid,row) for sid,row in native if row['payload'].get('type')=='task_complete']
                processed = t.get('turnTokenUsage')
                turn_after_asof=bool(t.get('completedAt') and timestamp(t['completedAt'])>as_of)
                if turn_after_asof:
                    processed=None;gaps.add('projection-after-as-of')
                if not isinstance(processed,dict) or processed.get('usageScope')!='main_agent':
                    processed={}; gaps.add('processed-main-usage-unknown')
                exp = normalize('codex',usage(processed,ORCH_USAGE))
                expected[(rid,tid)] = exp
                has_children = processed.get('hasSubagents')
                require(has_children is None or type(has_children) is bool, 'child-flag')
                if has_children is not False:
                    gaps.add('expected-child-inventory-unknown')
                if not starts or not ends or t.get('status')!='completed':
                    gaps.add('native-turn-incomplete')
                reported_for_requests = []
                for sid,row in parsed:
                    p = row['payload']
                    if row['type'] not in ('session_meta','turn_context') and p.get('type')!='thread_settings_applied':
                        continue
                    if row['type']=='turn_context' and p.get('turn_id')!=tid:
                        continue
                    ev={'source_id':sid,'line':row['line'],'at':row['at']}
                    vals = ({'provider':p.get('model_provider')} if row['type']=='session_meta' else
                            {'model_id':p.get('model'),'effort':p.get('effort')} if row['type']=='turn_context' else
                            {key:p.get('thread_settings',{}).get(nativekey) for key,nativekey in
                             {'model_id':'model','provider':'model_provider_id','effort':'reasoning_effort','service_tier':'service_tier'}.items()})
                    basis={'session_meta':'native-session','turn_context':'native-turn-context','event_msg':'native-settings-applied'}[row['type']]
                    vals={key:dim(vals.get(key),basis,ev) for key in DIMENSIONS}
                    ob={'native_session_id':session,'native_turn_id':tid if row['type']=='turn_context' else None,
                        'at':row['at'],'settings':vals,'evidence':provenance(ev,sources)}
                    if ob not in observations: observations.append(ob)
                session_observations, uncertain_session = session_settings_before_start(observations,starts,session,gaps)
                turn_observations=[o for o in observations if o['native_session_id']==session and o['native_turn_id']==tid]
                response_copies=defaultdict(list)
                for sid,row in native:
                    if row['type']=='token_usage_record':response_copies[row['payload']['response_id'],row['at']].append((sid,row))
                for sid,row in native:
                    if row['type']!='token_usage_record': continue
                    current={key:setting(None,sources) for key in DIMENSIONS}
                    turn_before,uncertain_turn=turn_settings_before_response(turn_observations,row['at'],response_copies[row['payload']['response_id'],row['at']],gaps)
                    applicable=session_observations+turn_before
                    for key in DIMENSIONS:
                        candidates=[o['settings'][key] for o in applicable if o['settings'][key]['value'] is not None]
                        selected = select_reported_setting(candidates,[*uncertain_session.get(key,()),*uncertain_turn.get(key,())],gaps)
                        if selected is not None:
                            current[key] = selected
                    reported_for_requests.append(current)
                    ev={'source_id':sid,'line':row['line'],'at':row['at']}
                    if len(requests)>=5000:
                        gaps.add('request-observation-limit');break
                    requests.append({'provider_namespace':'codex','native_request_id':row['payload']['response_id'],
                                     'original_native_run_id':a['id'],'original_native_turn_id':tid,
                                     'revision':int(row['at']*1000),'usage':usage(row['payload']['usage']),
                                     'reported':{k:v if v['value'] is not None else None for k,v in current.items()},'evidence':ev})
                aggregate={key:setting(None,sources) for key in DIMENSIONS}
                for key in DIMENSIONS:
                    vals=[p[key] for p in reported_for_requests]
                    if vals and all(v['value'] is not None and v['value']==vals[0]['value'] for v in vals):aggregate[key]=vals[0]
                out_turns.append({'native_turn_id':tid,'requested':requested,'reported':aggregate,'usage_scope':'main_agent' if processed else 'unknown',
                                  'usage_status':(processed.get('usageStatus') if starts and ends and t.get('status')=='completed' and processed.get('usageStatus') in ('complete','partial','unavailable') else 'partial'),
                                  'has_subagents':has_children})
            runs[rid]={'id':rid,'native_run_id':run['id'],'attempt_id':a['id'],'native_session_id':session,'provider_namespace':'codex',
                       'parent':None,'status':'complete' if a.get('status')=='completed' and not completion_after_asof else 'in_progress' if completion_after_asof else 'in_progress' if a.get('status') in ('running','starting') else 'unknown',
                       'started_at':timestamp(a['startedAt']) if a.get('startedAt') else None,'ended_at':timestamp(a['completedAt']) if a.get('completedAt') and not completion_after_asof else None,
                       'turns':out_turns,'expected_children':[] if all(t['has_subagents'] is False for t in out_turns) else None,
                       'memberships':[],'allocation':'unknown','evidence':provenance(pe,sources)}
            if (thread.get('lineage') or {}).get('parentThreadId'):
                gaps.add('parent-turn-binding-unknown')
            if any((th.get('lineage') or {}).get('parentThreadId')==thread['id'] for th in thread_map.values()):
                gaps.add('child-turn-binding-unknown');runs[rid]['expected_children']=None
            links.append({'run_id':rid,'thread_id':thread['id'],'project_id':thread['projectId'],'provider_thread_id':provider['id'],
                          'attempt_id':a['id'],'native_session_id':session,
                          'parent_thread_id':(thread.get('lineage') or {}).get('parentThreadId'),
                          'child_thread_ids':sorted(th['id'] for th in thread_map.values() if (th.get('lineage') or {}).get('parentThreadId')==thread['id'])})
        except (ContractError,KeyError,TypeError) as exc:
            errors.append({'collection':'runs','index':index,'code':str(exc) if isinstance(exc,ContractError) else 'projection-join'})
            gaps.add('unsupported-run-or-attempt')
    canonical = canonical_requests(requests,sources,runs,errors,setting_reader=setting)
    reconciled = reconcile(list(runs.values()),canonical,expected)
    for rec in reconciled:
        if rec['state']!='match':gaps.add('usage-reconciliation-'+rec['state'])
    result={'schema':'agent-evidence/2','deriver_version':'flightops-orch-native/1','origin':{'kind':origin},
            'source_identity':{'snapshot_digest':digest([projection,{s:[r['digest'] for r in ss] for s,ss in snapshots.items()}]),'host':None,'basis':'orch-native-readonly'},
            'generated_at':acquired_at,'acquired_at':acquired_at,'as_of':as_of,'window':dict(window),'sources':sorted(sources.values(),key=lambda s:s['id']),
            'runs':sorted(runs.values(),key=lambda r:r['id']),'requests':canonical,'operations':[],'errors':errors,
            'snapshots':sorted(snapshot_facts.values(),key=lambda x:x['source_id']),
            'links':sorted(links,key=lambda x:x['run_id']),'settings_observations':sorted(observations,key=lambda o:(o['at'],o['evidence']['source_id'],o['evidence']['line'])),
            'reconciliation':reconciled,'inventory':{'projects':sorted(projects),'run_count':len(projection['runs']),'attempt_count':len(projection['run_attempts']),
            'turn_count':len(projection['provider_turns']),'native_snapshot_count':sum(len(v) for v in snapshots.values()),
            'compatible_turns':sum(r['state']=='match' for r in reconciled),'unsupported_turns':max(0,len(projection['provider_turns'])-sum(r['state']=='match' for r in reconciled)),
            'gaps':sorted(gaps)},'health':derive_health(list(sources.values()),list(runs.values()),canonical,errors,as_of,window)}
    check()
    return accept_observed(result)
