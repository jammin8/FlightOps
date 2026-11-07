




from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
import math
import random
from statistics import NormalDist
import time

from . import engine_v1
from .contract import DECISION_SCHEMA, HISTORICAL_POLICY_VERSION, MANIFEST_SCHEMA, POLICY_VERSION, digest, require

MAX_WORK = 45_000_000
MAX_SECONDS = 45
SUPPORT_FLOOR = .7 - 1e-12


class BudgetExceeded(Exception):
    pass


def wilson(p: float, n: int, z: float = 1.959963984540054) -> list[float]:
    if not n:
        return [0.0, 1.0]
    d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    h = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return [max(0.0, c-h), min(1.0, c+h)]


def _seed(seed, *labels):
    return int(digest([seed, *labels])[:16], 16)


def _shape(manifest, mid):
    family = manifest['policy']['family']
    mix = {(c['repo'], c['task'], c['size']): c['weight'] for c in manifest['policy']['reference_mix']}
    cells = sorted(mix)
    values = {g: {c: [] for c in cells} for g in family}
    for row in manifest['rows']:
        fact = row['measures'][mid]
        cell = tuple(row['stratum']) if row['stratum'] else None
        if row['setup_id'] in values and cell in cells and fact['status'] == 'included':
            values[row['setup_id']][cell].append(float(fact['value']))
    support = [c for c in cells if all(len(values[g][c]) >= 2 for g in family)]
    mass = 0.
    for weight in sorted(mix[c] for c in support):
        mass += weight
    weights = {c: mix[c]/mass for c in support} if mass else {}
    missing = {g: {} for g in family}
    for g in family:
        for c in support:
            ids = []
            for row in manifest['rows']:
                possible = row['setup_id'] == g or (row['setup_id'] is None and g in row['possible_setup_ids'])
                same_cell = row['stratum'] is None or tuple(row['stratum']) == c
                if possible and same_cell and row['measures'][mid]['status'] != 'included':
                    ids.append(row['pr_id'])
            missing[g][c] = ids
    return values, weights, missing, min(1., mass), [list(c) for c in cells if c not in support]


def _covered(values, weights, missing, g):
    return bool(weights) and all(10*len(values[g][c]) >= 9*(len(values[g][c]) + len(missing[g][c])) for c in weights)


def _coverage(pop, covered):
    n = len(pop['included_ids'])
    known = len(pop['denominator_ids'])
    extra = len(pop['potential_denominator_ids'])
    d = known + extra
    return {'included': n, 'known_denominator': known, 'possible_extra': extra,
            'minimum_fraction': n/d if d else 0., 'threshold_pass': covered}


def _supported(manifest, mid, weights):
    rows = {row['pr_id']: row for row in manifest['rows']}
    rate = manifest['measures'][mid]['kind'] == 'rate'

    def outside(pr_id):
        stratum = rows[pr_id]['stratum']
        return bool(weights) and stratum is not None and tuple(stratum) not in weights

    populations = {}
    for g, pop in manifest['populations'][mid].items():
        kept = {key: [i for i in pop[key] if not outside(i)] for key in
                ('eligibility_ids', 'denominator_ids', 'potential_denominator_ids', 'included_ids', 'missing_ids')}
        populations[g] = kept | {
            'excluded': [{'pr_id': i, 'reason': 'outside_common_support'} for i in pop['eligibility_ids'] if outside(i)]
                        + pop['excluded'],
            'reasons': dict(sorted(Counter(reason for i in kept['eligibility_ids']
                                           for reason in rows[i]['measures'][mid]['reasons']).items())),
            'verified_numerator': sum(rows[i]['measures'][mid]['value'] for i in kept['included_ids']) if rate else None}
    return populations


def _weighted(parts, amount):
    total = math.fsum(parts)
    return total if amount else min(1., max(0., total))


def _summary(values, weights, amount):
    means, errors = {}, {}
    for g, cells in values.items():
        mean = _weighted([w * (sum(cells[c])/len(cells[c])) for c, w in weights.items()], amount)
        var = 0.0
        for c, w in weights.items():
            xs = cells[c]
            m = sum(xs)/len(xs)
            var += w*w * sum((x-m)**2 for x in xs) / (len(xs)*(len(xs)-1))


        if amount:
            means[g] = math.log(mean)
            errors[g] = max(math.sqrt(var)/mean, 1e-9)
        else:
            means[g] = mean
            errors[g] = max(math.sqrt(var), 1 / (2 * sum(len(cells[c]) for c in weights)))
    return means, errors


def _inference(values, weights, missing, measure, policy, measure_count, seed, deadline):
    amount = measure['kind'] == 'amount'
    family = policy['family']
    reference = policy['reference_setup']
    means, errors = _summary(values, weights, amount)
    pairs = list(combinations(family, 2))
    scale = {pair: math.hypot(errors[pair[0]], errors[pair[1]]) for pair in pairs}
    rng = random.Random(seed)
    maxima = []
    for b in range(policy['bootstrap_replicates']):
        if b % 20 == 0 and time.monotonic() > deadline:
            raise BudgetExceeded('wall_budget_exhausted')
        sampled = {g: {c: rng.choices(values[g][c], k=len(values[g][c])) for c in weights} for g in family}
        boot, _ = _summary(sampled, weights, amount)
        maxima.append(max([abs(boot[g]-means[g])/errors[g] for g in family] +
                          [abs((boot[a]-boot[b])-(means[a]-means[b]))/scale[a, b] for a, b in pairs]))
    alpha = .05 / measure_count
    maxima.sort()


    q = max(maxima[min(len(maxima)-1, math.ceil((1-alpha)*(len(maxima)+1))-1)],
            NormalDist().inv_cdf(1-alpha/(2*(len(family)+len(pairs)))))
    intervals = {g: [means[g]-q*errors[g], means[g]+q*errors[g]] for g in family}
    if not amount:
        z = NormalDist().inv_cdf(1-alpha/(2*len(family)*len(weights)))
        for g in family:
            lows, highs = [], []
            for c, w in weights.items():
                xs = values[g][c]
                low, high = wilson(sum(xs)/len(xs), len(xs), z)
                lows.append(w*low)
                highs.append(w*high)
            intervals[g] = [max(0., min(intervals[g][0], math.fsum(lows))),
                            min(1., max(intervals[g][1], math.fsum(highs)))]
    contrasts = {}
    for a, b in pairs:
        delta = means[a]-means[b]
        low, high = delta-q*scale[a, b], delta+q*scale[a, b]
        if not amount:
            low = min(low, intervals[a][0]-intervals[b][1])
            high = max(high, intervals[a][1]-intervals[b][0])
        contrasts[a, b] = [low, high]


    robust = {}
    for g in family:
        cells = [(w, values[g][c], len(missing[g][c])) for c, w in weights.items()]
        lower = _weighted([w*(sum(xs)/(len(xs)+m)) for w, xs, m in cells], amount)
        if amount:
            upper = _weighted([w*(sum(xs)/len(xs)) for w, xs, _ in cells], amount)
            low = intervals[g][0] + math.log(lower)-means[g]
            high = None if any(m for _, _, m in cells) else intervals[g][1] + math.log(upper)-means[g]
        else:
            upper = _weighted([w*((sum(xs)+m)/(len(xs)+m)) for w, xs, m in cells], amount)
            low = max(0., intervals[g][0] + lower-means[g])
            high = min(1., intervals[g][1] + upper-means[g])
        robust[g] = [low, high]
    coverage = {g: _coverage(policy['_populations'][g], _covered(values, weights, missing, g)) for g in family}
    coverage_pass = all(v['threshold_pass'] for v in coverage.values())
    precision = {}
    for g in family:
        low, high = intervals[g]
        precision[g] = (max(means[g]-low, high-means[g]) <= math.log(2) if amount
                        else (high-low)/2 <= .2)
    if amount:

        for (a, b), (low, high) in contrasts.items():
            ok = max(means[a]-means[b]-low, high-(means[a]-means[b])) <= math.log(2)
            precision[a] &= ok
            precision[b] &= ok
    actions = {g: {'Best': False, 'NeedsAttention': False, 'coverage_route': None} for g in family}
    sign = 1 if measure['direction'] == 'higher' else -1

    def better(a, b, bounds):
        if sign == 1:
            return bounds[a][0] is not None and bounds[b][1] is not None and bounds[a][0] > bounds[b][1]
        return bounds[a][1] is not None and bounds[b][0] is not None and bounds[a][1] < bounds[b][0]

    def deficit(g, bounds=None):
        if bounds is not None:
            low = (None if bounds[reference][0] is None or bounds[g][1] is None else
                   bounds[reference][0]-bounds[g][1]) if sign == 1 else (
                   None if bounds[g][0] is None or bounds[reference][1] is None else
                   bounds[g][0]-bounds[reference][1])
            return low
        pair = (g, reference) if (g, reference) in contrasts else (reference, g)
        low, high = contrasts[pair]
        if pair != (g, reference): low, high = -high, -low
        return -high if sign == 1 else low

    effect_checks = {}
    for g in family:
        effect_checks[g] = {'best_separates_every_other': all(better(g, h, intervals) for h in family if h != g),
                            'worst_case_best_separates_every_other': all(better(g, h, robust) for h in family if h != g),
                            'deficit_lower_bound': deficit(g) if g != reference else None,
                            'worst_case_deficit_lower_bound': deficit(g, robust) if g != reference else None,
                            'deficit_threshold': math.log(1.5) if amount else .1,
                            'scale': 'log_ratio' if amount else 'rate_difference'}
        if all(precision.values()):
            plain_best = all(better(g, h, intervals) for h in family if h != g)
            bound_best = all(better(g, h, robust) for h in family if h != g)
            actions[g]['Best'] = plain_best and (coverage_pass or bound_best)
            if g != reference:
                threshold = math.log(1.5) if amount else .1
                plain_bad = deficit(g) > threshold
                bounded = deficit(g, robust)
                actions[g]['NeedsAttention'] = plain_bad and (coverage_pass or (bounded is not None and bounded > threshold))
            actions[g]['coverage_route'] = 'at_least_90_percent_each' if coverage_pass else 'directional_worst_case'
    return {'means': means, 'intervals': intervals, 'robust_intervals': robust,
            'contrasts': contrasts, 'effect_checks': effect_checks, 'precision': precision, 'coverage': coverage, 'actions': actions,
            'max_statistic_critical': q, 'bootstrap_replicates': len(maxima),
            'bootstrap_maxima_hash': digest(maxima)}


def _null_values(values, weights, kind, scenario, rng):
    pooled = {c: [x for cells in values.values() for x in cells[c]] for c in weights}
    result = {}
    for g, cells in values.items():
        result[g] = {}
        for c in weights:
            n = len(cells[c])
            pool = pooled[c]
            if kind == 'rate':
                p = sum(pool)/len(pool) if scenario == 'pooled_joint_strata' else float(scenario)
                result[g][c] = [float(rng.random() < p) for _ in range(n)]
            elif scenario == 'pooled_joint_strata':
                result[g][c] = rng.choices(pool, k=n)
            else:
                sigma = float(scenario)

                center = math.log(sum(pool)/len(pool)) - sigma*sigma/2
                result[g][c] = [math.exp(center + rng.gauss(0, sigma)) for _ in range(n)]
    return result


def _calibrate(values, weights, missing, measure, policy, count, seed, deadline):
    scenarios = (['pooled_joint_strata', '0.01', '0.5', '0.99'] if measure['kind'] == 'rate'
                 else ['pooled_joint_strata', '0.5', '1.5'])
    trials = policy['calibration_simulations']
    reports = []

    z = NormalDist().inv_cdf(1-.05/len(scenarios))
    for scenario in scenarios:
        failures = []
        rng = random.Random(_seed(seed, 'null', scenario))
        for i in range(trials):
            if time.monotonic() > deadline: raise BudgetExceeded('wall_budget_exhausted')
            null = _null_values(values, weights, measure['kind'], scenario, rng)
            inference = _inference(null, weights, missing, measure, policy, count,
                                   _seed(seed, scenario, i), deadline)
            flags = [g + ':' + action for g, actions in inference['actions'].items()
                     for action in ('Best', 'NeedsAttention') if actions[action]]
            if flags: failures.append({'simulation': i, 'flags': flags})
        rate = len(failures)/trials
        interval = wilson(rate, trials, z)
        reports.append({'scenario': scenario, 'simulations': trials, 'false_flags': len(failures),
                        'false_flag_fraction': rate, 'upper_95_simultaneous': interval[1],
                        'pass': interval[1] <= .1, 'failure_cases': failures})
    result = {'status': 'computed', 'pass': all(r['pass'] for r in reports), 'seed': seed,
            'family_size': len(policy['family']), 'reference_setup': policy['reference_setup'],
            'shape': {g: {'/'.join(c): len(xs) for c, xs in cells.items() if c in weights}
                      for g, cells in values.items()},
            'missing_shape': {g: {'/'.join(c): len(ids) for c, ids in cells.items()} for g, cells in missing.items()},
            'scenarios': reports, 'rule': 'any flag in frozen family; simultaneous Wilson upper <= 0.10'}
    result['calibration_hash'] = digest(result)
    return result


def _unavailable(reason):
    return {'status': 'not_computed', 'pass': False, 'reason': reason}


def decide(manifest: dict) -> dict:
    if manifest.get('policy_version') == HISTORICAL_POLICY_VERSION:
        return engine_v1.decide(manifest)
    require(manifest.get('schema') == MANIFEST_SCHEMA and manifest.get('policy_version') == POLICY_VERSION,
            'unsupported frozen manifest')
    require(manifest.get('manifest_hash') == digest({k: v for k, v in manifest.items() if k != 'manifest_hash'}),
            'manifest hash mismatch')
    family = manifest['policy']['family']
    out = {'schema': DECISION_SCHEMA, 'policy_version': POLICY_VERSION, 'manifest_hash': manifest['manifest_hash'],
           'input_hash': manifest['input_hash'], 'as_of': manifest['as_of'], 'window_days': 90,
           'scope': 'among comparable setups on common joint-stratum support',
           'qualification': manifest['qualification'], 'policy': manifest['policy'], 'measures': {}}
    deadline = time.monotonic() + MAX_SECONDS
    used_work = 0
    for mid, measure in manifest['measures'].items():
        policy = dict(manifest['policy'])
        values, weights, missing, mass, uncovered = _shape(manifest, mid)
        populations = _supported(manifest, mid, weights)
        policy['_populations'] = populations
        counts = {g: sum(len(values[g][c]) for c in weights) for g in family}
        coverage = {g: _coverage(populations[g], _covered(values, weights, missing, g)) for g in family}
        coverage_pass = all(v['threshold_pass'] for v in coverage.values())
        gates = {'inventory_complete': manifest['inventory_complete'],
                 'two_exact_reported_setups': len(family) >= 2 and all(manifest['setups'][g]['exact'] for g in family),
                 'independent_prs_at_least_20_each': all(n >= 20 for n in counts.values()),
                 'common_support_at_least_70_percent': mass >= SUPPORT_FLOOR}
        result = {'kind': measure['kind'], 'unit': measure['unit'], 'direction': measure['direction'],
                  'family': family, 'reference_setup': policy['reference_setup'], 'independent_counts': counts,
                  'populations': populations, 'gates': gates, 'common_support_fraction': mass,
                  'supported_mix': [{'stratum': list(c), 'normalized_weight': w} for c, w in weights.items()],
                  'uncovered_strata': uncovered, 'groups': {}, 'calibration': _unavailable('prerequisites_failed'),
                  'excluded_groups': [g for g in manifest['setups'] if g not in family],
                  'comparisons': [], 'coverage': coverage, 'coverage_threshold_pass': coverage_pass}

        gates['verified_measure_representation'] = all(
            not row['measures'][mid]['reasons'] for row in manifest['rows']
            if row['measures'][mid]['status'] == 'included') and any(counts.values())
        prerequisites = all(gates.values())
        inference = None
        if prerequisites:
            scenarios = 4 if measure['kind'] == 'rate' else 3
            work = sum(counts.values()) * policy['bootstrap_replicates'] * (
                1 + scenarios*policy['calibration_simulations'])
            used_work += work
            if used_work > MAX_WORK:
                result['calibration'] = _unavailable('resampling_work_budget_exhausted')
            else:
                try:
                    inference = _inference(values, weights, missing, measure, policy, len(manifest['measures']),
                                           _seed(policy['seed'], mid, 'observed'), deadline)
                    result['calibration'] = _calibrate(values, weights, missing, measure, policy,
                                                       len(manifest['measures']), _seed(policy['seed'], mid, 'calibration'), deadline)
                except BudgetExceeded as exc:
                    result['calibration'] = _unavailable(str(exc))
        gates['calibration_false_flags_at_most_10_percent'] = result['calibration']['pass']
        gates['precision'] = inference is not None and all(inference['precision'].values())
        if inference:
            result['bootstrap'] = {k: inference[k] for k in
                                   ('max_statistic_critical', 'bootstrap_replicates', 'bootstrap_maxima_hash')}
            for (a, b), bounds in inference['contrasts'].items():
                result['comparisons'].append({'a': a, 'b': b, 'scale': 'log_ratio' if measure['kind'] == 'amount'
                                              else 'rate_difference', 'simultaneous_95_interval': bounds})
        global_ok = prerequisites and gates['calibration_false_flags_at_most_10_percent'] and gates['precision']
        for g in family:
            unmet = [k for k, ok in gates.items() if not ok]
            calls = []
            info = {'calls': calls, 'unmet_gates': unmet, 'status': 'InsufficientEvidence', 'coverage': coverage[g],
                    'gates': dict(gates) | {'coverage_or_directional_worst_case': coverage_pass}}
            if inference:
                action = inference['actions'][g]
                info.update({'estimate': inference['means'][g], 'scale': 'log_amount' if measure['kind'] == 'amount'
                             else 'rate', 'simultaneous_95_interval': inference['intervals'][g],
                             'worst_case_interval': inference['robust_intervals'][g],
                             'coverage': inference['coverage'][g], 'coverage_route': action['coverage_route'],
                             'effect_checks': inference['effect_checks'][g]})
                info['gates']['coverage_or_directional_worst_case'] = coverage_pass or action['Best'] or action['NeedsAttention']
                if not coverage_pass and not (action['Best'] or action['NeedsAttention']):
                    unmet.append('directional_worst_case_not_separated')
                if global_ok:
                    calls.extend(k for k in ('Best', 'NeedsAttention') if action[k])
                    info['status'] = ('Best' if 'Best' in calls else 'NeedsAttention' if calls else
                                      'NoClearDifference' if coverage_pass else 'InsufficientEvidence')
            if not info['gates']['coverage_or_directional_worst_case'] and 'directional_worst_case_not_separated' not in unmet:
                unmet.append('coverage_or_directional_worst_case')
            result['groups'][g] = info
        result['unmet_gates'] = [k for k, ok in gates.items() if not ok]
        out['measures'][mid] = result
    out['budget'] = {'max_resampled_pr_draws': MAX_WORK, 'estimated_draws': used_work,
                     'max_wall_seconds': MAX_SECONDS}
    out['decision_hash'] = digest(out)
    return out
