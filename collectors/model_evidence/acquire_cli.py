import argparse
from pathlib import Path
import signal
import sys
import time

from collectors.runs import atomic_write
from .__main__ import ExportTimeout
from .acquisition import acquire
from .contract import ContractError, require
from .projection import AcquisitionCancelled


def main(argv=None):
    p = argparse.ArgumentParser(description='Explicit read-only acquisition to one explicitly supplied publication path.\n\npython3 -m collectors.model_evidence.acquire_cli --help\nNo scheduler, cache, default source home, credential discovery or app activation.\n')
    p.add_argument('--sqlite', required=True)
    p.add_argument('--native-root', action='append', required=True)
    p.add_argument('--project', action='append', required=True, help='exact allowlisted registry project ID; preserve its prefix if present')
    p.add_argument('--window-start', type=float, required=True, help='Unix seconds; maximum span 90 days')
    p.add_argument('--as-of', type=float, required=True, help='frozen window end, Unix seconds')
    p.add_argument('--output', required=True)
    p.add_argument('--timeout', type=int, choices=range(1,31), default=5)
    a = p.parse_args(argv)
    cancelled = False
    def expired(signum, frame):
        nonlocal cancelled
        cancelled = True
        raise ExportTimeout()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(a.timeout)
    try:
        output=Path(a.output).resolve()
        database=Path(a.sqlite).resolve()
        require(output not in (database,Path(str(database)+'-wal'),Path(str(database)+'-shm')), 'output-overlaps-source')
        require(all(not output.is_relative_to(Path(root).resolve()) for root in a.native_root), 'output-overlaps-source')
        def check():
            if cancelled: raise ExportTimeout()
        value = acquire({'sqlite_path':Path(a.sqlite),'native_roots':[Path(x) for x in a.native_root],
                         'projects':a.project,'window':{'start':a.window_start,'end':a.as_of}}, acquired_at=time.time(), check=check)
        check()
        atomic_write(a.output,value)
    except (ContractError, OSError, TypeError, ValueError, AcquisitionCancelled, ExportTimeout):
        print('read-only evidence acquisition failed: invalid-unavailable-or-interrupted-source',file=sys.stderr)
        return 2
    finally:
        signal.alarm(0);signal.signal(signal.SIGALRM,previous)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
