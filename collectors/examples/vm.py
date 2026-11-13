#!/usr/bin/env python3
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import machine  # noqa: E402

NO_AGENTS = "no orchestrator adapter configured"


def build(now=time.time, read=machine.counters):
    return {"collected": now(), "vm": read(), "agents_error": NO_AGENTS}


if __name__ == "__main__":
    json.dump(build(), sys.stdout)
