from collections import defaultdict

from .contract import (SCHEMA, DERIVER, DIMENSIONS, ContractError, digest, encode, identifier,
                       number, provenance, require, root_metadata, scoped, setting, stamp, REPO, SHA)
from .usage import normalize
from .operations import derive_operations


def run_record(row, sources, as_of):
    require(isinstance(row, dict), "run-shape")
    provider = row.get("provider_namespace")
    rid = scoped(provider, row.get("native_run_id"))
    require(identifier(row.get("attempt_id")) and identifier(row.get("native_session_id")), "run-native-id")
    require(row.get("status") in ("complete", "in_progress", "unknown"), "run-status")
    turns = row.get("turns")
    require(isinstance(turns, list) and len(turns) <= 500, "turns")
    ids, out = set(), []
    for turn in turns:
        require(isinstance(turn, dict) and identifier(turn.get("native_turn_id")), "turn-id")
        tid = turn["native_turn_id"]
        require(tid not in ids, "duplicate-turn")
        ids.add(tid)
        dims = {}
        for kind in ("requested", "reported"):
            require(isinstance(turn.get(kind), dict) and not set(turn[kind]) - set(DIMENSIONS), "settings")
            dims[kind] = {key: setting(turn[kind].get(key), sources, requested=kind == "requested") for key in DIMENSIONS}
        require(turn.get("usage_scope") in ("main_agent", "child", "unknown"), "usage-scope")
        require(turn.get("usage_status") in ("complete", "partial", "unavailable"), "usage-status")
        require(turn.get("has_subagents") in (True, False, None) and
                (turn.get("has_subagents") is None or type(turn["has_subagents"]) is bool), "has-subagents")
        out.append({"native_turn_id": tid, **dims, "usage_scope": turn["usage_scope"],
                    "usage_status": turn["usage_status"], "has_subagents": turn.get("has_subagents")})
    parent = row.get("parent")
    if parent is not None:
        require(isinstance(parent, dict) and identifier(parent.get("native_turn_id")), "parent")
        parent = {"run_id": scoped(parent.get("provider_namespace"), parent.get("native_run_id")),
                  "native_turn_id": parent["native_turn_id"], "evidence": provenance(parent.get("evidence"), sources)}
    children = row.get("expected_children")
    require(children is None or isinstance(children, list) and all(identifier(c) for c in children)
            and len(set(children)) == len(children), "expected-children")
    memberships = row.get("memberships")
    require(isinstance(memberships, list), "memberships")
    links = []
    for m in memberships:
        require(isinstance(m, dict) and isinstance(m.get("repository"), str) and REPO.fullmatch(m["repository"]), "repository")
        require(number(m.get("pr")) and m["pr"] > 0, "pr")
        require(m.get("head_sha") is None or isinstance(m["head_sha"], str) and SHA.fullmatch(m["head_sha"]), "head-sha")
        links.append({"repository": m["repository"], "pr": m["pr"], "head_sha": m.get("head_sha"),
                      "evidence": provenance(m.get("evidence"), sources)})
    links = sorted(links, key=lambda m: (m["repository"], m["pr"], m["head_sha"] or ""))
    allocation = "exclusive" if len({(m["repository"], m["pr"]) for m in links}) == 1 else "shared" if links else "unknown"
    for key in ("started_at", "ended_at"):
        require(row.get(key) is None or stamp(row[key]) and row[key] <= as_of, "run-time")
    require(row.get("started_at") is None or row.get("ended_at") is None or row["started_at"] <= row["ended_at"], "run-time-order")
    return {"id": rid, "started_at": row.get("started_at"), "ended_at": row.get("ended_at"), "provider_namespace": provider, "native_run_id": row["native_run_id"],
            "attempt_id": row["attempt_id"], "native_session_id": row["native_session_id"],
            "parent": parent, "status": row["status"], "turns": sorted(out, key=lambda t: t["native_turn_id"]),
            "expected_children": children, "memberships": links, "allocation": allocation,
            "evidence": provenance(row.get("evidence"), sources)}


def request_observation(row, sources, runs, setting_reader=setting):
    require(isinstance(row, dict), "request-shape")
    provider = row.get("provider_namespace")
    key = scoped(provider, row.get("native_request_id"))
    owner = scoped(provider, row.get("original_native_run_id"))
    turn = row.get("original_native_turn_id")
    require(owner in runs and turn in {t["native_turn_id"] for t in runs[owner]["turns"]}, "request-owner")
    require(number(row.get("revision")), "request-revision")
    ev = provenance(row.get("evidence"), sources)
    usage = row.get("usage")
    normalize(provider, usage)
    require(isinstance(row.get("reported", {}), dict), "request-reported")
    reported = {k: setting_reader((row.get("reported") or {}).get(k), sources) for k in DIMENSIONS}
    return {"id": key, "provider_namespace": provider, "native_request_id": row["native_request_id"],
            "original_run_id": owner, "original_native_turn_id": turn, "revision": row["revision"],
            "raw_usage": {k: v for k, v in usage.items() if k in (
                "input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens", "reasoning_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens", "thinking_tokens")},
            "reported": reported, "evidence": ev}


def canonical_requests(rows, sources, runs, errors, setting_reader=setting):
    grouped = defaultdict(list)
    for index, row in enumerate(rows):
        try:
            obs = (request_observation(row, sources, runs) if setting_reader is setting else
                   request_observation(row, sources, runs, setting_reader=setting_reader))
            grouped[obs["id"]].append(obs)
        except (ContractError, TypeError) as exc:
            errors.append({"collection": "requests", "index": index, "code": str(exc) if isinstance(exc, ContractError) else "request-shape"})
    result = []
    for key, obs in sorted(grouped.items()):
        by_source = defaultdict(list)
        for item in obs:
            by_source[item["evidence"]["source_id"]].append(item)
        finals = []
        for copies in by_source.values():
            top = max((o["revision"], o["evidence"]["at"], o["evidence"]["line"]) for o in copies)
            finals.extend(o for o in copies if (o["revision"], o["evidence"]["at"], o["evidence"]["line"]) == top)
        top = max((o["revision"], o["evidence"]["at"]) for o in finals)
        finals = sorted((o for o in finals if (o["revision"], o["evidence"]["at"]) == top),
                        key=lambda o: (o["evidence"]["source_id"], o["evidence"]["line"]))
        first = finals[0]
        owners = {(o["original_run_id"], o["original_native_turn_id"]) for o in obs}
        conflict = len(owners) != 1
        merged = {}
        for o in finals:
            facts = {**o["raw_usage"], **{"reported_" + k: v["value"] for k, v in o["reported"].items()}}
            for k, v in facts.items():
                if v is not None:
                    if merged.get(k) is not None and merged[k] != v:
                        conflict = True
                    merged[k] = v
        try:
            usage = normalize(first["provider_namespace"], merged)
        except ContractError:
            conflict = True
            usage = normalize(first["provider_namespace"], {})
        if conflict:
            usage = normalize(first["provider_namespace"], {})
        settings = {}
        for k in DIMENSIONS:
            candidates = [o["reported"][k] for o in finals if o["reported"][k]["value"] is not None]
            settings[k] = candidates[0] if candidates and not conflict else setting(None, sources)
        owner = first["original_run_id"] if len(owners) == 1 else None
        result.append({"id": key, "provider_namespace": first["provider_namespace"],
                       "native_request_id": first["native_request_id"], "original_run_id": owner,
                       "original_native_turn_id": first["original_native_turn_id"] if owner else None,
                       "revision": first["revision"], "copies": len(obs), "conflict": conflict,
                       "final_sources": [o["evidence"] for o in finals], "reported": settings,
                       "usage": usage, "allocation": runs[owner]["allocation"] if owner else "unknown"})
    return result


def derive_health(sources, runs, requests, errors, as_of, window, operations=()):
    reasons = set()
    if any(o["state"] == "ambiguous-owner" for o in operations):
        reasons.add("producer-ambiguous")
    if errors:
        reasons.add("invalid-records")
    if any(s["gaps"] for s in sources):
        reasons.add("source-gaps")
    if any(s["complete_through"] is None or s["complete_through"] < window["end"]
           or s["history_start"] is None or s["history_start"] > window["start"] for s in sources):
        reasons.add("coverage-unproven")
    by_run = {r["id"]: r for r in runs}
    by_owner = defaultdict(list)
    for q in requests:
        by_owner[q["original_run_id"]].append(q)
        if q["conflict"]:
            reasons.add("request-conflict")
    for r in runs:
        if not r["turns"]:
            reasons.add("usage-incomplete")
        if r["status"] != "complete":
            reasons.add("in-progress-or-unknown")
        if r["parent"]:
            parent = by_run.get(r["parent"]["run_id"])
            if parent is None or r["parent"]["native_turn_id"] not in {t["native_turn_id"] for t in parent["turns"]}:
                reasons.add("parent-unresolved")
        walk, visited = r, set()
        while walk["parent"]:
            if walk["id"] in visited:
                reasons.add("parent-unresolved")
                break
            visited.add(walk["id"])
            walk = by_run.get(walk["parent"]["run_id"])
            if walk is None:
                break
        children = r["expected_children"]
        actual = {c["id"] for c in runs if c["parent"] and c["parent"]["run_id"] == r["id"]}
        if children is None or set(children) != actual or any(c not in by_run or by_run[c]["status"] != "complete" for c in children):
            reasons.add("children-unreconciled")
        for t in r["turns"]:
            qs = [q for q in by_owner[r["id"]] if q["original_native_turn_id"] == t["native_turn_id"]]
            if not qs or any(not q["usage"]["complete"] or q["conflict"] for q in qs) or t["usage_status"] != "complete":
                reasons.add("usage-incomplete")
            if t["has_subagents"] is None or t["has_subagents"] and not children or t["has_subagents"] is False and actual:
                reasons.add("children-unreconciled")
    return {"state": "partial" if reasons else "complete", "reasons": sorted(reasons),
            "live_qualified": False, "in_progress": sorted(r["id"] for r in runs if r["status"] == "in_progress")}


def export_bundle(bundle):
    sources = root_metadata(bundle)
    errors, runs, duplicates = [], {}, set()
    for index, row in enumerate(bundle["runs"]):
        try:
            r = run_record(row, sources, bundle["as_of"])
            if r["id"] in runs or r["id"] in duplicates:
                runs.pop(r["id"], None)
                duplicates.add(r["id"])
                raise ContractError("duplicate-run")
            runs[r["id"]] = r
        except (ContractError, TypeError) as exc:
            errors.append({"collection": "runs", "index": index, "code": str(exc) if isinstance(exc, ContractError) else "run-shape"})
    requests = canonical_requests(bundle["requests"], sources, runs, errors)
    operations = derive_operations(bundle, sources, runs, errors)
    ordered = [r for _, r in sorted(runs.items())]
    result = {"schema": SCHEMA, "deriver_version": DERIVER, "origin": dict(bundle["origin"]),
            "generated_at": bundle["generated_at"], "as_of": bundle["as_of"], "window": dict(bundle["window"]),
            "source_identity": {"bundle_digest": digest(bundle), "host": None, "basis": "offline-fixture"},
            "sources": sorted(sources.values(), key=lambda s: s["id"]), "runs": ordered,
            "requests": requests, "operations": operations, "errors": errors,
            "health": derive_health(list(sources.values()), ordered, requests, errors, bundle["as_of"], bundle["window"], operations)}

    from .ingest import accept_envelope
    result = accept_envelope(result)
    require(len(encode(result)) <= 4 * 1024 * 1024, "output-size")
    return result
