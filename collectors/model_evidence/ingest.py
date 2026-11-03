from copy import deepcopy
from collections import defaultdict
import posixpath

from .contract import ContractError, MAX_BYTES, encode, require, root_metadata
from .exporter import derive_health
from .schema import SCHEMA, validate


def accept_envelope(value):
    if isinstance(value, dict) and value.get("schema") == "agent-evidence/2":
        from .observed_contract import accept_observed
        return accept_observed(value)
    require(isinstance(value, dict) and len(encode(value)) <= MAX_BYTES, "envelope-size")
    stub = deepcopy(value)
    for key in ("runs", "requests", "operations"):
        require(isinstance(value.get(key), list) and len(value[key]) <= 5000, "envelope-record-limit")
        stub[key] = []
    validate(stub, SCHEMA)
    root_metadata({"schema": "model-evidence-fixture/1", "origin": value["origin"],
                   "generated_at": value["generated_at"], "as_of": value["as_of"], "sources": value["sources"],
                   "window": value["window"], "runs": [], "requests": [], "calls": [], "completions": [], "observations": []})
    out = deepcopy(value)
    sources = {s["id"]: s for s in out["sources"]}

    def evidence(node):
        if isinstance(node, dict):
            if set(node) == {"source_id", "source_digest", "line", "at"}:
                require(node["source_id"] in sources and node["source_digest"] == sources[node["source_id"]]["digest"], "evidence-source")
                require(node["at"] <= sources[node["source_id"]]["snapshot_at"], "evidence-time")
            for v in node.values():
                evidence(v)
        elif isinstance(node, list):
            for v in node:
                evidence(v)

    def settings(node):
        if isinstance(node, dict):
            if set(node) == {"value", "basis", "evidence"}:
                require((node["value"] is None) == (node["basis"] == "unknown") == (node["evidence"] is None), "setting-knownness")
            for v in node.values():
                settings(v)
        elif isinstance(node, list):
            for v in node:
                settings(v)

    def dimension_basis(group, basis):
        require(all(v["value"] is None or v["basis"] == basis for v in group.values()), "setting-basis")

    runs = {}
    for key in ("runs", "requests", "operations"):
        valid, seen, rejected = [], set(), set()
        for index, record in enumerate(out[key]):
            rid = None
            try:
                validate(record, SCHEMA["properties"][key]["items"])
                evidence(record)
                settings(record)
                rid = record.get("id") if key != "operations" else (record["call_id"], record["native_session_id"], record["operation"], record["sha"], record["destination_ref"])
                require(rid not in seen, "duplicate-record")
                seen.add(rid)
                if key == "runs":
                    require(record["id"] == record["provider_namespace"] + ":" + record["native_run_id"], "run-id")
                    require(record["started_at"] is None or record["ended_at"] is None or record["started_at"] <= record["ended_at"], "run-time-order")
                    require(all(record[k] is None or record[k] <= out["as_of"] for k in ("started_at", "ended_at")), "run-time")
                    for t in record["turns"]:
                        dimension_basis(t["requested"], "fixture-request")
                        dimension_basis(t["reported"], "fixture-report")
                    tids = [t["native_turn_id"] for t in record["turns"]]
                    require(len(set(tids)) == len(tids), "duplicate-turn")
                    allocation = "exclusive" if len({(m["repository"], m["pr"]) for m in record["memberships"]}) == 1 else "shared" if record["memberships"] else "unknown"
                    require(record["allocation"] == allocation, "allocation")
                if key == "requests":
                    require(record["id"] == record["provider_namespace"] + ":" + record["native_request_id"], "request-id")
                    dimension_basis(record["reported"], "fixture-report")
                    require(bool(record["final_sources"]), "request-final-source")
                    owner = record["original_run_id"]
                    require(owner is None and record["conflict"] or owner in runs and record["original_native_turn_id"] in {t["native_turn_id"] for t in runs[owner]["turns"]}, "request-owner")
                    require(owner is not None or record["original_native_turn_id"] is None, "request-owner")
                    require(owner is None or record["provider_namespace"] == runs[owner]["provider_namespace"], "request-provider")
                    require(record["allocation"] == (runs[owner]["allocation"] if owner else "unknown"), "allocation")
                    u = record["usage"]
                    require(u["known"] == {k: v is not None for k, v in u["buckets"].items()} and
                            u["complete"] == all(u["known"].values()), "usage-knownness")
                    b = u["buckets"]
                    require(b["output"] == u["output_total"], "usage-total")
                    require(b["reasoning"] is None or b["output"] is None or b["reasoning"] <= b["output"], "reasoning-overflow")
                    if all(b[k] is not None for k in ("fresh_input", "cache_read", "cache_write")):
                        require(sum(b[k] for k in ("fresh_input", "cache_read", "cache_write")) == u["input_total"], "usage-total")
                    require(not record["conflict"] or not any(u["known"].values()), "conflict-usage")
                if key == "operations":
                    owner = record["owning_run_id"]
                    require(owner in runs and record["root_run_id"] in runs and
                            record["owning_native_turn_id"] in {t["native_turn_id"] for t in runs[owner]["turns"]} and
                            record["native_session_id"] == runs[owner]["native_session_id"], "operation-owner")
                    require(record["call_id"].startswith(runs[owner]["provider_namespace"] + ":"), "operation-provider")
                    require(posixpath.normpath(record["worktree"]) == record["worktree"], "operation-worktree")
                    root, visited = owner, set()
                    while runs[root]["parent"]:
                        require(root not in visited, "parent-cycle")
                        visited.add(root)
                        parent = runs[root]["parent"]
                        require(parent["run_id"] in runs and parent["native_turn_id"] in {t["native_turn_id"] for t in runs[parent["run_id"]]["turns"]}, "parent-unresolved")
                        root = parent["run_id"]
                    require(record["root_run_id"] == root, "operation-root")
                    require(record["completion_evidence"]["at"] >= record["evidence"]["at"], "operation-time")
                    require((record["operation"] == "push") == (record["destination_ref"] is not None), "operation-destination")
                valid.append(record)
            except ContractError as exc:
                if rid in seen:
                    rejected.add(rid)
                out["errors"].append({"collection": key, "index": index, "code": str(exc)})
        out[key] = [r for r in valid if (r.get("id") if key != "operations" else (r["call_id"], r["native_session_id"], r["operation"], r["sha"], r["destination_ref"])) not in rejected]
        if key == "runs":
            runs = {r["id"]: r for r in out[key]}
    producers = defaultdict(set)
    for operation in out["operations"]:
        if operation["operation"] in ("commit", "merge"):
            producers[(operation["repository"], operation["sha"])].add(operation["owning_run_id"])
    for operation in out["operations"]:
        if operation["operation"] in ("commit", "merge") and len(producers[(operation["repository"], operation["sha"])]) > 1:
            operation["state"] = "ambiguous-owner"
    out["health"] = derive_health(out["sources"], out["runs"], out["requests"], out["errors"], out["as_of"], out["window"], out["operations"])
    validate(out, SCHEMA)
    return out
