import copy
import hashlib
import json
import math
from pathlib import Path
import random
import struct
import sys
from statistics import NormalDist

root = Path(__file__).resolve().parents[2]
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=False)
data = (root / 'tests/fixtures/flightops-decision-kernel-rate.json').read_bytes()
(output / 'valid.json').write_bytes(data)
base = json.loads(data)
cases = []

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def seal(value):
    value['budget']['estimated_draws'] = sum(
        sum(m['independent_counts'].values()) * value['policy']['bootstrap_replicates'] *
        (1 + (4 if m['kind']=='rate' else 3) * value['policy']['calibration_simulations'])
        for m in value['measures'].values()
        if all(v for k,v in m['gates'].items() if k not in ('precision','calibration_false_flags_at_most_10_percent')))
    for measure in value['measures'].values():
        c = measure['calibration']
        if c['status'] == 'computed':
            c['calibration_hash'] = digest({k: v for k, v in c.items() if k != 'calibration_hash'})
    value['decision_hash'] = digest({k: v for k, v in value.items() if k != 'decision_hash'})
    return value

def write(name, value, valid, statuses=None, reseal=True):
    if reseal:
        seal(value)
    (output / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')
    cases.append({'name': name, 'valid': valid, 'statuses': [{'id':k,'status':v} for k,v in (statuses or {}).items()]})

def invalid(name, mutate):
    value = copy.deepcopy(base)
    mutate(value)
    write(name, value, False)

def assign(path, value):
    def apply(obj):
        for key in path[:-1]:
            obj = obj[key]
        obj[path[-1]] = value
    return apply

def drop(path):
    def apply(obj):
        for key in path[:-1]:
            obj = obj[key]
        del obj[path[-1]]
    return apply

write('valid', copy.deepcopy(base), True, {'a': 'Best', 'b': 'NeedsAttention'})
measure = ['measures', 'metric']
group = measure + ['groups', 'a']
for name, path, value in [
    ('schema', ['schema'], 'flightops-decision/2'), ('policy_version', ['policy_version'], 'future/1'),
    ('window', ['window_days'], 7), ('future', ['as_of'], '2030-01-01T00:00:00Z'),
    ('bad_clock', ['as_of'], 'not-a-clock'), ('offset_clock', ['as_of'], '2026-10-29T12:00:00+00:00'),
    ('scope', ['scope'], 'all models globally'), ('attestation', ['qualification'], 'verified actual fleet'),
    ('manifest_ref', ['manifest_hash'], 'x' * 64), ('input_ref', ['input_hash'], ''),
    ('empty_measures', ['measures'], {}), ('negative_n', measure + ['independent_counts', 'a'], -1),
    ('overflow_n', measure + ['independent_counts', 'a'], 2**70), ('bool_n', measure + ['independent_counts', 'a'], True),
    ('groups_empty', measure + ['groups'], {}), ('reused_family', ['policy', 'family'], ['a', 'a']),
    ('reference', ['policy', 'reference_setup'], 'other'), ('sample_flag', measure + ['gates', 'independent_prs_at_least_20_each'], False),
    ('bad_support', measure + ['common_support_fraction'], .5), ('missing_support', measure + ['supported_mix'], []),
    ('empty_gates', measure + ['gates'], {}), ('bad_unmet', group + ['unmet_gates'], ['precision']),
    ('missing_calls', group + ['calls'], []), ('unknown_status', group + ['status'], 'Healthy'),
    ('interval_reversed', group + ['simultaneous_95_interval'], [1, 0]),
    ('missing_interval', group + ['simultaneous_95_interval'], None), ('rate_unbounded', group + ['worst_case_interval'], [0, None]),
    ('bad_estimate', group + ['estimate'], 2), ('wrong_scale', group + ['scale'], 'tokens'),
    ('fraction', measure + ['coverage', 'a', 'minimum_fraction'], .5),
    ('negative_possible', measure + ['coverage', 'a', 'possible_extra'], -1),
    ('coverage_gate', measure + ['coverage_threshold_pass'], False), ('shared_group_gate', group + ['gates', 'precision'], False),
    ('best_effect', group + ['effect_checks', 'best_separates_every_other'], False),
    ('calibration_pass', measure + ['calibration', 'pass'], False),
    ('calibration_shape', measure + ['calibration', 'shape', 'a'], {}),
    ('bootstrap_ref', measure + ['bootstrap', 'bootstrap_maxima_hash'], 'bad'),
    ('missing_contrasts', measure + ['comparisons'], []), ('bad_budget', ['budget', 'max_wall_seconds'], -1),
    ('unknown_top', ['actual_model'], 'a fabricated name')]:
    invalid(name, assign(path, value))
for name, path in [('missing_input', ['input_hash']), ('missing_asof', ['as_of']),
                   ('missing_precision', measure + ['gates', 'precision']), ('missing_population', measure + ['populations', 'a']),
                   ('missing_denominator', measure + ['populations', 'a', 'denominator_ids'])]:
    invalid(name, drop(path))
invalid('population_ids', assign(measure + ['populations', 'a', 'included_ids'], ['org/repo#1'] * 40))
def overlap_known(d):
    a=d['measures']['metric']['populations']['a']; b=d['measures']['metric']['populations']['b']
    for k in ('denominator_ids','eligibility_ids','included_ids'): b[k]=a[k][:]
invalid('cross_setup_allocation',overlap_known)
def unknown_deficit(d):
    g=d['measures']['metric']['groups']['b']
    g.update(status='NoClearDifference',calls=[])
    g['effect_checks'].update(deficit_lower_bound=None,worst_case_deficit_lower_bound=None)
invalid('unknown_deficit_overlap',unknown_deficit)
invalid('reference_deficit',assign(group+['effect_checks','deficit_lower_bound'],.2))
invalid('forged_precision',assign(group+['simultaneous_95_interval'],[0,1]))
def forged_separation(d):
    a=d['measures']['metric']['groups']['a']; b=d['measures']['metric']['groups']['b']
    for g in [a,b]: g.update(estimate=.5,simultaneous_95_interval=[.3,.7],worst_case_interval=[.3,.7])
invalid('forged_separation',forged_separation)
def bad_repo(d):
    d['policy']['reference_mix'][0]['repo']='not-a-repository'
    m=d['measures']['metric']; m['supported_mix'][0]['stratum'][0]='not-a-repository'
    for key in ('shape','missing_shape'):
        for g in m['calibration'][key]: m['calibration'][key][g]={'not-a-repository/feature/s':40 if key=='shape' else 0}
invalid('invalid_joint_repository',bad_repo)
tamper = copy.deepcopy(base)
tamper['scope'] += '!'
write('tampered_hash', tamper, False, reseal=False)
tamper = copy.deepcopy(base)
tamper['measures']['metric']['calibration']['calibration_hash'] = '0' * 64
tamper['decision_hash'] = digest({k:v for k,v in tamper.items() if k != 'decision_hash'})
write('calibration_hash', tamper, False, reseal=False)

overlap = copy.deepcopy(base)
for g in overlap['measures']['metric']['groups'].values():
    g.update(calls=[], status='NoClearDifference', estimate=.5, simultaneous_95_interval=[.3, .7], worst_case_interval=[.3, .7])
    g['effect_checks'].update(best_separates_every_other=False, worst_case_best_separates_every_other=False)
overlap['measures']['metric']['groups']['b']['effect_checks'].update(deficit_lower_bound=-.4, worst_case_deficit_lower_bound=-.4)
overlap['measures']['metric']['comparisons'][0]['simultaneous_95_interval'] = [-.4, .4]
for p in overlap['measures']['metric']['populations'].values(): p['verified_numerator']=20
write('overlap', overlap, True, {'a':'NoClearDifference','b':'NoClearDifference'})
imprecise=copy.deepcopy(overlap)
for g in imprecise['measures']['metric']['groups'].values(): g.update(simultaneous_95_interval=[.2,.8],worst_case_interval=[.2,.8])
imprecise['measures']['metric']['groups']['b']['effect_checks'].update(deficit_lower_bound=-.6,worst_case_deficit_lower_bound=-.6)
imprecise['measures']['metric']['comparisons'][0]['simultaneous_95_interval']=[-.6,.6]
write('forged_precision_without_separation',imprecise,False)
multiple=copy.deepcopy(base); multiple['measures']['second']=copy.deepcopy(overlap['measures']['metric'])
write('multiple_measures',multiple,True,{'a':'Best','b':'NeedsAttention'})

hold = copy.deepcopy(base)
r = hold['measures']['metric']
r['calibration'] = {'status':'not_computed', 'pass':False, 'reason':'wall_budget_exhausted'}
r['gates']['calibration_false_flags_at_most_10_percent'] = False
r['unmet_gates'] = ['calibration_false_flags_at_most_10_percent']
for g in r['groups'].values():
    g['calls'] = []; g['status'] = 'InsufficientEvidence'
    g['gates']['calibration_false_flags_at_most_10_percent'] = False
    g['unmet_gates'] = r['unmet_gates'][:]
write('calibration_hold', hold, True, {'a':'InsufficientEvidence','b':'InsufficientEvidence'})

sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
from collectors.model_analysis import build_manifest, decide
from collectors.model_analysis.engine import wilson

sys.path.insert(0, str(root/'tests'))
from test_model_analysis_support import light_cell, missing, packet, rebind, six_cells

def produced(p):
    rebind(p)
    return decide(build_manifest(p))

diluted = packet(400, kind='amount', direction='lower', effect=True)
for i, row in enumerate(diluted['observations']):
    if i % 400 >= 80: row['task'] = 'outside-reference'
    elif i % 400 >= 40: missing(row)
write('current', produced(packet(40, effect=True)), True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
write('outside_support', produced(diluted), True, {'a':'InsufficientEvidence','b':'InsufficientEvidence'}, reseal=False)
diluted['policy']['version'] = 'joint-strata-max-bootstrap/1'
rebind(diluted)
write('historical_outside_support', decide(build_manifest(diluted, replaying=True)), True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
invalid('unknown_policy', lambda value: (value.update(policy_version='joint-strata-max-bootstrap/3'), value['policy'].update(version='joint-strata-max-bootstrap/3')))
invalid('mixed_policy', assign(['policy_version'], 'joint-strata-max-bootstrap/2'))

def weighted(weights):
    p = packet(80, effect=True)
    p['policy']['reference_mix'] = [{'repo':'org/repo','task':str(i),'size':'s','weight':w} for i, w in enumerate(weights)]
    for i, row in enumerate(p['observations']): row['task'] = str(i % len(weights))
    return produced(p)

write('weighted_boundary', weighted([0.266411259804338, 0.1419783183553549, 0.5022241992749249, 0.08938622256538212]), True,
      {'a':'Best','b':'NeedsAttention'}, reseal=False)
write('support_mass_tolerance', weighted([.25 + 2e-10]*4), True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
write('support_gate_boundary', produced(six_cells()), True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
write('support_gate_reversed', produced(six_cells(order=(5, 4, 3, 2, 1, 0))), True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
write('support_gate_above', produced(six_cells([0.448, 0.052, 0.065, 0.074, 0.071, 0.29])), True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
below_support = produced(six_cells([0.428, 0.052, 0.065, 0.074, 0.071, 0.31]))
write('support_gate_below', below_support, True, {'a':'InsufficientEvidence','b':'InsufficientEvidence'}, reseal=False)
write('support_gate_just_below', produced(six_cells([0.438 - 1e-11, 0.052, 0.065, 0.074, 0.071, 0.3 + 1e-11])), True, {'a':'InsufficientEvidence','b':'InsufficientEvidence'}, reseal=False)
forged_support = copy.deepcopy(below_support)
forged_support['measures']['metric']['gates']['common_support_at_least_70_percent'] = True
forged_support['measures']['metric']['unmet_gates'] = [g for g in forged_support['measures']['metric']['unmet_gates'] if g != 'common_support_at_least_70_percent']
write('forged_support_gate', forged_support, False)

def historical(p):
    rebind(p)
    return decide(build_manifest(p, replaying=True))

standardized_rate = produced(light_cell(240, 24))
write('standardized_rate', standardized_rate, True, {'a':'InsufficientEvidence','b':'InsufficientEvidence'}, reseal=False)
write('standardized_amount', produced(light_cell(200, 20, kind='amount', direction='lower')), True, {'a':'InsufficientEvidence','b':'InsufficientEvidence'}, reseal=False)
historical_rate = historical(light_cell(240, 24, version='joint-strata-max-bootstrap/1'))
write('historical_standardized_rate', historical_rate, True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
write('historical_standardized_amount', historical(light_cell(200, 20, kind='amount', direction='lower', version='joint-strata-max-bootstrap/1')), True, {'a':'Best','b':'NeedsAttention'}, reseal=False)
forged_cells = copy.deepcopy(standardized_rate)
forged_cells['measures']['metric']['coverage_threshold_pass'] = True
for g in ('a', 'b'):
    forged_cells['measures']['metric']['coverage'][g]['threshold_pass'] = True
    forged_cells['measures']['metric']['groups'][g]['coverage']['threshold_pass'] = True
write('forged_cell_coverage', forged_cells, False)
relabelled = copy.deepcopy(historical_rate)
relabelled['policy_version'] = 'joint-strata-max-bootstrap/2'
relabelled['policy']['version'] = 'joint-strata-max-bootstrap/2'
write('relabelled_historical', relabelled, False)

small = json.loads((root/'tests/fixtures/model-analysis-kernel/membership-input.json').read_text())
insufficient = decide(build_manifest(small, replaying=True))
assert insufficient['budget']['estimated_draws'] == 0
assert all(g['status']=='InsufficientEvidence' for m in insufficient['measures'].values() for g in m['groups'].values())
write('insufficient_kernel', insufficient, True)
skipped=copy.deepcopy(insufficient)
for r in skipped['measures'].values():
    r['gates']['precision']=True; r['unmet_gates']=[g for g in r['unmet_gates'] if g!='precision']
    for g in r['groups'].values():
        g['gates']['precision']=True; g['unmet_gates']=[x for x in g['unmet_gates'] if x!='precision']
write('skipped_precision',skipped,False)

amount = copy.deepcopy(overlap)
r = amount['measures']['metric']; r['kind'] = 'amount'; r['unit'] = 'tokens'
r['comparisons'][0].update(scale='log_ratio', simultaneous_95_interval=[-.2,.2])
for p in r['populations'].values(): p['verified_numerator'] = None
for g in r['groups'].values():
    g.update(scale='log_amount', estimate=4.6, simultaneous_95_interval=[4.5,4.7], worst_case_interval=[4.5,4.7])
    g['effect_checks'].update(scale='log_ratio', deficit_threshold=math.log(1.5))
r['groups']['b']['effect_checks'].update(deficit_lower_bound=-.2,worst_case_deficit_lower_bound=-.2)
r['calibration']['scenarios'] = [s for s in r['calibration']['scenarios'] if s['scenario'] in ['pooled_joint_strata','0.5']]
third = copy.deepcopy(r['calibration']['scenarios'][1]); third['scenario'] = '1.5'; r['calibration']['scenarios'].append(third)
for scenario in r['calibration']['scenarios']:
    scenario['upper_95_simultaneous'] = wilson(scenario['false_flags']/scenario['simulations'], scenario['simulations'], NormalDist().inv_cdf(1-.05/3))[1]
    scenario['pass'] = scenario['upper_95_simultaneous'] <= .1
write('amount_overlap', amount, True, {'a':'NoClearDifference','b':'NoClearDifference'})

unbounded = copy.deepcopy(amount); r = unbounded['measures']['metric']; r['coverage_threshold_pass'] = False
for i, key in enumerate(['a','b']):
    p = r['populations'][key]; extra = [f'org/missing#{1000+i*10+j}' for j in range(10)]
    p['denominator_ids'] += extra; p['eligibility_ids'] += extra; p['missing_ids'] += extra; p['reasons'] = {'missing_value':10}
    c = {'included':40,'known_denominator':50,'possible_extra':0,'minimum_fraction':.8,'threshold_pass':False}
    r['coverage'][key] = c
    g = r['groups'][key]; g['coverage'] = copy.deepcopy(c)
    g.update(status='InsufficientEvidence', calls=[], coverage_route='directional_worst_case', worst_case_interval=[4.4,None], unmet_gates=['directional_worst_case_not_separated'])
    g['gates']['coverage_or_directional_worst_case'] = False
    if key!='a': g['effect_checks']['worst_case_deficit_lower_bound']=None
    r['calibration']['missing_shape'][key] = {cell:10 for cell in r['calibration']['missing_shape'][key]}
write('unbounded_amount', unbounded, True, {'a':'InsufficientEvidence','b':'InsufficientEvidence'})
long_reason=copy.deepcopy(unbounded)
for p in long_reason['measures']['metric']['populations'].values(): p['reasons']['upstream:'+'x'*200]=10
write('long_reason',long_reason,True,{'a':'InsufficientEvidence','b':'InsufficientEvidence'})

for name, value in [('rate_reversed', base), ('overlap_reversed', overlap), ('amount_reversed', amount)]:
    value = copy.deepcopy(value)
    c = value['measures']['metric']['comparisons'][0]
    c['a'], c['b'] = c['b'], c['a']
    c['simultaneous_95_interval'] = [-c['simultaneous_95_interval'][1], -c['simultaneous_95_interval'][0]]
    write(name, value, True)

bad = copy.deepcopy(overlap)
r = bad['measures']['metric']
r['comparisons'][0]['simultaneous_95_interval'] = [.2,.3]
r['groups']['b']['effect_checks']['deficit_lower_bound'] = .2
r['groups']['b'].update(status='NeedsAttention',calls=['NeedsAttention'])
write('D1_rate_point',bad,False)
bad = copy.deepcopy(overlap)
r = bad['measures']['metric']
r['comparisons'][0]['simultaneous_95_interval'] = [-.2,.2]
r['groups']['b']['effect_checks']['deficit_lower_bound'] = -.2
write('D1_rate_envelope',bad,False)
bad = copy.deepcopy(amount)
r = bad['measures']['metric']
r['comparisons'][0]['simultaneous_95_interval'] = [.2,.3]
r['groups']['b']['effect_checks']['deficit_lower_bound'] = .2
write('D1_amount_point',bad,False)
zero = copy.deepcopy(overlap)
r = zero['measures']['metric']
for g in r['groups'].values():
    g.update(simultaneous_95_interval=[.5,.5],worst_case_interval=[.5,.5])
r['comparisons'][0]['simultaneous_95_interval'] = [0.,0.]
r['groups']['b']['effect_checks'].update(deficit_lower_bound=0.,worst_case_deficit_lower_bound=0.)
write('zero_contrast',zero,True,{'a':'NoClearDifference','b':'NoClearDifference'})

bad = copy.deepcopy(base)
for scenario in bad['measures']['metric']['calibration']['scenarios']:
    scenario.update(false_flags=12,false_flag_fraction=.1,upper_95_simultaneous=.1,
                    failure_cases=[{'simulation':i,'flags':['b:NeedsAttention']} for i in range(12)])
write('D2_false_passing_upper',bad,False)

def calibration_counts(value, count):
    r = value['measures']['metric']
    c = r['calibration']
    scenarios = len(c['scenarios'])
    for scenario in c['scenarios']:
        n = scenario['simulations']
        upper = wilson(count/n,n,NormalDist().inv_cdf(1-.05/scenarios))[1]
        scenario.update(false_flags=count,false_flag_fraction=count/n,upper_95_simultaneous=upper,
                        failure_cases=[{'simulation':i,'flags':['b:NeedsAttention']} for i in range(count)])
        scenario['pass'] = upper <= .1
    c['pass'] = all(s['pass'] for s in c['scenarios'])
    r['gates']['calibration_false_flags_at_most_10_percent'] = c['pass']
    r['unmet_gates'] = [] if c['pass'] else ['calibration_false_flags_at_most_10_percent']
    for g in r['groups'].values():
        g['gates']['calibration_false_flags_at_most_10_percent'] = c['pass']
        g['unmet_gates'] = r['unmet_gates'][:]
        if not c['pass']: g.update(status='InsufficientEvidence',calls=[])
    return value

for name, seed, count in [('rate_last_passing',base,4),('rate_first_failing',base,5),
                           ('rate_twelve_failures',base,12),('amount_last_passing',amount,5),('amount_first_failing',amount,6)]:
    write(name,calibration_counts(copy.deepcopy(seed),count),True)
last = calibration_counts(copy.deepcopy(base),4)
first = calibration_counts(copy.deepcopy(base),5)
bad = copy.deepcopy(first)
for scenario in bad['measures']['metric']['calibration']['scenarios']:
    scenario['upper_95_simultaneous'] = .1
    scenario['pass'] = True
r = bad['measures']['metric']
r['calibration']['pass'] = True
r['gates']['calibration_false_flags_at_most_10_percent'] = True
r['unmet_gates'] = []
for key,g in r['groups'].items():
    g.update(status=base['measures']['metric']['groups'][key]['status'],calls=base['measures']['metric']['groups'][key]['calls'][:],unmet_gates=[])
    g['gates']['calibration_false_flags_at_most_10_percent'] = True
write('D2_boundary_upper',bad,False)
for name, mutate in [
    ('D2_count_mismatch',assign(measure+['calibration','scenarios',0,'false_flags'],3)),
    ('D2_fraction_mismatch',assign(measure+['calibration','scenarios',0,'false_flag_fraction'],.1)),
    ('D2_family_size',assign(measure+['calibration','family_size'],3)),
    ('D2_family_adjustment',assign(measure+['calibration','scenarios',0,'upper_95_simultaneous'],wilson(4/120,120)[1])),
    ('D2_failure_duplicate',assign(measure+['calibration','scenarios',0,'failure_cases',1,'simulation'],0)),
    ('D2_flag_unknown',assign(measure+['calibration','scenarios',0,'failure_cases',0,'flags'],['other:Best'])),
    ('D2_flag_duplicate',assign(measure+['calibration','scenarios',0,'failure_cases',0,'flags'],['b:NeedsAttention','b:NeedsAttention'])),
    ('D2_scenario_duplicate',assign(measure+['calibration','scenarios',1,'scenario'],'pooled_joint_strata'))]:
    bad = copy.deepcopy(last)
    mutate(bad)
    write(name,bad,False)

unicode_small = copy.deepcopy(small)
unicode_small['observations'] = []
for setup, name in zip(unicode_small['setups'],['é','e\u0301']): setup['id'] = name
unicode_small['policy']['family'] = ['é','e\u0301']
unicode_small['policy']['reference_setup'] = 'é'
unicode_kernel = decide(build_manifest(unicode_small, replaying=True))
unicode_golden = json.loads((root/'tests/fixtures/flightops-decision-kernel-unicode.json').read_text())
assert unicode_kernel == unicode_golden and unicode_kernel['budget']['estimated_draws'] == 0
write('unicode_kernel',unicode_kernel,True)

def unicode_setup(value):
    if isinstance(value,dict):
        return {(unicode_setup(k) if set(value)=={'a','b'} else k):unicode_setup(v) for k,v in value.items()}
    if isinstance(value,list): return [unicode_setup(v) for v in value]
    if isinstance(value,str):
        if value in ('a','b'): return {'a':'é','b':'e\u0301'}[value]
        for g, replacement in [('a','é'),('b','e\u0301')]:
            if value in (g+':Best',g+':NeedsAttention'): return replacement+value[1:]
    return value

unicode_computed = unicode_setup(calibration_counts(copy.deepcopy(base),1))
for scenario in unicode_computed['measures']['metric']['calibration']['scenarios']:
    scenario['failure_cases'][0]['flags'] = ['é:Best','e\u0301:NeedsAttention']
write('unicode_computed',unicode_computed,True,{'é':'Best','e\u0301':'NeedsAttention'})
unicode_reverse = copy.deepcopy(unicode_computed)
c = unicode_reverse['measures']['metric']['comparisons'][0]
c['a'],c['b'] = c['b'],c['a']
c['simultaneous_95_interval'] = [-c['simultaneous_95_interval'][1],-c['simultaneous_95_interval'][0]]
write('unicode_computed_reversed',unicode_reverse,True)
unicode_metrics = copy.deepcopy(base)
unicode_metrics['measures'] = {'é':copy.deepcopy(base['measures']['metric']),'e\u0301':copy.deepcopy(overlap['measures']['metric'])}
write('unicode_measures',unicode_metrics,True)
unicode_reasons = copy.deepcopy(unbounded)
for pop in unicode_reasons['measures']['metric']['populations'].values(): pop['reasons'] = {'é':5,'e\u0301':5}
unicode_reasons['measures']['metric']['excluded_groups'] = ['é','e\u0301']
write('unicode_reasons_exclusions',unicode_reasons,True)
unicode_cells = copy.deepcopy(base)
unicode_cells['policy']['reference_mix'] = [{'repo':'org/repo','task':task,'size':'s','weight':.5} for task in ['é','e\u0301']]
r = unicode_cells['measures']['metric']
r['supported_mix'] = [{'stratum':['org/repo',task,'s'],'normalized_weight':.5} for task in ['é','e\u0301']]
for key in ('shape','missing_shape'):
    for g in r['calibration'][key]: r['calibration'][key][g] = {'org/repo/'+task+'/s':20 if key=='shape' else 0 for task in ['é','e\u0301']}
write('unicode_strata',unicode_cells,True)
bad = copy.deepcopy(unicode_computed)
bad['policy']['reference_setup'] = 'e\u0301'
write('unicode_reference_mismatch',bad,False)

nonfinite = canonical(base).replace('"estimate":1.0', '"estimate":1e400')
assert nonfinite != canonical(base)
underflow = canonical(base).replace('"estimate":0.0', '"estimate":1e-400')
assert underflow != canonical(base)
raw_cases = {'duplicate': '{"schema":1,"schema":2}', 'duplicate_escape': '{"schema":1,"\\u0073chema":2}',
             'duplicate_unicode': '{"é":1,"\\u00e9":2}', 'empty': '', 'truncated': data.decode()[:200],
             'nonfinite': nonfinite, 'underflow_zero': underflow,
             'invalid_utf8': None}
for name, text in raw_cases.items():
    (output/(name+'.json')).write_bytes(b'\xff' if text is None else text.encode())
    cases.append({'name':name,'valid':False,'statuses':[]})
(output/'cases.json').write_text(json.dumps(cases,indent=2)+'\n')

values = [{'unicode':'snow ☃ / 😀','controls':'\n\t\x00','seed':2**64-1,'nested':[None,True,False,-0.0,1e-6,1e16]},
          5e-324, 2.2250738585072014e-308, 1.7976931348623157e308]
rng = random.Random(643)
for _ in range(128):
    value = struct.unpack('>d',rng.getrandbits(64).to_bytes(8,'big'))[0]
    if math.isfinite(value): values.append(value)
vectors = [{'input':json.dumps(v,indent=2,ensure_ascii=False),'canonical':canonical(v),'digest':digest(v)} for v in values]
(output/'canonical-vectors.json').write_text(json.dumps(vectors,indent=2)+'\n')
hashes = []
for n in [0,3,55,56,64,1000]:
    b = bytes(i%256 for i in range(n)); hashes.append({'bytes':list(b),'digest':hashlib.sha256(b).hexdigest()})
(output/'sha-vectors.json').write_text(json.dumps(hashes)+'\n')
print(json.dumps({'output':str(output),'cases':len(cases),'canonical_vectors':len(vectors),'sha_vectors':len(hashes),
                  'frozen_kernel_fixture':True,'insufficient_kernel_resampling_draws':0,'unicode_kernel_resampling_draws':0,
                  'amount_fixture_correction':'Constructed amount calibration uses its three-scenario Wilson adjustment, replacing inherited rate bounds.',
                  'constructed_variants':'Decoder representation controls only; not statistical or live-source evidence.'}))
