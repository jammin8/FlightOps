from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import stat

from .contract import ContractError, decode, require, stamp
from .native import project_record
from .projection import read_projection

SESSION = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
ALLOWED_PROJECTS = frozenset((
    'mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914', '15422c94-4afe-4fff-b056-f3b240bc63ce',
    '93db29b0-fce2-415c-9c1b-48a687f8302f', '175492b5-29c1-4884-9919-3b56a5d0f1c9',
    '851362d8-b65b-4e0e-8781-53c2aadaa12e'))


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 8*1024*1024
    total_bytes: int = 32*1024*1024
    files: int = 32
    entries: int = 2000
    lines: int = 100000
    line_bytes: int = 1024*1024

    def validate(self):
        caps = type(self)()
        for key, value in vars(self).items():
            require(type(value) is int and 0 < value <= getattr(caps, key), 'native-limits')


def safe_path(path, root):
    path, root = Path(path), Path(root)
    require(path.is_absolute() and root.is_absolute() and '..' not in path.parts and '..' not in root.parts, 'native-path')
    require(path.is_relative_to(root), 'native-path-scope')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        require(not current.is_symlink(), 'native-symlink')
    return path


def read_native(path, root, limits, *, check=lambda: None, remaining=None):
    limits.validate()
    path = safe_path(path, root)
    require(path.suffix == '.jsonl' and path.name.startswith('rollout-'), 'native-filename')
    cap = min(limits.file_bytes, limits.total_bytes if remaining is None else remaining)
    require(cap > 0, 'native-total-byte-limit')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    gaps, records, h, used = set(), [], hashlib.sha256(), 0
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode), 'native-not-regular')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            for line in range(1, limits.lines+1):
                check()
                raw = f.readline(min(limits.line_bytes+1, cap-used+1))
                if not raw:
                    break
                if len(raw) > limits.line_bytes or used+len(raw) > cap:
                    gaps.add('native-byte-or-line-limit'); break
                used += len(raw); h.update(raw)
                if not raw.endswith(b'\n'):
                    gaps.add('native-interrupted-line'); break
                try:
                    obj = decode(raw)
                    projected = project_record(obj)
                    if projected is not None:
                        if len(records)>=5000:
                            gaps.add('native-metadata-limit');break
                        records.append({'line': line, **projected})
                except (ValueError, TypeError, ContractError, RecursionError):
                    gaps.add('invalid-native-record')
            else:
                gaps.add('native-line-limit')
            after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            gaps.add('native-changed-during-read')
        if used != before.st_size:
            gaps.add('native-prefix-only')
        check()
    finally:
        os.close(fd)
    return {'records': records, 'digest': h.hexdigest(), 'bytes': used, 'device': before.st_dev, 'inode': before.st_ino,
            'gaps': sorted(gaps), 'operations_omitted': False}


def discover(roots, sessions, window, limits, *, explicit_paths=None, check=lambda: None):
    limits.validate()
    require(0 < len(roots) <= 2 and all(Path(r).is_absolute() for r in roots), 'native-roots')
    require(all(isinstance(s, str) and SESSION.fullmatch(s) for s in sessions), 'native-session-id')
    paths, gaps, visited, entries = {}, set(), set(), 0
    for session, supplied in (explicit_paths or {}).items():
        require(session in sessions, 'explicit-session-scope')
        matching = [r for r in roots if Path(supplied).is_relative_to(Path(r))]
        require(len(matching) == 1, 'explicit-path-scope')
        paths.setdefault(session, []).append((safe_path(supplied, matching[0]), Path(matching[0])))
    start = datetime.fromtimestamp(window['start'], timezone.utc).date()
    end = datetime.fromtimestamp(window['end'], timezone.utc).date()
    require(0 <= (end-start).days <= 90, 'window')
    for root in map(Path, roots):
        directories = [root] + [root / str((start+timedelta(days=d)).year) / f'{(start+timedelta(days=d)).month:02}' / f'{(start+timedelta(days=d)).day:02}' for d in range((end-start).days+1)]
        for directory in directories:
            check()
            safe_path(directory, root)
            if not directory.exists():
                continue
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    check(); entries += 1
                    if entries > limits.entries:
                        gaps.add('native-inventory-limit'); break
                    if not entry.name.startswith('rollout-') or not entry.name.endswith('.jsonl'):
                        continue
                    session = entry.name[-42:-6]
                    if session in sessions and session not in (explicit_paths or {}):
                        candidate = safe_path(Path(entry.path), root)
                        if candidate not in visited:
                            paths.setdefault(session, []).append((candidate, root)); visited.add(candidate)
            if entries > limits.entries:
                break
        if entries > limits.entries:
            break
    return paths, sorted(gaps)


def acquire(scope, *, acquired_at, check=lambda: None):
    from .adapter import derive_observed
    require(stamp(acquired_at), 'acquired-at')
    projects, window, roots = scope['projects'], scope['window'], scope['native_roots']
    require(set(projects) <= ALLOWED_PROJECTS, 'project-allowlist')
    require(window['end'] <= acquired_at, 'window-future')
    limits = scope.get('limits', Limits()); limits.validate()
    projection = read_projection(scope['sqlite_path'], projects, window, check=check)
    providers = {p['id']:p for p in projection['provider_threads']}
    sessions = {a.get('nativeThreadId') for a in projection['run_attempts'] if providers.get(a.get('providerThreadId'),{}).get('driver') == 'codex' and isinstance(a.get('nativeThreadId'), str) and SESSION.fullmatch(a['nativeThreadId'])}
    paths, gaps = discover(roots, sessions, window, limits, explicit_paths=scope.get('native_paths'), check=check)
    snapshots, total, files, metadata_count = {}, 0, 0, 0
    for session in sorted(sessions):
        for path, root in sorted(paths.get(session, [])):
            check()
            if files == limits.files or total >= limits.total_bytes or metadata_count >= 10000:
                gaps.append('native-file-or-total-limit'); break
            files += 1
            try:
                snapshot = read_native(path, root, limits, check=check, remaining=limits.total_bytes-total)
                total += snapshot['bytes']
                remaining_records=10000-metadata_count
                if len(snapshot['records'])>remaining_records:
                    snapshot['records']=snapshot['records'][:remaining_records]
                    snapshot['gaps'].append('native-global-metadata-limit')
                metadata_count += len(snapshot['records'])
                snapshots.setdefault(session, []).append(snapshot)
            except (OSError, ContractError):
                gaps.append('native-unavailable')
    projection['gaps'] = sorted(set(projection['gaps'] + gaps))
    result = derive_observed(projection, snapshots, projects=projects, window=window, as_of=window['end'], acquired_at=acquired_at,
                             origin='metadata-replay', check=check)
    result['origin']['kind'] = 'readonly-acquisition'
    from .observed_contract import accept_observed
    return accept_observed(result)
