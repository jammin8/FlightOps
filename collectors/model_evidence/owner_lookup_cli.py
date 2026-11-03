import argparse
import os
from pathlib import Path
import stat
import sys

from .owner_lookup import observe_exact_run, _close_fd
from .owner_lookup_contract import DATABASE_PATH,MAX_BYTES,LookupError,LookupCancelled,accept_selector


class _Parser(argparse.ArgumentParser):
    def error(self,message):
        raise LookupError('cli-arguments')


def _selector_file(path):
    target=Path(path);fd=None
    try:
        if not target.is_absolute() or not target.is_relative_to('/tmp') or '..' in target.parts:raise LookupError('selector-file')
        parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:
            for part in target.parent.parts[1:]:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                os.close(parent);parent=child
            fd=os.open(target.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        finally:os.close(parent)
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_BYTES:raise LookupError('selector-file')
        raw=bytearray()
        while True:
            part=os.read(fd,min(65536,MAX_BYTES+1-len(raw)))
            if not part:break
            raw.extend(part)
            if len(raw)>MAX_BYTES:raise LookupError('selector-file')
        after=os.fstat(fd);entry=os.stat(target,follow_symlinks=False)
        if any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(entry,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):raise LookupError('selector-file')
        return accept_selector(bytes(raw))
    except (OSError,ValueError):raise LookupError('selector-file') from None
    finally:
        if fd is not None:_close_fd(fd,sys.exc_info()[1])


def main(argv=None):
    parser=_Parser(allow_abbrev=False,description='Explicit exact-run read-only owner lookup; no discovery, native reads, sends or scheduler.')
    parser.add_argument('--selector',required=True)
    parser.add_argument('--database',required=True)
    parser.add_argument('--output',required=True)
    try:
        args=parser.parse_args(argv)
        if args.database!=DATABASE_PATH:raise LookupError('database-path')
        selector=_selector_file(args.selector)
        observe_exact_run(selector,args.database,args.output)
        return 0
    except (LookupCancelled,KeyboardInterrupt):
        print('owner-lookup-cancelled-or-deadline',file=sys.stderr)
        return 130
    except (LookupError,OSError,ValueError):
        print('owner-lookup-invalid-unavailable-or-publication-failed',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
