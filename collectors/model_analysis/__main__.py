
import argparse
import json
from pathlib import Path
import sys

from .cohort import build_manifest
from .contract import ContractError, MAX_BYTES, canonical, digest, require
from .engine import decide


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        require(k not in result, 'duplicate JSON object key: ' + k)
        result[k] = v
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='python3 -m collectors.model_analysis {freeze,replay} INPUT --output NEW_FILE')
    parser.add_argument('mode', choices=['freeze', 'replay'])
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='new file; overwrite refused')
    args = parser.parse_args(argv)
    try:
        limit = MAX_BYTES if args.mode == 'freeze' else 32 * 1024 * 1024
        with args.input.open('rb') as stream:
            data = stream.read(limit+1)
        require(len(data) <= limit, 'input byte limit')
        packet = json.loads(data, object_pairs_hook=_pairs,
                            parse_constant=lambda s: (_ for _ in ()).throw(ContractError('nonfinite JSON')))
        if args.mode == 'freeze':
            manifest = build_manifest(packet)
            result = {'schema': 'flightops-analysis-freeze/1', 'normalized_input': packet, 'manifest': manifest}
            result['freeze_hash'] = digest(result)
        else:
            require(packet['schema'] == 'flightops-analysis-freeze/1', 'unsupported freeze schema')
            require(packet['freeze_hash'] == digest({k: v for k, v in packet.items() if k != 'freeze_hash'}),
                    'freeze hash mismatch')
            manifest = build_manifest(packet['normalized_input'], replaying=True)
            require(canonical(manifest) == canonical(packet['manifest']), 'manifest does not replay from normalized input')
            result = decide(manifest)
        output = canonical(result) + b'\n'
        require(len(output) <= 32 * 1024 * 1024, 'output byte limit')
        with args.output.open('xb') as stream:
            stream.write(output)
        print(digest(result))
        return 0
    except (ContractError, OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        print(f'model-analysis refusal: {type(exc).__name__}: {str(exc)[:300]}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
