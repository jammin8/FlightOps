import hashlib
import json
import math
import re

SCHEMA = "agent-evidence/1"
BUNDLE_SCHEMA = "model-evidence-fixture/1"
DERIVER = "flightops-model-evidence/1"
MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 5000
MAX_TEXT = 65536
PROVIDERS = {"codex", "claude"}
DIMENSIONS = ("provider", "instance", "model_id", "effort", "service_tier")
BASES = {"fixture-request", "fixture-report", "fixture-receipt", "fixture-inventory"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@%/-]{0,159}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ContractError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ContractError(code)


def identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def number(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def bounded_numeric(value, minimum, maximum):
    return (type(value) in (int, float) and minimum <= value <= maximum
            and (type(value) is int or math.isfinite(value)))


def stamp(value):
    return bounded_numeric(value, 0, 2**53 - 1)


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def decode(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES, "input-size")
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, "duplicate-json-key")
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError("nonfinite-json")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError("invalid-json") from exc


def exact_keys(obj, keys, code):
    require(isinstance(obj, dict) and set(obj) == set(keys), code)


def provenance(row, sources):
    require(isinstance(row, dict), "provenance")
    source = row.get("source_id")
    require(identifier(source) and source in sources, "provenance-source")
    require(number(row.get("line")) and row["line"] > 0, "provenance-line")
    require(stamp(row.get("at")) and row["at"] <= sources[source]["snapshot_at"], "provenance-time")
    return {"source_id": source, "source_digest": sources[source]["digest"],
            "line": row["line"], "at": row["at"]}


def setting(value, sources, requested=False):
    if value is None:
        return {"value": None, "basis": "unknown", "evidence": None}
    require(isinstance(value, dict) and identifier(value.get("value")), "setting")
    require(value.get("basis") == ("fixture-request" if requested else "fixture-report"), "setting-basis")
    return {"value": value["value"], "basis": value["basis"],
            "evidence": provenance(value.get("evidence"), sources)}


def scoped(provider, native):
    require(isinstance(provider, str) and provider in PROVIDERS and identifier(native), "identity")
    result = provider + ":" + native
    require(identifier(result), "identity-length")
    return result


def root_metadata(bundle):
    require(isinstance(bundle, dict) and bundle.get("schema") == BUNDLE_SCHEMA, "bundle-schema")
    require(stamp(bundle.get("generated_at")) and stamp(bundle.get("as_of")), "bundle-time")
    require(bundle["as_of"] <= bundle["generated_at"], "bundle-time-order")
    origin = bundle.get("origin")
    exact_keys(origin, ("kind", "fixture_id"), "fixture-origin")
    require(origin["kind"] == "offline-fixture" and identifier(origin["fixture_id"]), "fixture-origin")
    require(isinstance(bundle.get("sources"), list) and 0 < len(bundle["sources"]) <= 32, "sources")
    sources = {}
    for s in bundle["sources"]:
        exact_keys(s, ("id", "digest", "snapshot_at", "history_start", "history_end", "complete_through", "gaps"), "source-shape")
        require(identifier(s["id"]) and s["id"] not in sources, "source-id")
        require(isinstance(s["digest"], str) and DIGEST.fullmatch(s["digest"]), "source-digest")
        require(stamp(s["snapshot_at"]) and s["snapshot_at"] <= bundle["as_of"], "source-time")
        for k in ("history_start", "history_end", "complete_through"):
            require(s[k] is None or stamp(s[k]) and s[k] <= s["snapshot_at"], "source-bound")
        require(s["history_start"] is None or s["history_end"] is None or s["history_start"] <= s["history_end"], "source-bound-order")
        require(s["complete_through"] is None or s["history_end"] is not None and s["complete_through"] <= s["history_end"], "source-watermark")
        require(isinstance(s["gaps"], list) and all(identifier(g) for g in s["gaps"]), "source-gaps")
        sources[s["id"]] = s
    window = bundle.get("window")
    exact_keys(window, ("start", "end"), "window")
    require(stamp(window["start"]) and stamp(window["end"]) and window["start"] <= window["end"] <= bundle["as_of"], "window")
    for key in ("runs", "requests", "calls", "completions", "observations"):
        require(isinstance(bundle.get(key), list), "records")
    require(sum(len(bundle[k]) for k in ("runs", "requests", "calls", "completions", "observations")) <= MAX_RECORDS, "record-limit")
    require(len(encode(bundle)) <= MAX_BYTES, "input-size")
    require(isinstance(bundle.get("repositories", {}), dict), "repositories")
    return sources
