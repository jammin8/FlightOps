import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "config.example.json")
DEFAULTS = {
    "github_owner": "example-org",
    "repos": {"backend": "example-backend", "web": "example-web", "ios": "example-ios"},
    "linear_workspace": "example",
    "linear_keychain_service": "flightops-linear",
    "linear_initiative": "Launch",
    "linear_team_key": "ENG",
    "ssh_target": "flightops-vm",
    "published_status": ".local/state/flightops/flightops-status.json",
    "orchestrator_url": "http://127.0.0.1:3773",
    "orchestrator_protocol_header": "x-orchestration-protocol",
}


def under_tests():
    return "unittest" in sys.modules or os.path.basename(sys.argv[0] if sys.argv else "") in ("pytest", "py.test")


def paths():
    named = os.environ.get("FLIGHTOPS_CONFIG")
    if named:
        return [named]
    if under_tests():
        # Tests never read a developer's own config; collectors they start inherit the fixture.
        os.environ["FLIGHTOPS_CONFIG"] = os.path.join(ROOT, "tests", "fixtures", "site-config.json")
        return [os.environ["FLIGHTOPS_CONFIG"]]
    return [os.path.join(ROOT, "flightops.local.json"), os.path.expanduser("~/.config/flightops/config.json")]


def load(candidates=None):
    config = dict(DEFAULTS)
    for path in candidates if candidates is not None else paths():
        try:
            with open(path) as f:
                local = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(local, dict):
            config.update({k: v for k, v in local.items() if k in DEFAULTS and isinstance(v, type(DEFAULTS[k]))})
        break
    return config


CONFIG = load()
OWNER = CONFIG["github_owner"]
REPOS = dict(CONFIG["repos"])
