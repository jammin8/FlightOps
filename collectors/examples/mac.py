#!/usr/bin/env python3
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

NOT_CONFIGURED = {"ios_ci": "no local CI adapter configured", "codex": "no usage adapter configured",
                  "agents": "no agent counter configured"}


def github_budget():
    import repos
    return repos.gh_budget()


def build(now=time.time, budget=github_budget):
    try:
        github = {"value": budget(), "fetched": now()}
    except Exception as e:
        github = {"value": None, "fetched": None, "error": str(e)[:120] or type(e).__name__}
    return {"collected": now(), "github": github, "errors": dict(NOT_CONFIGURED)}


if __name__ == "__main__":
    json.dump(build(), sys.stdout)
