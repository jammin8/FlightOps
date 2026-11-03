from copy import deepcopy

from .contract import DIMENSIONS, MAX_BYTES, encode, identifier, provenance, require
from .exporter import derive_health
from .schema import (SCHEMA as FIXTURE, obj, arr, enum, nullable, IDENT, TIME, COUNT, BOOL,
                     EVIDENCE, HASH, USAGE, SOURCE, validate)

BASES = ('unknown', 'orch-request', 'native-session', 'native-turn-context', 'native-settings-applied')
SETTING = obj({'value': nullable(IDENT), 'basis': enum(*BASES), 'evidence': nullable(EVIDENCE)})
SETTINGS = obj({k: SETTING for k in DIMENSIONS})
RUN = deepcopy(FIXTURE['properties']['runs']['items'])
RUN['properties']['turns']['items']['properties']['requested'] = SETTINGS
RUN['properties']['turns']['items']['properties']['reported'] = SETTINGS
REQUEST = deepcopy(FIXTURE['properties']['requests']['items'])
REQUEST['properties']['reported'] = SETTINGS
MATCH = obj({k: nullable(BOOL) for k in ('fresh_input','cache_read','cache_write','output','reasoning')})
RECON = obj({'run_id': IDENT, 'native_turn_id': IDENT, 'expected': USAGE, 'observed': USAGE,
             'matches': MATCH, 'state': enum('match','mismatch','unknown'), 'request_count': COUNT})
OBSERVATION = obj({'native_session_id': IDENT, 'native_turn_id': nullable(IDENT), 'at': TIME,
                   'settings': SETTINGS, 'evidence': EVIDENCE})
LINK = obj({'run_id': IDENT, 'thread_id': IDENT, 'project_id': IDENT, 'provider_thread_id': IDENT,
            'attempt_id': IDENT, 'native_session_id': IDENT, 'parent_thread_id': nullable(IDENT),
            'child_thread_ids': arr(IDENT)})
PROPERTIES = deepcopy(FIXTURE['properties'])
PROPERTIES.update({
    'schema': enum('agent-evidence/2'), 'deriver_version': enum('flightops-orch-native/1'),
    'origin': obj({'kind': enum('readonly-acquisition','metadata-replay','synthetic-replay')}),
    'source_identity': obj({'snapshot_digest': HASH, 'host': {'type':'null'}, 'basis': enum('orch-native-readonly')}),
    'snapshots': arr(obj({'source_id':IDENT,'native_session_id':IDENT,'bytes_read':nullable(COUNT),
                         'device':nullable(COUNT),'inode':nullable(COUNT),'last_line':COUNT}),32),
    'acquired_at': TIME, 'sources': arr(SOURCE,33), 'runs': arr(RUN), 'requests': arr(REQUEST), 'operations': arr(FIXTURE['properties']['operations']['items'],0),
    'links': arr(LINK), 'settings_observations': arr(OBSERVATION), 'reconciliation': arr(RECON),
    'inventory': obj({'projects': arr(IDENT,5), 'run_count': COUNT, 'attempt_count': COUNT, 'turn_count': COUNT,
                      'native_snapshot_count': COUNT, 'compatible_turns': COUNT, 'unsupported_turns': COUNT,
                      'gaps': arr(IDENT)})})
SCHEMA = obj(PROPERTIES)
JSON_SCHEMA = {'$schema':'https://json-schema.org/draft/2020-12/schema', '$id':'urn:flightops:agent-evidence:2', **SCHEMA}


def setting(value, sources, requested=False):
    if value is None:
        return {'value':None, 'basis':'unknown', 'evidence':None}
    require(isinstance(value, dict) and identifier(value.get('value')), 'setting')
    allowed = ('orch-request',) if requested else BASES[2:]
    require(value.get('basis') in allowed, 'setting-basis')
    return {'value':value['value'], 'basis':value['basis'], 'evidence':provenance(value.get('evidence'), sources)}


def accept_observed(value):
    require(isinstance(value, dict) and len(encode(value)) <= MAX_BYTES, 'observed-size')
    validate(value, SCHEMA)
    require(value['window']['start'] <= value['window']['end'] == value['as_of'] <= value['acquired_at'] == value['generated_at'], 'observed-clock')
    require(value['window']['end']-value['window']['start'] <= 90*86400, 'observed-window')
    sources = {s['id']: s for s in value['sources']}
    require(len(sources) == len(value['sources']) and sources, 'observed-sources')
    for s in sources.values():
        require(s['snapshot_at'] <= value['acquired_at'], 'source-clock')
        require(s['complete_through'] is None, 'history-watermark-unqualified')
        require(s['history_start'] is None or s['history_end'] is not None and s['history_start'] <= s['history_end'] <= s['snapshot_at'], 'source-bounds')
    def walk(node):
        if isinstance(node, dict):
            if set(node) == {'source_id','source_digest','line','at'}:
                require(node['source_id'] in sources and node['source_digest'] == sources[node['source_id']]['digest'], 'source-lineage')
                require(node['at'] <= sources[node['source_id']]['snapshot_at'], 'evidence-clock')
            if set(node) == {'value','basis','evidence'}:
                require((node['value'] is None) == (node['basis']=='unknown') == (node['evidence'] is None), 'setting-knownness')
            if set(node) == set(USAGE['properties']):
                buckets = node['buckets']
                require(node['known'] == {k:v is not None for k,v in buckets.items()} and node['complete'] == all(node['known'].values()), 'usage-knownness')
                require(buckets['output'] == node['output_total'], 'usage-output')
                require(buckets['reasoning'] is None or buckets['output'] is None or buckets['reasoning'] <= buckets['output'], 'usage-reasoning')
                if all(buckets[k] is not None for k in ('fresh_input','cache_read','cache_write')):
                    require(sum(buckets[k] for k in ('fresh_input','cache_read','cache_write')) == node['input_total'], 'usage-input')
            for v in node.values(): walk(v)
        elif isinstance(node, list):
            for v in node: walk(v)
    walk(value)
    runs = {r['id']:r for r in value['runs']}
    require(len(runs) == len(value['runs']), 'duplicate-run')
    for r in runs.values():
        require(r['id'] == 'codex:'+r['attempt_id'] and r['provider_namespace']=='codex', 'observed-run-id')
        require(all(r[k] is None or r[k]<=value['as_of'] for k in ('started_at','ended_at')), 'run-clock')
        require(r['allocation']=='unknown' and r['memberships']==[], 'pr-joins-out-of-scope')
        for t in r['turns']:
            require(all(v['value'] is None or v['basis']=='orch-request' for v in t['requested'].values()), 'requested-basis')
            require(all(v['value'] is None or v['basis'] in BASES[2:] for v in t['reported'].values()), 'reported-basis')
    for link in value['links']:
        require(link['run_id'] in runs and link['project_id'] in value['inventory']['projects'], 'projection-link')
        require(link['attempt_id']==runs[link['run_id']]['attempt_id'] and link['native_session_id']==runs[link['run_id']]['native_session_id'], 'projection-link')
    for snapshot in value['snapshots']:
        require(snapshot['source_id'] in sources, 'snapshot-source')
    ids = set()
    for q in value['requests']:
        require(q['id'] not in ids and q['id']=='codex:'+q['native_request_id'], 'request-id'); ids.add(q['id'])
        owner = q['original_run_id']
        require(owner is None and q['conflict'] or owner in runs and q['original_native_turn_id'] in {t['native_turn_id'] for t in runs[owner]['turns']}, 'request-owner')
        require(q['allocation']=='unknown', 'request-allocation')
        require(bool(q['final_sources']), 'request-final-source')
        require(all(v['value'] is None or v['basis'] in BASES[2:] for v in q['reported'].values()), 'reported-basis')
        require(not q['conflict'] or not any(q['usage']['known'].values()), 'conflict-usage')
    from .adapter import reconcile
    expected = {(r['run_id'],r['native_turn_id']):r['expected'] for r in value['reconciliation']}
    recomputed = reconcile(value['runs'], value['requests'], expected)
    require(value['reconciliation'] == recomputed, 'reconciliation-derived')
    out = deepcopy(value)
    reasons = derive_health(out['sources'],out['runs'],out['requests'],out['errors'],out['as_of'],out['window'])
    if out['inventory']['gaps'] or any(r['state']!='match' for r in recomputed):
        reasons['state']='partial'
        reasons['reasons']=sorted(set(reasons['reasons']+['acquisition-gaps']))
    out['health'] = reasons
    return out
