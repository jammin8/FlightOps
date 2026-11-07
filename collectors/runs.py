import os
import tempfile
from pathlib import Path
from typing import Protocol

from collectors.model_evidence.contract import (ContractError, MAX_BYTES, decode, encode, require, stamp)
from collectors.model_evidence.ingest import accept_envelope

CACHE_SCHEMA = "agent-evidence-cache/1"
CLOCK_SKEW = 5


class EvidenceTransport(Protocol):
    def read(self, *, max_bytes: int, timeout_s: float) -> bytes: ...


def read_file(path):
    with Path(path).open("rb") as f:
        raw = f.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "input-size")
    return raw


def empty_cache():
    return {"schema": CACHE_SCHEMA, "received_at": None, "last_attempt_at": None,
            "failed_at": None, "last_error": None, "export": None}


def load(path):
    try:
        c = decode(read_file(path))
        require(isinstance(c, dict) and set(c) == set(empty_cache()) and c.get("schema") == CACHE_SCHEMA, "cache-schema")
        for key in ("received_at", "last_attempt_at", "failed_at"):
            require(c[key] is None or stamp(c[key]), "cache-time")
        require(c["last_error"] is None or c["last_error"] in ("source-unavailable", "source-invalid", "source-regressed", "publication-failed"), "cache-error")
        if c["export"] is not None:
            c["export"] = accept_envelope(c["export"])
            require(c["received_at"] is not None, "cache-received")
        return c
    except (OSError, ValueError, TypeError, RecursionError):
        return empty_cache()


def ingest(previous, raw, *, now):
    require(stamp(now), "ingest-time")
    previous = previous or empty_cache()
    require(previous.get("schema") == CACHE_SCHEMA, "cache-schema")
    if previous.get("export") is not None:
        previous = {**previous, "export": accept_envelope(previous["export"])}
    result = {**previous, "last_attempt_at": now}
    try:
        value = accept_envelope(decode(raw))
        require(value["generated_at"] <= now, "future-publication")
        old = previous.get("export")
        if old and (value["as_of"] < old["as_of"] or value["generated_at"] < old["generated_at"]):
            result.update(failed_at=now, last_error="source-regressed")
            return result
        result.update(export=value, received_at=now, failed_at=None, last_error=None)
    except (ContractError, TypeError, RecursionError):
        result.update(failed_at=now, last_error="source-unavailable" if raw is None else "source-invalid")
    return result


def atomic_write(path, cache):
    path = Path(path)
    raw = encode(cache)
    require(len(raw) <= MAX_BYTES, "publication-size")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def refresh(path, *, now, source_path=None, transport=None, timeout_s=5.0):
    require((source_path is None) != (transport is None), "choose-one-input")
    require(type(timeout_s) in (int, float) and 0 < timeout_s <= 30, "transport-timeout")
    old = load(path)
    try:
        raw = read_file(source_path) if source_path is not None else transport.read(max_bytes=MAX_BYTES, timeout_s=timeout_s)
        result = ingest(old, raw, now=now)
    except (OSError, TimeoutError):
        result = ingest(old, None, now=now)
    except ContractError:
        result = ingest(old, b"", now=now)
    try:
        atomic_write(path, result)
    except (OSError, ContractError):
        return {**old, "last_attempt_at": now, "failed_at": now, "last_error": "publication-failed"}
    return result


def status(cache, *, now, stale_after_s=900):
    require(stamp(now) and stamp(stale_after_s), "status-time")
    e = cache.get("export")
    source_at = min((s["snapshot_at"] for s in e["sources"]), default=e["as_of"]) if e else None
    source_at = min(source_at, e["as_of"]) if e else None
    future = bool(e) and max(source_at, e["as_of"], e["generated_at"]) > now + CLOCK_SKEW
    age = max(0, now - source_at) if source_at is not None and not future else None
    error = cache.get("last_error") or ("future-source" if future else None)
    return {"state": "error" if error else e["health"]["state"] if e else "unavailable",
            "partial": e["health"]["state"] == "partial" if e else True,
            "stale": age is None or age > stale_after_s, "source_at": source_at, "source_age_s": age,
            "received_at": cache.get("received_at"), "failed_at": cache.get("failed_at"),
            "error": error, "live_qualified": False}
