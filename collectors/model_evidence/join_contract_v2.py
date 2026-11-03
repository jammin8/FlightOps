from copy import deepcopy

from . import join_contract as v1
from .capture_contract import COMMON, PAYLOADS, MANIFEST, LEDGER_GAPS, LedgerSnapshot, bounded_json
from .contract import MAX_BYTES, encode, require
from .ingest import accept_envelope
from .selected_ci_contract import INPUT as SELECTED_CI, accept_source
from .schema import obj, arr, enum, nullable, IDENT, TIME, COUNT, BOOL, HASH, FULL_SHA, REPOSITORY, validate

HEAD = deepcopy(v1.HEAD)
HEAD['properties']['replaced'] = nullable(BOOL)
HEAD['properties']['predecessor_revision'] = nullable(COUNT)
HEAD['required'].append('predecessor_revision')
EVENT = {'anyOf': [obj({**deepcopy(COMMON), 'kind': enum(kind), 'payload': deepcopy(spec)}) for kind, spec in PAYLOADS.items()]}
SNAPSHOT = obj({'schema': enum('flightops-boundary-snapshot/1'), 'manifest': nullable(deepcopy(MANIFEST)),
    'events': arr(EVENT), 'gaps': arr(enum(*LEDGER_GAPS), 64), 'as_of': nullable(TIME), 'custody': enum('supplied-replay')})
INPUT = obj({'schema': enum('evidence-head-input/2'), 'origin': enum('synthetic', 'supplied'), 'generated_at': TIME, 'agent_evidence': {'type': 'null'},
    'sources': arr(deepcopy(v1.SOURCE), 128), 'heads': arr(HEAD), 'policies': arr(deepcopy(v1.POLICY)),
    'ci_attempts': arr(deepcopy(v1.CI)), 'selected_ci_inputs': arr(deepcopy(SELECTED_CI), 8), 'boundary_snapshots': arr(SNAPSHOT, 32)})
NOTE = obj({'collection': IDENT, 'id': IDENT, 'reason': IDENT})
HEAD_RESULT = obj({'id': IDENT, 'revision': COUNT, 'replacement_state': enum('current', 'replaced', 'unknown', 'conflict'),
    'known_current': nullable(BOOL), 'reasons': arr(IDENT), 'v1_row': nullable(deepcopy(v1.HEAD))})
ASSIGNMENT = obj({'id': IDENT, 'intents': arr(deepcopy(PAYLOADS['assignment_intent'])), 'intent_at': nullable(TIME),
    'dispatches': arr(deepcopy(PAYLOADS['dispatch_observed'])), 'owners': arr(deepcopy(PAYLOADS['owner_bound'])),
    'resolutions': arr(deepcopy(PAYLOADS['assignment_resolution'])), 'owner': nullable(deepcopy(PAYLOADS['owner_bound'])),
    'decision': nullable(deepcopy(PAYLOADS['assignment_resolution'])), 'repository': nullable(REPOSITORY),
    'pr': nullable({**COUNT, 'minimum': 1}), 'result_repository': nullable(REPOSITORY),
    'result_pr': nullable({**COUNT, 'minimum': 1}), 'input_sha': nullable(FULL_SHA), 'result_sha': nullable(FULL_SHA),
    'reviewed_sha': nullable(FULL_SHA), 'actor_id': nullable(IDENT), 'consistent': BOOL,
    'input_head_ids': arr(IDENT), 'result_head_ids': arr(IDENT), 'review_head_ids': arr(IDENT),
    'known': obj({k: BOOL for k in ('repository', 'pr', 'result_repository', 'result_pr', 'input_sha', 'result_sha', 'reviewed_sha', 'actor', 'native_owner', 'decision')}),
    'reasons': arr(IDENT), 'v1_row': nullable(deepcopy(v1.ASSIGNMENT)), 'v1_dispositions': arr(IDENT)})
OPERATION = obj({'id': IDENT, 'events': arr(EVENT), 'terminal_state': enum('in_progress', 'success', 'failure', 'cancelled', 'unknown'),
    'owned_process_known': BOOL, 'git_observations': arr(obj({'observation': deepcopy(PAYLOADS['git_result']), 'dispositions': arr(IDENT)})), 'reasons': arr(IDENT)})
COMPATIBILITY = obj({'v1_input': nullable(deepcopy(v1.INPUT)), 'omitted_head_ids': arr(IDENT),
    'omitted_assignment_ids': arr(IDENT), 'reasons': arr(IDENT)})
DENOMINATORS = obj({k: COUNT for k in ('selected_heads', 'selected_pr_identities', 'selected_head_observations',
    'unknown_replacement_heads', 'conflicting_heads', 'selected_events', 'selected_assignments', 'explicit_decisions',
    'native_owner_mappings', 'selected_operations', 'unknown_owned_processes', 'selected_agent_runs', 'selected_agent_turns',
    'accepted_turns', 'captured_responses', 'known_usage_responses', 'reconciled_main_turns', 'exact_pr_linked_usage',
    'authenticated_rows', 'known_child_allocation_runs', 'retention_complete_sources')})
OUTPUT = obj({'schema': enum('evidence-head-joins/2'), 'generated_at': TIME, 'input_digest': HASH,
    'input': deepcopy(INPUT), 'heads': arr(HEAD_RESULT), 'assignments': arr(ASSIGNMENT), 'operations': arr(OPERATION),
    'ci': arr(deepcopy(v1.CI_RESULT)), 'dispositions': arr(NOTE, 25000), 'compatibility': COMPATIBILITY,
    'denominators': DENOMINATORS, 'gaps': arr(IDENT), 'authentication': enum('not-performed'),
    'merge_authority': enum(False), 'normalized_rows': arr({'type': 'null'}, 0)})


def agent_spec():
    return {'anyOf': [{'type': 'null'}, {'$ref': 'urn:flightops:agent-evidence:1'}, {'$ref': 'urn:flightops:agent-evidence:2'}]}


def document(spec, name):
    out = deepcopy(spec)
    if name == 'evidence-head-input:2':
        out['properties']['agent_evidence'] = agent_spec()
    else:
        out['properties']['input']['properties']['agent_evidence'] = agent_spec()
        out['properties']['compatibility']['properties']['v1_input']['anyOf'][0]['properties']['agent_evidence'] = {'$ref': 'urn:flightops:agent-evidence:2'}
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'urn:flightops:' + name, **out}


INPUT_JSON_SCHEMA = document(INPUT, 'evidence-head-input:2')
OUTPUT_JSON_SCHEMA = document(OUTPUT, 'evidence-head-joins:2')


def bounded(value):
    try:
        raw = value if type(value) is bytes else encode(value)
        return bounded_json(raw, MAX_BYTES)
    except (ValueError, TypeError, RecursionError):
        require(False, 'join-v2-json')


def accept_input(value, *, check=lambda: None):
    check()
    b = bounded(value)
    require(type(b) is dict and 'agent_evidence' in b, 'join-v2-fields')
    require(type(b.get('selected_ci_inputs')) is list and len(b['selected_ci_inputs']) <= 8, 'join-v2-selected-count')
    validate({**b, 'agent_evidence': None, 'selected_ci_inputs': []}, INPUT)
    agent = b['agent_evidence']
    if agent is not None:
        accepted = accept_envelope(agent)
        require(accepted == agent, 'join-v2-agent-not-exact')
        require(agent['generated_at'] <= b['generated_at'], 'join-v2-agent-clock')
    sources = {s['id']: s for s in b['sources']}
    require(len(sources) == len(b['sources']), 'join-v2-source-duplicate')
    require(not set(sources) & {s['id'] for s in (agent or {}).get('sources', [])}, 'join-v2-source-collision')
    for s in sources.values():
        check()
        require(s['snapshot_at'] <= b['generated_at'] and (s['generated_at'] is None or s['generated_at'] <= s['snapshot_at'])
            and (s['complete_through'] is None or s['complete_through'] <= s['snapshot_at']), 'join-v2-source-clock')
    for collection in ('heads', 'policies', 'ci_attempts'):
        for row in b[collection]:
            check()
            e = row['evidence']
            s = sources.get(e['source_id'])
            require(s is not None and e['source_digest'] == s['digest'] and e['at'] <= s['snapshot_at'], 'join-v2-provenance')
            require(row['repository'] == row['repository'].lower(), 'join-v2-repository')
            for field in ('anchor', 'observed', 'read', 'refresh_failed', 'policy_read', 'started_at', 'completed_at'):
                require(row.get(field) is None or row[field] <= e['at'], 'join-v2-receipt-clock')
    for selected in b['selected_ci_inputs']:
        check()
        accept_source(encode(selected))
    for snapshot in b['boundary_snapshots']:
        check()
        expected = LedgerSnapshot(snapshot['manifest'], snapshot['events'], snapshot['gaps'], snapshot['as_of']).fields()
        require(snapshot == expected, 'join-v2-snapshot')
        require(snapshot['as_of'] is None or snapshot['as_of'] <= b['generated_at'], 'join-v2-snapshot-clock')
    check()
    return deepcopy(b)


def validated_output(value, *, check=lambda: None):
    check()
    out = bounded(value)
    require(type(out) is dict and 'input' in out and 'compatibility' in out, 'join-v2-output')
    inner = accept_input(out['input'], check=check)
    stub = deepcopy(out)
    stub['input']['agent_evidence'] = None
    stub['input']['selected_ci_inputs'] = []
    if stub['compatibility']['v1_input'] is not None:
        stub['compatibility']['v1_input']['agent_evidence'] = None
    validate(stub, OUTPUT)
    if out['compatibility']['v1_input'] is not None:
        v1.accept_input(out['compatibility']['v1_input'])
    require(out['generated_at'] == inner['generated_at'], 'join-v2-output-clock')
    return out


def accept_output(value, *, check=lambda: None):
    out = validated_output(value, check=check)
    from .joins_v2 import derive
    require(out == derive(out['input'], check=check), 'join-v2-derived-output')
    return out
