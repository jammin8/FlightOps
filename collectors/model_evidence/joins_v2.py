from collections import defaultdict
from copy import deepcopy

from . import join_contract as v1
from .capture_projection import project
from .contract import digest, encode, require
from .schema import validate
from .join_contract_v2 import HEAD, accept_input, validated_output
from .joins import deduplicate, check_scope, ci_for_head


def unique(rows):
    return [row for _, row in sorted({encode(r): deepcopy(r) for r in rows}.items())]


def empty_input(*, generated_at, agent_evidence=None):
    return {'schema': 'evidence-head-input/2', 'origin': 'supplied', 'generated_at': generated_at, 'agent_evidence': deepcopy(agent_evidence),
        'sources': [], 'heads': [], 'policies': [], 'ci_attempts': [], 'selected_ci_inputs': [], 'boundary_snapshots': []}


def from_v1(value):
    b = v1.accept_input(value)
    require(b['agent_evidence'] == value['agent_evidence'], 'join-v2-v1-agent-not-exact')
    out = empty_input(generated_at=b['generated_at'], agent_evidence=b['agent_evidence'])
    for key in ('sources', 'policies', 'ci_attempts'):
        out[key] = b[key]
    out['heads'] = [{**h, 'predecessor_revision': None} for h in b['heads']]
    require(not b['operations'] and not b['assignments'], 'join-v2-v1-receipts-require-original-boundaries')
    return accept_input(out)


def normalize_pr_record(record, repository, evidence, *, policy_id=None, policy_read=None):
    require(type(record) is dict and type(record.get('number')) is int and record['number'] > 0, 'pr-record')
    require(type(record.get('heads')) is list and type(record.get('pr')) is str
        and record['pr'].rsplit('#', 1)[-1] == str(record['number']), 'pr-binding')
    r0, decided = record.get('r0') or {}, record.get('first_decided') or {}
    result = []
    for h in record['heads']:
        is_r0 = h['sha'] == r0.get('sha')
        row = {'id': 'head-' + digest([repository, record['number'], h['sha']])[:32], 'revision': 0,
            'predecessor_revision': None, 'evidence': deepcopy(evidence), 'repository': repository, 'pr': record['number'],
            'sha': h['sha'], 'anchor': record['anchor'], 'anchor_kind': record['anchor_kind'] or 'unknown',
            **{k: deepcopy(h[k]) for k in ('observed', 'read', 'refresh_failed', 'complete', 'sync', 'replaced', 'first_attempt', 'first_completed', 'latest')},
            'r0_status': r0.get('status', 'unknown') if is_r0 else 'unknown', 'r0_receipt': r0.get('receipt') if is_r0 else None,
            'r0_basis': {'opened event': 'opened-event', 'push events': 'push-events'}.get(r0.get('evidence'), 'workflow') if is_r0 else 'unknown',
            'policy_id': policy_id, 'policy_source': record['policy_source'], 'policy_read': policy_read,
            'required': deepcopy(record['required']), 'first_decided': h['sha'] == decided.get('sha'),
            'gaps': sorted(set(record['gaps'] + record['incomplete'] + h['why']))}
        validate(row, HEAD)
        result.append(row)
    return result


def from_selected_ci(raw, *, generated_at, agent_evidence=None):
    from .selected_ci import normalize
    from .selected_ci_contract import REPOSITORY, SHA
    n = normalize(raw, generated_at=generated_at)
    out = empty_input(generated_at=generated_at, agent_evidence=agent_evidence)
    for key in ('sources', 'policies', 'ci_attempts'):
        out[key] = n[key]
    out['selected_ci_inputs'] = [deepcopy(n['observations'])]
    evidence = deepcopy(n['policies'][0]['evidence'])
    unknown = {'outcome': 'unknown', 'reason': None, 'failed': []}
    out['heads'] = [{'id': 'head-' + digest([REPOSITORY, 8383, SHA])[:32], 'revision': 0, 'predecessor_revision': None,
        'evidence': evidence, 'repository': REPOSITORY, 'pr': 8383, 'sha': SHA, 'anchor': None, 'anchor_kind': 'unknown',
        'observed': None, 'read': n['observations']['head']['fetched'], 'refresh_failed': None, 'complete': False, 'sync': None,
        'r0_status': 'unknown', 'r0_receipt': None, 'r0_basis': 'unknown', 'policy_id': n['policies'][0]['id'],
        'policy_source': 'rules', 'policy_read': n['observations']['policy']['read'], 'required': n['assessment']['protected_contexts'],
        'replaced': None, 'first_attempt': deepcopy(unknown), 'first_completed': deepcopy(unknown), 'latest': deepcopy(unknown),
        'first_decided': False, 'gaps': ['first-ready-unavailable', 'historical-policy-unavailable']}]
    return accept_input(out)


def canonical_input(b):
    out = deepcopy(b)
    for key in ('sources', 'heads', 'policies', 'ci_attempts', 'selected_ci_inputs'):
        out[key] = unique(out[key])
    for s in out['boundary_snapshots']:
        s['events'] = unique(s['events'])
        s['gaps'] = sorted(set(s['gaps']))
    out['boundary_snapshots'] = unique(out['boundary_snapshots'])
    return out


def one(values):
    rows = unique(values)
    return rows[0] if len(rows) == 1 else None


def heads_projection(b, note):
    groups = defaultdict(list)
    for h in b['heads']:
        groups[h['id']].append(h)
    results = []
    for identity, rows in sorted(groups.items()):
        rev = max(h['revision'] for h in rows)
        chosen = one([r for r in rows if r['revision'] == rev])
        reasons = set()
        if chosen is None or len({(r['repository'], r['pr'], r['sha']) for r in rows}) != 1:
            reasons.add('head-identity-conflict')
        for h in rows:
            if (h['revision'] == 0 and h['predecessor_revision'] is not None or h['revision'] > 0 and
                (h['predecessor_revision'] != h['revision'] - 1 or
                 len(unique([r for r in rows if r['revision'] == h['predecessor_revision']])) != 1)):
                reasons.add('head-revision-unresolved')
        if chosen is not None:
            same = [h for h in b['heads'] if (h['repository'], h['pr'], h['sha']) == (chosen['repository'], chosen['pr'], chosen['sha']) and h['id'] != identity]
            if chosen['replaced'] is not None and any(h['replaced'] is not None and h['replaced'] != chosen['replaced'] for h in same):
                reasons.add('head-replacement-conflict')
        if reasons:
            state, current, row = 'conflict', None, None
        elif chosen['replaced'] is None:
            state, current, row = 'unknown', None, None
            reasons.add('replacement-state-unknown')
        else:
            state = 'replaced' if chosen['replaced'] else 'current'
            current = not chosen['replaced']
            row = {k: deepcopy(v) for k, v in chosen.items() if k != 'predecessor_revision'}
            validate(row, v1.HEAD)
        for reason in reasons:
            note('heads', identity, reason)
        results.append({'id': identity, 'revision': rev, 'replacement_state': state, 'known_current': current,
            'reasons': sorted(reasons), 'v1_row': row})
    return results


def assignment_projection(events, capture, heads, ci, agent, check):
    groups = defaultdict(list)
    event_ids = defaultdict(list)
    for e in events:
        event_ids[e['event_id']].append(e)
        if e['assignment_id'] is not None:
            groups[e['assignment_id']].append(e)
    by_event = {key: rows[0] for key, rows in event_ids.items() if len(rows) == 1}
    base = {a['assignment_id']: a for a in capture['assignments']}
    bad_events = {d['identity'] for d in capture['dispositions']}
    facts, intents = {}, {}
    for identity, rows in sorted(groups.items()):
        check()
        candidates = unique([e['payload'] for e in rows if e['kind'] == 'assignment_intent'])
        a = one(candidates)
        if a is not None:
            intents[identity] = a
        ds = unique([e['payload'] for e in rows if e['kind'] == 'dispatch_observed'])
        owners = unique([e['payload'] for e in rows if e['kind'] == 'owner_bound'])
        rs = unique([e['payload'] for e in rows if e['kind'] == 'assignment_resolution'])
        at = min((e['recorded_at'] for e in rows if e['kind'] == 'assignment_intent'), default=None)
        reasons, fatal = set(), set()
        if len({(e['recorder_id'], e['recorder_epoch']) for e in rows}) > 1:
            fatal.add('assignment-cross-source-ambiguous')
        if a is None:
            fatal.add('intent-missing-or-conflicting')
        elif a['revision'] > 0 and a['supersedes_assignment_id'] is None:
            fatal.add('intent-revision-predecessor-unresolved')
        if any(e['event_id'] in bad_events for e in rows):
            fatal.add('event-dependency-or-identity-conflict')
        d = one(ds)
        if len(ds) > 1:
            fatal.add('dispatch-conflict')
        if d is None:
            reasons.add('dispatch-unavailable')
        elif a is not None and (d['child_thread_id'] != a['target_thread_id'] or d['client_request_id'] not in (None, identity)):
            fatal.add('dispatch-target-mismatch')
        if d is not None and at is not None and d['request_observed_at'] < at:
            fatal.add('dispatch-before-intent')
        owner = owners[0] if len(owners) == 1 and base[identity]['owner_state'] == 'structurally-matched-unverified' else None
        if owners and owner is None:
            fatal.add('native-owner-mismatch-or-conflict')
        if owner is None:
            reasons.add('native-owner-unavailable')
        if a is not None and a['requested_owner_binding_id'] is not None:
            bound = by_event.get(a['requested_owner_binding_id'])
            if bound is None or bound['assignment_id'] != identity or bound['kind'] != 'owner_bound' or bound['payload'] != owner:
                fatal.add('requested-owner-mismatch')
        if fatal & {'assignment-cross-source-ambiguous', 'requested-owner-mismatch'}:
            owner = None
        actor = a['actor_binding_id'] if a is not None else None
        if owner is not None and actor is not None and owner['actor_binding_id'] is not None and actor != owner['actor_binding_id']:
            fatal.add('actor-binding-conflict')
        if actor is None:
            reasons.add('actor-unavailable')
        if owner is not None and agent is not None:
            run = next((r for r in agent['runs'] if r['id'] == 'codex:' + owner['attempt_id']), None)
            if run is None or run['native_session_id'] != owner['native_session_id'] or owner['native_turn_id'] not in {t['native_turn_id'] for t in run['turns']}:
                reasons.add('agent-owner-unresolved')
        terminals = [r for r in rs if r['resolution'] in ('completed', 'failed', 'cancelled')]
        signature = lambda r: {k: r[k] for k in ('resolution', 'result_repository', 'result_pr', 'result_sha', 'reviewed_sha', 'review_outcome', 'terminal_run_id', 'worker_terminal_state', 'worker_started_at', 'worker_completed_at', 'resolved_at')}
        if len(unique([signature(r) for r in terminals])) > 1:
            fatal.add('old-terminal-conflict')
        for e in rows:
            if e['kind'] != 'assignment_resolution':
                continue
            p = e['payload']
            if p['revision'] > 0 and not any(old['event_id'] in e['dependencies'] and old['kind'] == 'assignment_resolution'
                    and old['assignment_id'] == identity and old['payload']['revision'] + 1 == p['revision']
                    and old['recorded_at'] < e['recorded_at'] for old in rows):
                fatal.add('resolution-predecessor-unresolved')
            if len(unique([r for r in rs if r['revision'] == p['revision']])) > 1:
                fatal.add('resolution-revision-conflict')
            if p['worker_started_at'] is not None and p['worker_completed_at'] is not None and p['worker_started_at'] > p['worker_completed_at']:
                fatal.add('worker-clock-conflict')
            if p['resolved_at'] is not None and p['resolved_at'] > p['observed_at']:
                fatal.add('decision-clock-conflict')
            if p['worker_completed_at'] is not None and p['observed_at'] < p['worker_completed_at']:
                fatal.add('result-observed-before-completion')
            run_id = owner['orchestration_run_id'] if owner is not None else d['orchestration_run_id'] if d is not None else None
            if run_id is not None and p['terminal_run_id'] is not None and p['terminal_run_id'] != run_id:
                fatal.add('resolution-owner-mismatch')
            if a is not None:
                if p['trigger_id'] != a['trigger_id']:
                    fatal.add('resolution-trigger-mismatch')
                if (p['result_repository'] is not None and p['result_repository'] != a['repository'] or
                    p['result_pr'] is not None and a['pr'] is not None and p['result_pr'] != a['pr']):
                    fatal.add('resolution-target-mismatch')
                if a['role'] == 'review' and p['resolution'] == 'completed' and (p['review_outcome'] is None or
                        p['reviewed_sha'] is None or p['reviewed_sha'] != a['reviewed_sha']):
                    fatal.add('explicit-review-decision-missing')
            if p['resolution'] in ('completed', 'failed', 'cancelled') and (p['worker_terminal_state'] != p['resolution'] or p['resolved_at'] is None):
                fatal.add('explicit-work-decision-missing')
        decision = max(terminals, key=lambda r: r['revision']) if terminals and not fatal else None
        if not terminals:
            reasons.add('explicit-decision-unavailable')
        selected_sha = one([r['result_sha'] for r in terminals])
        reviewed_sha = a['reviewed_sha'] if a is not None else one([r['reviewed_sha'] for r in terminals])
        facts[identity] = {'id': identity, 'intents': candidates, 'intent_at': at, 'dispatches': ds, 'owners': owners,
            'resolutions': rs, 'owner': owner, 'decision': decision, 'repository': a['repository'] if a else None,
            'pr': a['pr'] if a else None, 'result_repository': one([r['result_repository'] for r in terminals]),
            'result_pr': one([r['result_pr'] for r in terminals]), 'input_sha': a['input_sha'] if a else None, 'result_sha': selected_sha,
            'reviewed_sha': reviewed_sha, 'actor_id': actor, 'fatal': fatal, 'reasons_set': reasons}
    successors = set()
    for identity, a in intents.items():
        check()
        f = facts[identity]
        for field in ('supersedes_assignment_id', 'trigger_id'):
            seen, cursor = set(), identity
            while cursor in intents:
                check()
                if cursor in seen:
                    f['fatal'].add('assignment-dependency-cycle')
                    break
                seen.add(cursor)
                item = intents[cursor]
                if field == 'trigger_id' and item['trigger_kind'] != 'review':
                    break
                cursor = item[field]
        prior = a['supersedes_assignment_id']
        if prior is not None:
            old = intents.get(prior)
            valid = old is not None and all(a[k] == old[k] for k in ('task_id', 'repository', 'pr', 'input_sha', 'reviewed_sha'))
            valid = valid and a['input_sha'] is not None and a['revision'] == old['revision'] + 1 and facts[prior]['intent_at'] < f['intent_at']
            if valid and not f['fatal'] and not facts[prior]['fatal']:
                successors.add(identity)
            else:
                f['fatal'].add('supersession-unresolved')
        if a['trigger_kind'] == 'initial':
            if a['trigger_id'] is not None or a['role'] == 'rework':
                f['fatal'].add('rework-or-trigger-unresolved')
        elif a['trigger_kind'] == 'review':
            trigger = facts.get(a['trigger_id'])
            original = intents.get(a['trigger_id'])
            valid = original is not None and original['role'] == 'review' and all(a[k] == original[k] for k in ('task_id', 'repository', 'pr'))
            valid = valid and a['input_sha'] is not None and a['pr'] is not None and a['input_sha'] == original['reviewed_sha']
            decision = trigger['decision'] if trigger is not None else None
            if not valid or decision is None or decision['review_outcome'] != 'changes_requested' or decision['resolved_at'] > f['intent_at']:
                f['fatal'].add('review-trigger-unresolved')
        else:
            candidates = [c for c in ci if c['id'] == a['trigger_id'] and c['repository'] == a['repository'] and c['sha'] == a['input_sha']]
            valid = len(candidates) == 1 and a['pr'] is not None and any(h['repository'] == a['repository'] and h['pr'] == a['pr'] and h['sha'] == a['input_sha'] for h in heads)
            if valid:
                c = candidates[0]
                valid = (c['status'] == 'COMPLETED' and c['conclusion'] in ('FAILURE', 'ERROR', 'TIMED_OUT') and c['completed_at'] is not None
                    and c['completed_at'] <= f['intent_at'] and c['history_complete'] and all(c[k] is not None for k in ('check_id', 'suite_id', 'app_id', 'workflow_run_id', 'event', 'run_attempt')))
            if not valid:
                f['fatal'].add('ci-trigger-unresolved')
    for f in facts.values():
        a = intents.get(f['id'])
        if a is not None and a['role'] == 'review':
            actors = {other['actor_id'] for other in facts.values() if other['id'] != f['id'] and other['decision'] is not None
                and intents.get(other['id'], {}).get('role') in ('implementation', 'rework') and other['repository'] == f['repository']
                and other['pr'] == f['pr'] and other['result_sha'] is not None and other['result_sha'] == f['reviewed_sha']}
            if f['actor_id'] is not None and f['actor_id'] in actors:
                f['fatal'].add('same-actor-review')
            if not actors or None in actors or f['actor_id'] is None:
                f['reasons_set'].add('review-independence-unavailable')
    for _ in range(len(facts) + 1):
        check()
        changed = False
        for identity, a in intents.items():
            dependencies = [a['supersedes_assignment_id']] if a['supersedes_assignment_id'] is not None else []
            if a['trigger_kind'] == 'review':
                dependencies.append(a['trigger_id'])
            if any(prior not in facts or facts[prior]['fatal'] for prior in dependencies) and 'dependency-unqualified' not in facts[identity]['fatal']:
                facts[identity]['fatal'].add('dependency-unqualified')
                changed = True
        if not changed:
            break
    for identity in successors:
        if not facts[identity]['fatal']:
            facts[intents[identity]['supersedes_assignment_id']]['reasons_set'].add('assignment-superseded')
    result = []
    for f in facts.values():
        check()
        fatal, reasons = f.pop('fatal'), f.pop('reasons_set')
        f['consistent'] = not bool(fatal)
        if fatal:
            f['decision'] = None
        if len(intents.get(f['id'], {}).get('memberships', [])) > 1:
            reasons.add('shared-pr-membership-allocation-unknown')
        reasons |= fatal | {'source-authentication-not-performed', 'child-inventory-unknown', 'usage-allocation-unknown'}
        for dimension in ('repository', 'pr', 'input_sha', 'result_sha'):
            if f[dimension] is None:
                reasons.add(dimension.replace('_', '-') + '-unavailable')
        def target(sha, repo, pr):
            return sorted({h['id'] for h in heads if sha is not None and repo is not None and pr is not None
                and (h['repository'], h['pr'], h['sha']) == (repo, pr, sha)})
        f['input_head_ids'] = target(f['input_sha'], f['repository'], f['pr'])
        f['result_head_ids'] = target(f['result_sha'], f['result_repository'], f['result_pr']) if f['decision'] is not None else []
        f['review_head_ids'] = target(f['reviewed_sha'], f['repository'], f['pr'])
        f['known'] = {k: f[k] is not None for k in ('repository', 'pr', 'result_repository', 'result_pr', 'input_sha', 'result_sha', 'reviewed_sha')}
        f['known'].update(actor=f['actor_id'] is not None, native_owner=f['owner'] is not None, decision=f['decision'] is not None)
        f['reasons'] = sorted(reasons)
        f['v1_row'], f['v1_dispositions'] = assignment_v1(f, groups[f['id']], intents.get(f['id']), agent)
        result.append(f)
    return sorted(result, key=lambda f: f['id'])


def assignment_v1(f, events, a, agent):
    reasons = set()
    d = one(f['dispatches'])
    p, owner = f['decision'], f['owner']
    if not f['consistent'] or p is None or a is None:
        reasons.add('v1-assignment-unresolved')
    if d is None or owner is None or f['actor_id'] is None:
        reasons.add('v1-owner-actor-or-dispatch-unavailable')
    if a is None or a['pr'] is None or a['input_sha'] is None:
        reasons.add('v1-pr-or-head-unavailable')
    run = next((r for r in (agent or {}).get('runs', []) if owner is not None and r['id'] == 'codex:' + owner['attempt_id']), None)
    if run is None or owner is None or run['native_session_id'] != owner['native_session_id'] or owner['native_turn_id'] not in {t['native_turn_id'] for t in run['turns']}:
        reasons.add('v1-agent-owner-unavailable')
    if p is not None and run is not None and (run['started_at'] is not None and f['intent_at'] < run['started_at'] or run['ended_at'] is not None and p['observed_at'] > run['ended_at']):
        reasons.add('v1-observation-outside-worker-lifetime')
    if p is not None:
        if p['result_sha'] is None or p['result_sha'] != f['input_sha']:
            reasons.add('v1-input-result-heads-differ-or-unknown')
        if p['resolution'] not in ('open', 'completed', 'failed'):
            reasons.add('v1-resolution-unrepresentable')
        if d is not None and (p['worker_started_at'] is not None and min(f['intent_at'], d['request_observed_at']) < p['worker_started_at'] or
                p['worker_completed_at'] is not None and p['observed_at'] > p['worker_completed_at']):
            reasons.add('v1-observation-outside-worker-lifetime')
    if reasons:
        return None, sorted(reasons)
    e = min((e for e in events if e['kind'] == 'assignment_resolution' and e['payload'] == p), key=lambda e: (e['recorded_at'], e['event_id']))
    row = {'id': f['id'], 'revision': p['revision'], 'evidence': {'source_id': 'boundary-' + digest([e['recorder_id'], e['recorder_epoch'], f['id']])[:24],
        'source_digest': digest(unique(events)), 'line': e['sequence'], 'at': e['recorded_at']}, 'task_id': a['task_id'],
        'run_id': 'codex:' + owner['attempt_id'], 'native_turn_id': owner['native_turn_id'], 'actor_id': f['actor_id'],
        'repository': a['repository'], 'pr': a['pr'], 'sha': a['input_sha'], 'role': a['role'],
        'assigned_at': f['intent_at'], 'resolved_at': p['observed_at'], 'resolution': p['resolution'],
        'reviewed_sha': p['reviewed_sha'], 'review_outcome': p['review_outcome'], 'trigger': a['trigger_kind'],
        'trigger_id': a['trigger_id'], 'supersedes': a['supersedes_assignment_id']}
    validate(row, v1.ASSIGNMENT)
    return row, []


def derive(value, *, check=lambda: None):
    b = canonical_input(accept_input(value, check=check))
    notes = []
    def note(collection, identity, reason):
        notes.append({'collection': collection, 'id': identity, 'reason': reason})
    head_facts = heads_projection(b, note)
    events = unique([e for s in b['boundary_snapshots'] for e in s['events']])
    ledger_gaps = sorted({g for s in b['boundary_snapshots'] for g in s['gaps']})
    capture = project(events, ledger_gaps=ledger_gaps, check=check)
    legacy_notes = []
    records = {k: deduplicate(b[k], k, legacy_notes) for k in ('policies', 'ci_attempts')}
    for d in legacy_notes:
        note(d['collection'], d['id'], d['reason'])
    conflict_ids = {d['id'] for d in legacy_notes if d['collection'] == 'ci_attempts' and 'conflict' in d['reason']}
    invalid_ci = [c for c in records['ci_attempts'] if c['started_at'] is not None and c['completed_at'] is not None and c['started_at'] > c['completed_at']]
    for c in invalid_ci:
        note('ci_attempts', c['id'], 'invalid-interval')
    records['ci_attempts'] = [c for c in records['ci_attempts'] if c not in invalid_ci]
    conflicts = {check_scope(c) for c in b['ci_attempts'] if c['id'] in conflict_ids} | {check_scope(c) for c in invalid_ci}
    sources = {s['id']: s for s in b['sources']}
    r0_receipts = defaultdict(set)
    for h in b['heads']:
        if h['r0_status'] == 'verified' and h['r0_receipt'] is not None:
            r0_receipts[(h['repository'], h['r0_receipt'])].add((h['pr'], h['sha']))
    ci = []
    for f in head_facts:
        check()
        h = next(h for h in b['heads'] if h['id'] == f['id'] and h['revision'] == f['revision'])
        c = ci_for_head(h, records, sources, conflicts)
        if f['replacement_state'] == 'conflict':
            c.update(first_attempt='unknown', first_completed='unknown', latest='unknown', verified_r0_ci_eligible=False,
                reasons=sorted(set(c['reasons']) | {'head-observation-conflict'}))
        if len(r0_receipts.get((h['repository'], h['r0_receipt']), ())) > 1:
            c.update(verified_r0_ci_eligible=False, reasons=sorted(set(c['reasons']) | {'r0-receipt-conflict'}))
        ci.append(c)
    assignments = assignment_projection(events, capture, b['heads'], records['ci_attempts'], b['agent_evidence'], check)
    operations = []
    for op in capture['operations']:
        rows = [e for e in events if e['operation_id'] == op['operation_id']]
        owned = any(e['payload'].get('process_identity', {}).get('kind') == 'owned-process'
            for e in rows if type(e['payload'].get('process_identity')) is dict) and op['known']['process']
        operations.append({'id': op['operation_id'], 'events': rows, 'terminal_state': op['terminal_state'],
            'owned_process_known': bool(owned), 'git_observations': deepcopy(op['git_observations']), 'reasons': sorted(set(op['reasons']) | {'operation-proof-unqualified'} | (set() if owned else {'owned-process-unavailable'}))})
    for a in assignments:
        for reason in a['reasons']:
            note('assignments', a['id'], reason)
    agent = b['agent_evidence']
    compatibility = {'v1_input': None, 'omitted_head_ids': sorted(h['id'] for h in head_facts if h['v1_row'] is None),
        'omitted_assignment_ids': sorted(a['id'] for a in assignments if a['v1_row'] is None), 'reasons': []}
    ci_blocked = {c['head_id'] for c in ci if 'check-history-conflict' in c['reasons']}
    compatibility['omitted_head_ids'] = sorted(set(compatibility['omitted_head_ids']) | ci_blocked)
    if ci_blocked:
        compatibility['reasons'].append('v1-ci-history-unrepresentable')
    if agent is not None and agent['schema'] == 'agent-evidence/2':
        old = {'schema': 'evidence-head-input/1', 'generated_at': b['generated_at'], 'agent_evidence': deepcopy(agent),
            'sources': deepcopy(b['sources']), 'heads': [h['v1_row'] for h in head_facts if h['v1_row'] is not None and h['id'] not in ci_blocked],
            **records, 'operations': [], 'assignments': []}
        for a in assignments:
            row = a['v1_row']
            if row is None:
                continue
            row = deepcopy(row)
            sid = row['evidence']['source_id']
            if sid in {s['id'] for s in old['sources']}:
                compatibility['reasons'].append('v1-boundary-source-collision')
                compatibility['omitted_assignment_ids'].append(a['id'])
                continue
            old['sources'].append({'id': sid, 'digest': row['evidence']['source_digest'], 'snapshot_at': row['evidence']['at'],
                'generated_at': None, 'complete_through': None, 'origin': 'supplied', 'gaps': ['source-authentication-not-performed']})
            old['assignments'].append(row)
        compatibility['v1_input'] = v1.accept_input(old)
    else:
        compatibility['reasons'].append('v1-requires-agent-evidence-2')
    agent_runs = (agent or {}).get('runs', [])
    requests = (agent or {}).get('requests', [])
    inventory = (agent or {}).get('inventory', {})
    gaps = set(capture['gaps']) | {'authentication-not-performed', 'eligible-inventory-unknown', 'child-inventory-unknown', 'retention-unknown'}
    if agent is None:
        gaps.add('agent-evidence-unavailable')
    else:
        gaps.update(inventory.get('gaps', []))
    denominators = {'selected_heads': len(head_facts), 'selected_pr_identities': len({(h['repository'], h['pr']) for h in b['heads']}),
        'selected_head_observations': len(b['heads']), 'unknown_replacement_heads': sum(h['replacement_state'] == 'unknown' for h in head_facts),
        'conflicting_heads': sum(h['replacement_state'] == 'conflict' for h in head_facts), 'selected_events': len(events),
        'selected_assignments': len(assignments), 'explicit_decisions': sum(a['decision'] is not None for a in assignments),
        'native_owner_mappings': sum(a['owner'] is not None for a in assignments), 'selected_operations': len(operations),
        'unknown_owned_processes': sum(not o['owned_process_known'] for o in operations),
        'selected_agent_runs': inventory.get('run_count', len(agent_runs)), 'selected_agent_turns': inventory.get('turn_count', sum(len(r['turns']) for r in agent_runs)),
        'accepted_turns': sum(len(r['turns']) for r in agent_runs), 'captured_responses': len(requests),
        'known_usage_responses': sum(q['usage']['complete'] and not q['conflict'] for q in requests),
        'reconciled_main_turns': sum(r['state'] == 'match' for r in (agent or {}).get('reconciliation', [])),
        'exact_pr_linked_usage': 0, 'authenticated_rows': 0, 'known_child_allocation_runs': sum(r['expected_children'] == [] for r in agent_runs),
        'retention_complete_sources': sum(s['complete_through'] is not None and s['complete_through'] >= agent['as_of'] and not s['gaps'] for s in (agent or {}).get('sources', []))}
    result = {'schema': 'evidence-head-joins/2', 'generated_at': b['generated_at'], 'input_digest': digest(b), 'input': b,
        'heads': head_facts, 'assignments': assignments, 'operations': operations, 'ci': ci, 'dispositions': unique(notes),
        'compatibility': compatibility, 'denominators': denominators, 'gaps': sorted(gaps),
        'authentication': 'not-performed', 'merge_authority': False, 'normalized_rows': []}
    check()
    return validated_output(result, check=check)
