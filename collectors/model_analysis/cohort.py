
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import timedelta
import re
import time

from .contract import (DIGEST, SHA, REPO, MANIFEST_SCHEMA, POLICY_VERSION, digest,
                       number, require, text, timestamp, unique, validate)


def size_bin(lines: int) -> str:

    return ('xs' if lines <= 20 else 's' if lines <= 100 else 'm' if lines <= 500
            else 'l' if lines <= 2000 else 'xl')


def build_manifest(packet: dict, replaying: bool = False) -> dict:
    setups, measures = validate(packet)
    require(replaying or packet['policy']['version'] == POLICY_VERSION, 'new freezes use the current policy')
    as_of = timestamp(packet['as_of'])
    frozen_at = timestamp(packet['frozen_at'])
    require(frozen_at >= as_of, 'frozen_at before as_of')
    start = as_of - timedelta(days=90)
    recent = as_of - timedelta(days=28)
    deadline = time.monotonic() + 45

    def check_time():
        require(time.monotonic() <= deadline, 'cohort wall budget exhausted')

    sources = unique(packet['sources'], 'id')
    evidence = unique(packet['evidence'], 'id')
    observations = unique(packet['observations'], 'pr_id')
    for sid, src in sources.items():
        check_time()
        require(text(src['schema']) and DIGEST.fullmatch(src['digest']) is not None, 'source schema/digest')
        require(type(src['complete']) is bool and isinstance(src['gaps'], list), 'source coverage flags')
        require(timestamp(src['snapshot_at']) <= frozen_at, 'source newer than freeze')
        require(timestamp(src['history_start']) <= timestamp(src['complete_through']) <= timestamp(src['snapshot_at']),
                'reversed or future source coverage')
    for ev in evidence.values():
        check_time()
        require(ev['source_id'] in sources and ev['schema'] == 'flightops-pr-proof/1' and
                DIGEST.fullmatch(ev['digest']) is not None, 'evidence source/schema/digest')
        require(type(ev['qualified']) is bool and text(ev['claim']) and text(ev['pr_id']), 'evidence fields')
        require(SHA.fullmatch(ev['anchor_sha']) is not None, 'evidence full anchor SHA')
        timestamp(ev['effective_at'])
        require(timestamp(ev['recorded_at']) <= frozen_at, 'evidence newer than freeze')

    proof_lookups = 0

    def proof(ids, claim, row, measure_id=None):
        nonlocal proof_lookups
        if not isinstance(ids, list):
            return False
        for ident in ids:
            proof_lookups += 1
            require(proof_lookups <= 2_000_000, 'proof lookup budget exhausted')
            if proof_lookups % 1000 == 0:
                require(time.monotonic() <= deadline, 'cohort wall budget exhausted')
            ev = evidence.get(ident)
            if not ev or not (ev['claim'] == claim and ev['qualified'] and ev['pr_id'] == row['pr_id'] and
                              ev['anchor_sha'] == row['anchor']['r0_sha'] and
                              timestamp(ev['effective_at']) <= as_of and
                              (claim != 'first_ready' or ev['effective_at'] == row['first_ready_at'])):
                continue
            if claim == 'actual_setup':
                setup = setups.get(row['setup_id'])
                expected = {d: getattr(setup, d).value for d in ('provider', 'model', 'effort', 'tier')} if setup else None
                if ev.get('setup_id') != row['setup_id'] or ev.get('reported_settings') != expected:
                    continue
            if claim == 'verified_anchor' and (ev.get('base_sha') != row['anchor']['base_sha'] or
                                               ev.get('anchor_kind') != row['anchor']['kind']):
                continue
            if claim == 'anchored_size':
                size = row['anchored_size']
                if size is None or ev.get('base_sha') != size.get('base_sha') or ev.get('changed_lines') != size.get('changed_lines'):
                    continue
            if measure_id is not None:
                fact = row['measures'].get(measure_id)
                bound = {'measure_id': measure_id, 'value': fact['value'], 'effective_at': fact['effective_at']}
                if ev.get('measure_id') != measure_id or ev.get('value_hash') != digest(bound):
                    continue
            src = sources[ev['source_id']]
            if (src['complete'] and not src['gaps'] and timestamp(src['history_start']) <= start and
                    timestamp(src['complete_through']) >= as_of and
                    timestamp(src['history_start']) <= timestamp(ev['effective_at']) <= timestamp(src['snapshot_at']) and
                    timestamp(ev['recorded_at']) <= timestamp(src['snapshot_at'])):
                return True
        return False

    inventory = packet['inventory']
    require(inventory['status'] in ('complete', 'unavailable', 'partial'), 'inventory status')
    require(isinstance(inventory['source_ids'], list) and
            all(s in sources for s in inventory['source_ids']), 'inventory source identities')
    inventory_complete = inventory['status'] == 'complete' and bool(inventory['source_ids']) and all(
        sources[s]['complete'] and not sources[s]['gaps'] and
        timestamp(sources[s]['history_start']) <= start and
        timestamp(sources[s]['complete_through']) >= as_of for s in inventory['source_ids'])
    family = packet['policy']['family']
    rows, exclusions = [], []

    for pr_id, raw in observations.items():
        check_time()
        repo = raw['repo']
        require(REPO.fullmatch(repo) is not None and type(raw['number']) is int and raw['number'] > 0 and
                pr_id == f"{repo}#{raw['number']}", 'noncanonical repository/PR identity')
        require(text(raw['revision']) and text(raw['task']), 'revision/task required')
        require(timestamp(raw['observed_at']) <= frozen_at, 'observation newer than freeze')
        anchor = raw['anchor']
        require(anchor['r0_sha'] is None or SHA.fullmatch(anchor['r0_sha']) is not None, 'invalid r0 SHA')
        require(anchor['base_sha'] is None or SHA.fullmatch(anchor['base_sha']) is not None, 'invalid base SHA')
        ready = timestamp(raw['first_ready_at']) if raw['first_ready_at'] is not None else None
        membership_proven = ready is not None and proof(raw['membership_evidence_ids'], 'first_ready', raw)
        if membership_proven and not start <= ready <= as_of:
            exclusions.append({'pr_id': pr_id, 'reason': 'outside_first_ready_90d',
                               'first_ready_at': raw['first_ready_at']})
            continue
        member = membership_proven and start <= ready <= as_of
        setup = setups.get(raw['setup_id'])
        candidates = raw['possible_setup_ids']
        require(isinstance(candidates, list) and len(set(candidates)) == len(candidates) and
                all(g in setups for g in candidates), 'invalid possible setups')
        if raw['setup_id'] is not None:
            require(raw['setup_id'] in setups, 'undefined setup')
        exact = bool(setup and setup.exact and proof(raw['setup_evidence_ids'], 'actual_setup', raw))
        if exact:

            exact = all(proof(list(s.evidence_ids), 'actual_setup', raw)
                        for s in (setup.provider, setup.model, setup.effort, setup.tier))
        known_group = raw['setup_id'] if exact else None
        possible = ([known_group] if known_group else
                    sorted(set(candidates) & set(family)) if candidates else list(family))

        if known_group is None and any(not setups[g].exact for g in candidates):
            possible = list(family)
        maturity = raw['mature_at'] is not None and timestamp(raw['mature_at']) <= as_of
        anchored = (anchor['r0_sha'] is not None and anchor['base_sha'] is not None and
                    anchor['kind'] in ('opened', 'first_ready', 'dispatch', 'observer') and
                    proof(anchor['evidence_ids'], 'verified_anchor', raw))
        size = raw['anchored_size']
        size_ok = (anchored and size is not None and size.get('base_sha') == anchor['base_sha'] and
                   size.get('r0_sha') == anchor['r0_sha'] and
                   type(size.get('changed_lines')) is int and 0 <= size['changed_lines'] <= 2**63 - 1 and
                   proof(size.get('evidence_ids'), 'anchored_size', raw))
        joint = [repo, raw['task'], size_bin(size['changed_lines'])] if size_ok else None
        require(isinstance(raw['independence_keys'], list) and raw['independence_keys'] and
                all(isinstance(k, str) and re.fullmatch(r'(run|request|stack|cluster):[^\s]{1,180}', k)
                    for k in raw['independence_keys']), 'independence keys required')
        require(type(raw['shared']) is bool, 'shared flag required')
        rows.append({'pr_id': pr_id, 'revision': raw['revision'], 'first_ready_at': raw['first_ready_at'],
                     'membership': 'included' if member else 'potential', 'setup_id': known_group,
                     'possible_setup_ids': possible, 'mature': maturity, 'anchor': anchor,
                     'anchored_size': size if size_ok else None, 'stratum': joint,
                     'independence_keys': sorted(set(raw['independence_keys'])), 'shared': raw['shared'],
                     'description_28d': member and ready >= recent, 'measures': {},
                     'input_record_hash': digest(raw)})
    owners = defaultdict(set)

    for raw in observations.values():
        check_time()
        for key in raw['independence_keys']:
            owners[key].add(raw['pr_id'])
    for row in rows:
        check_time()
        raw = observations[row['pr_id']]
        row['correlated_keys'] = [k for k in row['independence_keys'] if len(owners[k]) > 1]
        for mid, measure in measures.items():
            obs = raw['measures'].get(mid)
            reasons = []
            if row['membership'] != 'included': reasons.append('membership_unknown')
            if row['setup_id'] is None: reasons.append('actual_setup_unknown')
            if not row['mature']: reasons.append('immature')
            if row['stratum'] is None: reasons.append('anchored_size_unknown')
            if row['shared']: reasons.append('shared_unit')
            if row['correlated_keys']: reasons.append('correlated_unit')
            value = obs.get('value') if isinstance(obs, dict) else None
            if obs is None or value is None:
                reasons.append('missing_value')
            elif not number(value):
                reasons.append('overflow_value' if type(value) in (int, float) else 'invalid_value')
            elif measure.kind == 'rate' and value not in (0, 1): reasons.append('invalid_rate')
            elif measure.kind == 'amount' and value == 0: reasons.append('zero_amount')
            elif measure.kind == 'amount' and 0 < value < 1e-100: reasons.append('underflow_amount')
            elif measure.kind == 'amount' and value < 0: reasons.append('negative_amount')
            if isinstance(obs, dict):
                if timestamp(obs['effective_at']) > as_of: reasons.append('future_value')
                for claim in measure.required_claims:
                    if not proof(obs['evidence_ids'], claim, raw, mid): reasons.append('receipt_missing:' + claim)
                if obs.get('reason'): reasons.append('upstream:' + str(obs['reason'])[:200])
            else:
                reasons.append('measure_evidence_missing')
            row['measures'][mid] = {'status': 'included' if not reasons else 'missing',
                                    'value': value if not reasons else None, 'reasons': sorted(set(reasons)),
                                    'evidence_ids': sorted(obs['evidence_ids']) if isinstance(obs, dict) else []}
    populations = {}
    for mid in measures:
        groups = {}
        for group in family:
            known = [r for r in rows if r['setup_id'] == group]
            potential = [r for r in rows if r['setup_id'] is None and group in r['possible_setup_ids']]
            included = [r for r in known if r['measures'][mid]['status'] == 'included']
            included_ids = {r['pr_id'] for r in included}
            missing = [r for r in known if r['pr_id'] not in included_ids]
            possible_ids = {r['pr_id'] for r in known + potential}
            group_excluded = [{'pr_id': r['pr_id'], 'reason': 'different_exact_or_possible_setup'}
                              for r in rows if r['pr_id'] not in possible_ids] + exclusions
            groups[group] = {'eligibility_ids': [r['pr_id'] for r in known + potential],
                             'excluded': group_excluded,
                             'denominator_ids': [r['pr_id'] for r in known],
                             'potential_denominator_ids': [r['pr_id'] for r in potential],
                             'included_ids': [r['pr_id'] for r in included],
                             'missing_ids': [r['pr_id'] for r in missing],
                             'reasons': dict(sorted(Counter(reason for r in known + potential
                                 for reason in r['measures'][mid]['reasons']).items())),
                             'verified_numerator': sum(r['measures'][mid]['value'] for r in included)
                             if measures[mid].kind == 'rate' else None}
        populations[mid] = groups
    result = {'schema': MANIFEST_SCHEMA, 'policy_version': packet['policy']['version'], 'as_of': packet['as_of'],
              'frozen_at': packet['frozen_at'], 'window_days': 90, 'description_days': 28,
              'window_start': start.isoformat().replace('+00:00', 'Z'), 'snapshot_id': packet['snapshot_id'],
              'previous_manifest_hash': packet.get('previous_manifest_hash'), 'input_hash': digest(packet),
              'policy': packet['policy'], 'inventory_complete': inventory_complete, 'inventory': inventory,
              'sources': list(sources.values()), 'evidence': list(evidence.values()),
              'setups': {g: asdict(s) | {'exact': s.exact} for g, s in setups.items()},
              'measures': {m: asdict(v) for m, v in measures.items()}, 'rows': rows,
              'excluded': exclusions, 'populations': populations,
              'qualification': 'normalized assertions only; upstream authenticity not established by this kernel'}
    result['manifest_hash'] = digest(result)
    check_time()
    return result
