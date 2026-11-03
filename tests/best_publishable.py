#!/usr/bin/env python3
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import best_support as support

with open(sys.argv[1], "w") as f:
    json.dump(support.through_live_client(support.capture()), f, sort_keys=True)
