import json
import os
import re

NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
PALETTE = ("#4E79A7", "#F28E2B", "#59A14F", "#B07AA1", "#76B7B2", "#EDC948", "#E15759", "#9C755F", "#FF9DA7", "#BAB0AC")


def path():
    return os.environ.get("FLIGHTOPS_CONFIG") or os.path.expanduser("~/.config/flightops/config.json")


def parse(raw):
    raw = raw if isinstance(raw, dict) else {}
    repos, seen = [], set()
    for entry in raw.get("repos") if isinstance(raw.get("repos"), list) else []:
        item = {"name": entry} if isinstance(entry, str) else entry if isinstance(entry, dict) else {}
        name = item.get("name")
        if not isinstance(name, str) or not NAME.match(name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        color = item.get("color") if isinstance(item.get("color"), str) and COLOR.match(item["color"]) else None
        repos.append({"name": name, "color": color})
    for index, repo in enumerate(repos):
        repo["color"] = repo["color"] or PALETTE[index % len(PALETTE)]
    check = raw.get("approval_check")
    machines = [m for m in raw.get("machines") or [] if isinstance(m, str) and m] if isinstance(raw.get("machines"), list) else []
    return {"repos": repos, "approval_check": check if isinstance(check, str) and check else None, "machines": machines}


def load(where=None):
    try:
        with open(where or path()) as f:
            return parse(json.load(f)), None
    except FileNotFoundError:
        return parse({}), "missing"
    except (OSError, ValueError):
        return parse({}), "unreadable"


def save(repos, where=None, keep=None):
    where = where or path()
    current = keep if keep is not None else load(where)[0]
    colors = {r["name"].lower(): r["color"] for r in current["repos"]}
    body = {"repos": [{"name": n, "color": colors[n.lower()]} if n.lower() in colors else n for n in repos]}
    if current["approval_check"]:
        body["approval_check"] = current["approval_check"]
    if current["machines"]:
        body["machines"] = current["machines"]
    os.makedirs(os.path.dirname(where), exist_ok=True)
    temp = where + ".tmp"
    with open(temp, "w") as f:
        json.dump(body, f, indent=2)
        f.write("\n")
    os.replace(temp, where)
    return parse(body)
