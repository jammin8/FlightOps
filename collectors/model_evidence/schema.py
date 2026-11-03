from .contract import ContractError, DIMENSIONS, ID, REPO, SHA, DIGEST, require, bounded_numeric


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def arr(items, maximum=5000):
    return {"type": "array", "items": items, "maxItems": maximum}


def enum(*values):
    return {"enum": list(values)}


def nullable(spec):
    return {"anyOf": [spec, {"type": "null"}]}


IDENT = {"type": "string", "pattern": ID.pattern.replace("\\Z", "$")}
REPOSITORY = {"type": "string", "pattern": REPO.pattern.replace("\\Z", "$")}
FULL_SHA = {"type": "string", "pattern": SHA.pattern.replace("\\Z", "$")}
HASH = {"type": "string", "pattern": DIGEST.pattern.replace("\\Z", "$")}
TIME = {"type": "number", "minimum": 0, "maximum": 2**53 - 1}
COUNT = {"type": "integer", "minimum": 0, "maximum": 2**53 - 1}
BOOL = {"type": "boolean"}
EVIDENCE = obj({"source_id": IDENT, "source_digest": HASH, "line": {**COUNT, "minimum": 1}, "at": TIME})
SETTING = obj({"value": nullable(IDENT), "basis": enum("unknown", "fixture-request", "fixture-report"),
               "evidence": nullable(EVIDENCE)})
SETTINGS = obj({d: SETTING for d in DIMENSIONS})
TURN = obj({"native_turn_id": IDENT, "requested": SETTINGS, "reported": SETTINGS,
            "usage_scope": enum("main_agent", "child", "unknown"),
            "usage_status": enum("complete", "partial", "unavailable"), "has_subagents": nullable(BOOL)})
RUN = obj({"id": IDENT, "started_at": nullable(TIME), "ended_at": nullable(TIME), "provider_namespace": enum("codex", "claude"), "native_run_id": IDENT,
           "attempt_id": IDENT, "native_session_id": IDENT,
           "parent": nullable(obj({"run_id": IDENT, "native_turn_id": IDENT, "evidence": EVIDENCE})),
           "status": enum("complete", "in_progress", "unknown"), "turns": arr(TURN, 500),
           "expected_children": nullable(arr(IDENT)), "memberships": arr(obj({"repository": REPOSITORY,
               "pr": {**COUNT, "minimum": 1}, "head_sha": nullable(FULL_SHA), "evidence": EVIDENCE})),
           "allocation": enum("exclusive", "shared", "unknown"), "evidence": EVIDENCE})
BUCKET_NAMES = ("fresh_input", "cache_read", "cache_write", "output", "reasoning")
USAGE = obj({"buckets": obj({k: nullable(COUNT) for k in BUCKET_NAMES}),
             "known": obj({k: BOOL for k in BUCKET_NAMES}), "input_total": nullable(COUNT),
             "output_total": nullable(COUNT), "complete": BOOL,
             "semantics": enum("cache-inside-input/reasoning-inside-output")})
REQUEST = obj({"id": IDENT, "provider_namespace": enum("codex", "claude"), "native_request_id": IDENT,
               "original_run_id": nullable(IDENT), "original_native_turn_id": nullable(IDENT), "revision": COUNT,
               "copies": {**COUNT, "minimum": 1}, "conflict": BOOL, "final_sources": arr(EVIDENCE),
               "reported": SETTINGS, "usage": USAGE, "allocation": enum("exclusive", "shared", "unknown")})
OPERATION = obj({"call_id": IDENT, "completion_id": IDENT, "native_session_id": IDENT,
                 "owning_run_id": IDENT, "owning_native_turn_id": IDENT, "root_run_id": IDENT,
                 "repository": REPOSITORY, "worktree": {"type": "string", "pattern": "^/[A-Za-z0-9_./:@-]+$", "maxLength": 500},
                 "call_digest": HASH, "completion_digest": HASH, "evidence": EVIDENCE,
                 "completion_evidence": EVIDENCE, "repository_evidence": EVIDENCE,
                 "basis": enum("fixture-receipt"), "state": enum("proven-in-fixture", "ambiguous-owner"),
                 "operation": enum("commit", "merge", "push"), "sha": FULL_SHA,
                 "destination_ref": nullable(IDENT), "remote": nullable(IDENT),
                 "proof": enum("fixture-pre-post-creation", "log-head-printed", "log-commit-line", "log-push-range"),
                 "observation_evidence": nullable(EVIDENCE)})
ERROR = obj({"collection": enum("runs", "requests", "calls", "completions", "observations", "operations"),
             "index": COUNT, "code": IDENT})
SOURCE = obj({"id": IDENT, "digest": HASH, "snapshot_at": TIME, "history_start": nullable(TIME),
              "history_end": nullable(TIME), "complete_through": nullable(TIME), "gaps": arr(IDENT)})
SCHEMA = obj({"schema": enum("agent-evidence/1"), "deriver_version": enum("flightops-model-evidence/1"),
              "origin": obj({"kind": enum("offline-fixture"), "fixture_id": IDENT}),
              "generated_at": TIME, "as_of": TIME, "window": obj({"start": TIME, "end": TIME}),
              "source_identity": obj({"bundle_digest": HASH, "host": {"type": "null"}, "basis": enum("offline-fixture")}),
              "sources": arr(SOURCE, 32), "runs": arr(RUN), "requests": arr(REQUEST),
              "operations": arr(OPERATION), "errors": arr(ERROR),
              "health": obj({"state": enum("complete", "partial"), "reasons": arr(IDENT),
                             "live_qualified": enum(False), "in_progress": arr(IDENT)})})
JSON_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:flightops:agent-evidence:1", **SCHEMA}


def validate(value, spec):
    import re
    if "anyOf" in spec:
        for branch in spec["anyOf"]:
            try:
                validate(value, branch)
                return
            except ContractError:
                pass
        raise ContractError("schema-nullable")
    if "enum" in spec:
        require(any(type(value) is type(v) and value == v for v in spec["enum"]), "schema-enum")
        return
    kind = spec["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if kind == "number":
        require(bounded_numeric(value, spec["minimum"], spec["maximum"]), "schema-number")
    else:
        require(type(value) is types[kind], "schema-type")
    if kind == "object":
        require(set(value) == set(spec["properties"]), "schema-properties")
        for k, child in spec["properties"].items():
            validate(value[k], child)
    elif kind == "array":
        require(len(value) <= spec["maxItems"], "schema-array-limit")
        for v in value:
            validate(v, spec["items"])
    elif kind == "string":
        require(len(value) <= spec.get("maxLength", 160), "schema-string-limit")
        if "pattern" in spec:
            require(re.fullmatch(spec["pattern"].removesuffix("$"), value) is not None, "schema-pattern")
    elif kind in ("number", "integer"):
        require(spec["minimum"] <= value <= spec["maximum"], "schema-number-range")
