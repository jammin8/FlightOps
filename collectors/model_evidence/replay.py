from .adapter import derive_observed
from .contract import digest, require
from .native import project_record, timestamp


def replay_owned(fixture, thread_metadata, *, correction=None, origin='metadata-replay'):
    require(fixture.get('schema')=='flightops-owned-runtime-metadata-fixture/1','owned-fixture-schema')
    require(thread_metadata.get('id')==fixture['run']['threadId'],'owned-thread-id')
    rows = {r['line']:r for r in fixture['native']['records']}
    gaps=[]
    if correction is not None:
        require(correction.get('schema')=='flightops-owned-native-settings-order-fixture/1' and
                correction.get('native_session')==fixture['attempts'][0]['nativeThreadId'],'settings-correction')
        for r in correction['records']:
            raw={'line':r['line'],'timestamp':r['timestamp'],'type':'event_msg',
                 'payload':{k:v for k,v in r.items() if k not in ('line','timestamp')}}
            if r['line'] in rows and project_record(rows[r['line']])!=project_record(raw):
                gaps.append('correction-conflict')
            elif r['line'] not in rows: rows[r['line']]=raw
    records=[rows[k] for k in sorted(rows)]
    projection={'threads':[thread_metadata],'runs':[fixture['run']],'run_attempts':fixture['attempts'],
                'provider_threads':[fixture['provider']],'provider_turns':fixture['turns'],'gaps':gaps}
    session=fixture['attempts'][0]['nativeThreadId']
    snapshots={session:[{'records':records,'digest':digest(records),'gaps':[],
                         'operations_omitted':bool(fixture['native'].get('tool_shapes'))}]}
    at=timestamp(fixture['observed_at'])
    return derive_observed(projection,snapshots,projects=[thread_metadata['projectId']],
                           window={'start':at-90*86400,'end':at},as_of=at,acquired_at=at,origin=origin)
