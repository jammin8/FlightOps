from datetime import datetime

from .contract import ContractError, identifier, require, stamp

NATIVE_USAGE = {'input_tokens': 'input_tokens', 'cached_input_tokens': 'cached_input_tokens',
                'cache_write_input_tokens': 'cache_creation_tokens', 'output_tokens': 'output_tokens',
                'reasoning_output_tokens': 'reasoning_tokens'}
ORCH_USAGE = {'inputTokens': 'input_tokens', 'cachedInputTokens': 'cached_input_tokens',
            'cacheCreationTokens': 'cache_creation_tokens', 'outputTokens': 'output_tokens',
            'reasoningTokens': 'reasoning_tokens'}


def timestamp(value):
    if stamp(value):
        return value
    require(isinstance(value, str) and len(value) <= 40 and value.endswith(('Z', '+00:00')), 'timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ContractError('timestamp') from exc
    require(stamp(result), 'timestamp')
    return result


def project_record(raw):
    require(isinstance(raw, dict), 'native-record')
    p = raw.get('payload')
    require(isinstance(p, dict), 'native-payload')
    kind = raw.get('type')
    out = {}
    if kind == 'session_meta':
        out = {k: p.get(k) for k in ('id', 'timestamp', 'cli_version', 'model_provider')}
    elif kind == 'token_usage_record':
        out = {k: p.get(k) for k in ('response_id', 'root_turn_id', 'session_id', 'thread_id', 'turn_id')}
        out['usage'] = {k: p.get('usage', {}).get(k) for k in NATIVE_USAGE} if isinstance(p.get('usage'), dict) else {}
    elif kind == 'turn_context':
        out = {k: p.get(k) for k in ('turn_id', 'root_turn_id', 'model', 'effort')}
    elif kind == 'event_msg' and p.get('type') in ('task_started', 'task_complete'):
        out = {k: p.get(k) for k in ('type', 'turn_id', 'root_turn_id', 'started_at', 'completed_at') if k in p}
    elif kind == 'event_msg' and p.get('type') == 'thread_settings_applied':
        settings = p.get('thread_settings')
        out = {'type': p['type'], 'thread_id': p.get('thread_id'), 'thread_settings':
               {k: settings.get(k) for k in ('model', 'model_provider_id', 'service_tier', 'reasoning_effort')}
               if isinstance(settings, dict) else {}}
    elif kind == 'event_msg' and (p.get('type')=='unqualified-command-lifecycle' or
            p.get('type')=='item_completed' and isinstance(p.get('item'),dict) and p['item'].get('type')=='CommandExecution'):
        out = {'type':'unqualified-command-lifecycle','thread_id':p.get('thread_id'),'turn_id':p.get('turn_id')}
    elif kind == 'response_item' and p.get('type') in ('custom_tool_call', 'function_call', 'custom_tool_call_output', 'function_call_output', 'unsupported-operation'):
        out = {'type': 'unsupported-operation'}
    else:
        return None
    return {'timestamp': raw.get('timestamp'), 'type': kind, 'payload': out}


def usage(raw, mapping=NATIVE_USAGE):
    require(isinstance(raw, dict), 'native-usage')
    return {target: raw.get(source) for source, target in mapping.items()}


def parse_snapshot(snapshot, session, turn_ids, as_of):
    require(isinstance(snapshot, dict) and isinstance(snapshot.get('records'), list), 'native-snapshot')
    rows, gaps, metas = [], set(snapshot.get('gaps', [])), []
    for row in snapshot['records']:
        try:
            record = project_record(row)
            if record is None:
                continue
            record['line'] = row.get('line')
            require(type(record['line']) is int and 0 < record['line'] <= 2**53 - 1, 'native-line')
            at = timestamp(record['timestamp'])
            if at > as_of:
                gaps.add('after-as-of-records')
                continue
            record['at'] = at
            p = record['payload']
            if record['type'] == 'session_meta':
                require(p.get('id') == session, 'native-session-mismatch')
                metas.append(record)
                if p.get('cli_version')!='0.166.0':gaps.add('unqualified-native-version')
            elif p.get('type') == 'unqualified-command-lifecycle':
                if p.get('thread_id')!=session or not identifier(p.get('turn_id')):
                    gaps.add('command-owner-unknown')
                elif p['turn_id'] in turn_ids:
                    gaps.add('unqualified-command-lifecycle')
                continue
            elif p.get('type') == 'unsupported-operation':
                gaps.add('unsupported-operation-binding')
                continue
            elif record['type'] == 'token_usage_record':
                require(p.get('session_id') == session and p.get('thread_id') == session, 'native-session-mismatch')
                if p.get('turn_id') not in turn_ids:
                    if p.get('root_turn_id') in turn_ids: gaps.add('native-child-unsupported')
                    continue
                require(identifier(p.get('response_id')) and identifier(p.get('root_turn_id')), 'native-identity')
                if p['root_turn_id'] != p['turn_id']:
                    gaps.add('native-child-unsupported')
                    continue
            elif p.get('type') == 'thread_settings_applied':
                require(p.get('thread_id') == session, 'native-session-mismatch')
            elif p.get('turn_id') not in turn_ids:
                continue
            elif p.get('root_turn_id') != p.get('turn_id'):
                gaps.add('native-child-unsupported')
                continue
            rows.append(record)
        except (ContractError, TypeError):
            gaps.add('invalid-native-record')
    require(len(metas) >= 1 and all(m['payload']['id'] == session for m in metas), 'native-session-missing')
    return sorted(rows, key=lambda r: (r['at'], r['line'])), sorted(gaps)
