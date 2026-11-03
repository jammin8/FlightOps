
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, TypedDict

INPUT_SCHEMA = 'flightops-normalized-pr/1'
MANIFEST_SCHEMA = 'flightops-cohort/1'
DECISION_SCHEMA = 'flightops-decision/1'
POLICY_VERSION = 'joint-strata-max-bootstrap/2'
HISTORICAL_POLICY_VERSION = 'joint-strata-max-bootstrap/1'
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 5000
MAX_GROUPS = 8
MAX_STRATA = 24
MAX_MEASURES = 8
MAX_VALUE = 1e100
SHA = re.compile(r'^[0-9a-f]{40}$')
DIGEST = re.compile(r'^[0-9a-f]{64}$')
REPO = re.compile(r'^[a-z0-9_.-]+/[a-z0-9_.-]+$')


class ContractError(ValueError):
    pass


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise ContractError(reason)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value: str) -> datetime:
    require(isinstance(value, str) and value.endswith('Z'), 'UTC timestamp must end in Z')
    try:
        result = datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError as exc:
        raise ContractError('invalid timestamp') from exc
    require(result.utcoffset().total_seconds() == 0, 'timestamp must be UTC')
    return result.astimezone(timezone.utc)


def text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 200


def number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and abs(value) <= MAX_VALUE
    except OverflowError:
        return False


def unique(records: list[dict], key: str) -> dict[str, dict]:

    out = {}
    for record in records:
        require(isinstance(record, dict) and text(record.get(key)), f'invalid {key}')
        ident = record[key]
        require(ident not in out or canonical(out[ident]) == canonical(record),
                f'conflicting {key}: {ident}; explicit reconciliation required')
        out[ident] = record
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class Setting:
    value: str | None
    basis: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Setup:
    id: str
    provider: Setting
    model: Setting
    effort: Setting
    tier: Setting

    @property
    def exact(self) -> bool:
        return all(s.value is not None and s.basis == 'reported' and s.evidence_ids
                   for s in (self.provider, self.model, self.effort, self.tier))


@dataclass(frozen=True)
class Measure:
    id: str
    kind: str
    direction: str
    semantics: str
    required_claims: tuple[str, ...]
    unit: str


class AnchorInput(TypedDict):
    kind: str
    r0_sha: str | None
    base_sha: str | None
    evidence_ids: list[str]


class AnchoredSizeInput(TypedDict):
    base_sha: str
    r0_sha: str
    changed_lines: int
    evidence_ids: list[str]


class MeasureInput(TypedDict):
    value: float | int | None
    effective_at: str
    evidence_ids: list[str]
    reason: str | None


class ObservationInput(TypedDict):
    pr_id: str
    repo: str
    number: int
    revision: str
    observed_at: str
    first_ready_at: str | None
    membership_evidence_ids: list[str]
    setup_id: str | None
    possible_setup_ids: list[str]
    setup_evidence_ids: list[str]
    task: str
    mature_at: str | None
    anchor: AnchorInput
    anchored_size: AnchoredSizeInput | None
    shared: bool
    independence_keys: list[str]
    measures: dict[str, MeasureInput]



CLAIMS = {
    'first_try_ci': {'verified_anchor', 'original_attempt_complete', 'exact_producer'},
    'rework': {'candidate_inventory_complete', 'assignment_complete', 'resolution_complete', 'exact_producer'},
    'usage': {'usage_complete', 'usage_exclusive', 'exact_producer'},
    'generic': {'measure_verified', 'exact_producer'},
}


def validate(packet: dict) -> tuple[dict[str, Setup], dict[str, Measure]]:
    require(isinstance(packet, dict) and packet.get('schema') == INPUT_SCHEMA, 'unsupported input schema')
    require(len(canonical(packet)) <= MAX_BYTES, 'input byte limit')
    timestamp(packet['as_of'])
    require(text(packet.get('snapshot_id')), 'snapshot_id required')
    require(packet.get('previous_manifest_hash') is None or
            bool(DIGEST.fullmatch(packet['previous_manifest_hash'])), 'invalid prior manifest hash')
    policy = packet['policy']
    require(policy.get('version') in (POLICY_VERSION, HISTORICAL_POLICY_VERSION), 'unsupported policy version')
    require(type(policy.get('seed')) is int and 0 <= policy['seed'] < 2**63, 'invalid seed')
    require(199 <= policy.get('bootstrap_replicates', 0) <= 999, 'bootstrap budget 199..999')
    require(120 <= policy.get('calibration_simulations', 0) <= 400, 'calibration budget 120..400')
    require(type(policy['bootstrap_replicates']) is int and
            type(policy['calibration_simulations']) is int, 'integer budgets required')
    require(1 <= len(policy['reference_mix']) <= MAX_STRATA, 'stratum limit')
    keys = set()
    for cell in policy['reference_mix']:
        require(REPO.fullmatch(cell['repo']) is not None and text(cell['task']) and
                cell['size'] in ('xs', 's', 'm', 'l', 'xl'), 'invalid joint stratum')
        key = (cell['repo'], cell['task'], cell['size'])
        require(key not in keys, 'duplicate reference stratum')
        keys.add(key)
        require(number(cell['weight']) and cell['weight'] > 0, 'invalid reference weight')
    require(abs(sum(c['weight'] for c in policy['reference_mix']) - 1) < 1e-9,
            'reference weights must sum to one')
    setups = {}
    exact_keys = set()
    require(1 <= len(packet['setups']) <= MAX_GROUPS, 'setup limit')
    for ident, raw in unique(packet['setups'], 'id').items():
        dims = []
        for dim in ('provider', 'model', 'effort', 'tier'):
            s = raw[dim]
            require(s['value'] is None or text(s['value']), 'invalid setting value')
            require(s['basis'] in ('reported', 'requested', 'unknown'), 'invalid setting basis')
            require(isinstance(s['evidence_ids'], list) and all(map(text, s['evidence_ids'])),
                    'invalid setting evidence')
            dims.append(Setting(s['value'], s['basis'], tuple(s['evidence_ids'])))
        setup = Setup(ident, *dims)
        if setup.exact:
            key = tuple(s.value for s in dims)
            require(key not in exact_keys, 'duplicate exact setup under different IDs')
            exact_keys.add(key)
        setups[ident] = setup
    family = policy['family']
    require(isinstance(family, list) and len(set(family)) == len(family) and
            all(g in setups for g in family) and 2 <= len(family) <= MAX_GROUPS, 'invalid frozen family')
    require(policy['reference_setup'] in family, 'reference outside family')
    measures = {}
    require(1 <= len(packet['measures']) <= MAX_MEASURES, 'measure limit')
    for ident, raw in unique(packet['measures'], 'id').items():
        require(raw['kind'] in ('rate', 'amount') and raw['direction'] in ('higher', 'lower'), 'measure type')
        require(raw['semantics'] in CLAIMS and text(raw['unit']), 'measure semantics/unit')
        require(isinstance(raw['required_claims'], list) and
                all(map(text, raw['required_claims'])) and
                CLAIMS[raw['semantics']] <= set(raw['required_claims']), 'insufficient receipt claims')
        measures[ident] = Measure(ident, raw['kind'], raw['direction'], raw['semantics'],
                                 tuple(sorted(set(raw['required_claims']))), raw['unit'])
    require(isinstance(packet['observations'], list) and len(packet['observations']) <= MAX_ROWS, 'row limit')
    require(len(packet['sources']) <= 64 and len(packet['evidence']) <= 30000, 'provenance limit')
    return setups, measures
