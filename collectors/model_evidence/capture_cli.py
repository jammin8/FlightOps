import argparse
import sys

from .contract import ContractError,encode
from .capture_contract import CaptureCancelled
from .capture_ledger import Ledger,read_input,write_output
from .capture_projection import import_supplied,project


class Parser(argparse.ArgumentParser):
    def error(self,message):
        raise ContractError('capture-cli-arguments')


def main(argv=None,*,check=lambda:None):
    parser=Parser(prog='flightops-boundary')
    sub=parser.add_subparsers(dest='action',required=True)
    supplied=sub.add_parser('validate');supplied.add_argument('--input',required=True);supplied.add_argument('--output',required=True)
    for name in ('replay','seal'):
        child=sub.add_parser(name);child.add_argument('--ledger',required=True);child.add_argument('--recorder',required=True)
        child.add_argument('--epoch',required=True);child.add_argument('--output',required=True)
    try:
        args=parser.parse_args(argv)
        check()
        if args.action=='validate':result=import_supplied(read_input(args.input,check=check),check=check)
        else:
            ledger=Ledger(args.ledger,args.recorder,args.epoch,check=check)
            if args.action=='seal':ledger.seal_snapshot(args.output);return 0
            snapshot=ledger.snapshot();result=project(snapshot['events'],ledger_gaps=snapshot['gaps'],check=check)
        check();write_output(args.output,encode(result),check=check)
        return 0
    except CaptureCancelled:
        print('capture-cancelled',file=sys.stderr);return 130
    except (ContractError,OSError,TypeError,ValueError,RecursionError):
        print('capture-input-or-output-unavailable',file=sys.stderr);return 2


if __name__=='__main__':
    raise SystemExit(main())
