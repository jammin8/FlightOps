#!/usr/bin/env python3
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import machine  # noqa: E402


def build(now=time.time, read=machine.host):
    return {"collected": now(), "host": read(), "vm_skipped": True}


if __name__ == "__main__":
    json.dump(build(), sys.stdout)
