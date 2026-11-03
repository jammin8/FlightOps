import argparse
import sys
import signal

from collectors.runs import atomic_write, read_file
from .contract import ContractError, decode
from .exporter import export_bundle
from .ingest import accept_envelope


class ExportTimeout(Exception):
    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit offline paths only: python3 -m collectors.model_evidence IN OUT.')
    parser.add_argument("bundle")
    parser.add_argument("output")
    parser.add_argument("--timeout", type=int, choices=range(1, 31), default=5,
                        help="POSIX offline processing deadline in seconds (1..30)")
    args = parser.parse_args(argv)
    cancelled = False
    def expired(signum, frame):
        nonlocal cancelled
        cancelled = True
        raise ExportTimeout()

    prior = signal.signal(signal.SIGALRM, expired)
    signal.alarm(args.timeout)
    try:
        value = accept_envelope(export_bundle(decode(read_file(args.bundle))))
        if cancelled:
            raise ExportTimeout()
        atomic_write(args.output, value)
    except (ExportTimeout, OSError, ContractError, RecursionError, TypeError):
        print("offline evidence export failed: invalid-or-unavailable-input", file=sys.stderr)
        return 2
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prior)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
