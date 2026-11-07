
import copy

AS_OF = '2026-10-29T12:00:00Z'
READY = '2026-10-01T12:00:00Z'
R0, BASE, DIGEST = 'a'*40, 'b'*40, 'c'*64


def packet(n=40, groups=('a', 'b'), kind='rate', direction='higher', effect=False):
    p = {'schema': 'flightops-normalized-pr/1', 'as_of': AS_OF, 'frozen_at': AS_OF,
         'snapshot_id': 'synthetic-only-1', 'previous_manifest_hash': None,
         'policy': {'version': 'joint-strata-max-bootstrap/2', 'seed': 139,
                    'bootstrap_replicates': 199, 'calibration_simulations': 120,
                    'family': list(groups), 'reference_setup': groups[0],
                    'reference_mix': [{'repo': 'org/repo', 'task': 'feature', 'size': 's', 'weight': 1.0}]},
         'inventory': {'status': 'complete', 'source_ids': ['source']},
         'sources': [{'id': 'source', 'schema': 'synthetic-source/1', 'digest': DIGEST,
                      'complete': True, 'gaps': [], 'snapshot_at': AS_OF,
                      'history_start': '2026-01-01T00:00:00Z', 'complete_through': AS_OF}],
         'setups': [], 'measures': [{'id': 'metric', 'kind': kind, 'direction': direction,
                                    'semantics': 'generic', 'required_claims': ['measure_verified', 'exact_producer'],
                                    'unit': 'fraction' if kind == 'rate' else 'tokens'}],
         'evidence': [], 'observations': []}
    for gi, group in enumerate(groups):
        setup = {'id': group}
        for dim in ('provider', 'model', 'effort', 'tier'):
            setup[dim] = {'value': group if dim == 'model' else 'reported-'+dim, 'basis': 'reported', 'evidence_ids': []}
        p['setups'].append(setup)
        for i in range(n):
            pr_id = f'org/repo#{gi*n+i+1}'
            ids = {}
            for claim in ('first_ready', 'actual_setup', 'verified_anchor', 'anchored_size', 'measure_verified', 'exact_producer'):
                ident = f'{pr_id}:{claim}'
                ids[claim] = ident
                p['evidence'].append({'id': ident, 'claim': claim, 'pr_id': pr_id, 'anchor_sha': R0,
                                      'schema': 'flightops-pr-proof/1', 'source_id': 'source', 'digest': DIGEST,
                                      'qualified': True, 'effective_at': READY, 'recorded_at': AS_OF})
            for dim in ('provider', 'model', 'effort', 'tier'):
                setup[dim]['evidence_ids'].append(ids['actual_setup'])
            if kind == 'rate':
                value = int(i % 2 == 0) if not effect else int(gi == 0)
            else:
                value = (100 if gi == 0 or not effect else 1000) + i % 7
            p['observations'].append({'pr_id': pr_id, 'repo': 'org/repo', 'number': gi*n+i+1,
                'revision': 'normalized-revision-1', 'observed_at': AS_OF, 'first_ready_at': READY,
                'membership_evidence_ids': [ids['first_ready']], 'setup_id': group, 'possible_setup_ids': [],
                'setup_evidence_ids': [ids['actual_setup']], 'task': 'feature', 'mature_at': AS_OF,
                'anchor': {'kind': 'opened', 'r0_sha': R0, 'base_sha': BASE, 'evidence_ids': [ids['verified_anchor']]},
                'anchored_size': {'base_sha': BASE, 'r0_sha': R0, 'changed_lines': 50, 'evidence_ids': [ids['anchored_size']]},
                'shared': False, 'independence_keys': ['request:'+pr_id],
                'measures': {'metric': {'value': value, 'effective_at': READY,
                                        'evidence_ids': [ids['measure_verified'], ids['exact_producer']], 'reason': None}}})
    rebind(p)
    return p


def light_cell(n, measured, kind='rate', direction='higher', version=None):
    p = packet(n, kind=kind, direction=direction, effect=True)
    if version:
        p['policy']['version'] = version
    p['policy']['reference_mix'] = [{'repo': 'org/repo', 'task': t, 'size': 's', 'weight': w}
                                    for t, w in (('feature', .9), ('low-weight', .1))]
    for i, row in enumerate(p['observations']):
        if i % n >= 2*measured:
            row['task'] = 'low-weight'
        elif i % n >= measured:
            missing(row)
        elif kind == 'rate':
            row['measures']['metric']['value'] = int((i % 6 != 0) == (row['setup_id'] == 'a'))
    return p


SIX_CELLS = (0.438, 0.052, 0.065, 0.074, 0.071, 0.3)


def six_cells(weights=SIX_CELLS, order=range(6)):
    p = packet(100, effect=True)
    mix = [{'repo': 'org/repo', 'task': str(i), 'size': 's', 'weight': w} for i, w in enumerate(weights)]
    p['policy']['reference_mix'] = [mix[i] for i in order]
    for i, row in enumerate(p['observations']):
        row['task'] = str(i % 5)
    return p


def missing(row):
    row['measures']['metric']['value'] = None


def clone(value):
    return copy.deepcopy(value)


def rebind(p):

    from collectors.model_analysis.contract import digest
    setups = {s['id']: s for s in p['setups']}
    for ev in p['evidence']:
        row = next((r for r in p['observations'] if r['pr_id'] == ev['pr_id']), None)
        if row is None:
            continue
        claim = ev['claim']
        if claim == 'actual_setup' and row['setup_id'] is not None:
            ev['setup_id'] = row['setup_id']
            ev['reported_settings'] = {d:setups[row['setup_id']][d]['value'] for d in ('provider','model','effort','tier')}
        if claim == 'verified_anchor':
            ev['base_sha'] = row['anchor']['base_sha']
            ev['anchor_kind'] = row['anchor']['kind']
        if claim == 'anchored_size' and row['anchored_size'] is not None:
            ev['base_sha'] = row['anchored_size']['base_sha']
            ev['changed_lines'] = row['anchored_size']['changed_lines']
        if claim in ('measure_verified', 'exact_producer'):
            fact = row['measures']['metric']
            ev['measure_id'] = 'metric'
            ev['value_hash'] = digest({'measure_id':'metric','value':fact['value'],'effective_at':fact['effective_at']})
