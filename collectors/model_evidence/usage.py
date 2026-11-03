from .contract import ContractError, number, require, identifier, stamp

BUCKETS = ("fresh_input", "cache_read", "cache_write", "output", "reasoning")


def normalize(provider, raw):
    require(isinstance(raw, dict), "usage")
    fields = (("input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens", "reasoning_tokens")
              if provider == "codex" else
              ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens", "thinking_tokens"))
    require(provider in ("codex", "claude"), "unsupported-provider")
    values = [raw.get(k) for k in fields]
    require(all(v is None or number(v) for v in values), "usage-number")
    inp, read, write, out, reason = values
    require(out is None or reason is None or reason <= out, "reasoning-overflow")
    if provider == "codex":
        require(inp is None or (read or 0) + (write or 0) <= inp, "cache-overflow")
        fresh = inp - read - write if all(v is not None for v in (inp, read, write)) else None
        total = inp
    else:
        fresh = inp
        total = inp + read + write if all(v is not None for v in (inp, read, write)) else None
        require(total is None or number(total), "usage-overflow")
    buckets = dict(zip(BUCKETS, (fresh, read, write, out, reason)))
    return {"buckets": buckets, "known": {k: v is not None for k, v in buckets.items()},
            "input_total": total, "output_total": out,
            "complete": all(v is not None for v in buckets.values()),
            "semantics": "cache-inside-input/reasoning-inside-output"}


def codex_turn_updates(events):
    require(isinstance(events, list) and len(events) <= 5000, "turn-event-limit")
    fields = ("input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens", "reasoning_tokens")
    baseline, starts, totals, last_at, active = None, {}, {}, -1, None
    for event in events:
        require(isinstance(event, dict) and identifier(event.get("turn_id")) and stamp(event.get("at")), "turn-event")
        require(event["at"] >= last_at, "turn-event-order")
        last_at = event["at"]
        turn = event["turn_id"]
        latest, cumulative = event.get("latest"), event.get("cumulative")
        normalize("codex", latest)
        if cumulative is not None:
            normalize("codex", cumulative)
        if turn not in starts:
            starts[turn] = baseline
            active = turn
        elif turn != active:
            starts[active] = None
        start = starts[turn]
        can_delta = cumulative is not None and start is not None and all(
            cumulative.get(k) is not None and start.get(k) is not None and cumulative[k] >= start[k]
            for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
        if can_delta:
            raw = {k: cumulative[k] - start[k] if cumulative.get(k) is not None and start.get(k) is not None
                   and cumulative[k] >= start[k] else None for k in fields}
        else:
            raw = dict(latest)
        totals[turn] = {**normalize("codex", raw),
                        "derivation_scope": "turn-delta" if can_delta else "latest-response",
                        "turn_coverage_complete": False}
        if cumulative is not None:
            baseline = dict(cumulative)
    return totals
