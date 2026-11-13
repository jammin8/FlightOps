# Current Orchestrator/Codex read-only adapter

This packet implements acquisition, rather than merely specifying it. It does not
activate a collector, replace a feed/cache, join PRs, compute cohorts or make Best /
Needs attention calls. The existing offline `agent-evidence/1` contract and golden
fixtures remain unchanged.

## Explicit acquisition entry point

`python3 -m collectors.model_evidence.acquire_cli --help` describes the new CLI.
Every source and destination must be supplied. For Root's later separately reviewed
bounded acquisition, the arguments are an absolute SQLite path, one/two configured
native session roots, one or more allowlisted project IDs, a frozen Unix `--as-of`,
explicit `--window-start` (at most 90 days earlier), and an explicit output file.
No command is scheduled or run against real native sources by importing the module.

The current authorized source configuration is the existing dev statev2 SQLite store
and Codex sessions/archived_sessions roots, when present. They are **not defaults**
in the code. Configured source homes must be supplied explicitly; no auth/config,
keychain, shell-snapshot or private home resolution is attempted. In this source
packet tests use temporary databases/files and Root-supplied private metadata
fixtures. A real bounded acquisition must follow independent full review; outputs
for this worker remain in its unique /tmp evidence directory.

`acquisition.acquire(scope, acquired_at=..., check=...)` returns a sanitized object
and writes nothing. The CLI calls the previously reviewed atomic writer only after
derivation and validation. Its destination cannot replace the database/sidecars or
reside under a native input root. There is no app refresh wiring or transport plugin.

## SQLite boundary and current joins

`projection.read_projection` opens only the explicit file URI with `mode=ro`, sets
`PRAGMA query_only=ON`, and uses BEGIN/read/ROLLBACK on one connection. A SQLite
authorizer denies writes, attach, pragmas after configuration, and reads from every
table except sqlite_master and these five metadata projections:

- orchestration_v2_projection_threads
- orchestration_v2_projection_runs
- orchestration_v2_projection_run_attempts
- orchestration_v2_projection_provider_threads
- orchestration_v2_projection_provider_turns

Fixed SQL expressions extract only needed technical metadata. Titles, messages,
human requests, queue tables/positions, current thread modelSelection and arbitrary
payload fields are not returned. Historical **run** modelSelection supplies requested
settings. Relational primary/project/thread/run/attempt/provider IDs must agree with
the extracted payload IDs before joining. A provider turn joins its run attempt,
which joins the run, provider thread, strong native session and strong native turn.
The run's provider thread, when it names one, must equal the attempt's, and the run,
attempt and provider thread must all declare the same provider instance. Otherwise the
attempt is refused with `run-provider-reference-mismatch`, `provider-instance-conflict`
or `provider-instance-unknown`, matching exact owner lookup; other attempts are unaffected.
No title/branch/time-only matching exists. Attempt IDs distinguish retries even when
native sessions resume. Conflicting native response ownership stays conflicted.

The allowlist contains exactly the current registry IDs verified by Root's canonical
project reads and matching relational metadata on 2026-10-30:

| Project | Exact ID |
| --- | --- |
| FlightOps | `mcp:5cffed67-debf-4e5f-ace3-1d3ca260d914` |
| scratch | `15422c94-4afe-4fff-b056-f3b240bc63ce` |
| backend | `93db29b0-fce2-415c-9c1b-48a687f8302f` |
| web | `175492b5-29c1-4884-9919-3b56a5d0f1c9` |
| iOS | `851362d8-b65b-4e0e-8781-53c2aadaa12e` |

Only FlightOps has a prefix. The other four IDs are bare UUIDs; their `mcp:`-prefixed
counterparts are rejected. The reader passes exact IDs to projection queries without
prefix normalization, alias acceptance or dynamic registry discovery.

Run selection uses the requested window including runs overlapping its start, not a
14-day seed. Selected threads, their allowlisted parent/child metadata, attempts,
provider turns and provider threads come from the same transaction. An explicit
row-limit gap invalidates inventory completeness. A missing/deleted historical run
cannot be proven absent just because a projection query returned successfully.

Source digest is over selected projected metadata, **not** a hash of a changing main
SQLite file that omits WAL state. No claim is made that an active SQLite file's bytes
remain unchanged while other actors use Orchestrator. Synthetic tests establish that this
reader cannot mutate the test DB, denies unrelated tables, and retains one snapshot
across a concurrent WAL update.

## Native file boundary

Discovery visits only the explicitly supplied roots' flat rollout files and the
calendar day directories in the requested window. It reads only matching relevant
native-session UUID filenames. It does not recursively traverse a provider home or
open irrelevant files. Explicit native-ID-to-path mappings can supply an older
resumed session that predates those discovery days; without such a mapping a missing
source is a gap, not a zero count or an invented path. Archived sources are searched
only when explicitly configured. Source path/device/format compatibility is not
inferred from environment variables.

Symlinks, paths outside a configured root, nonregular files and unsupported filenames
fail closed. Native opens use O_RDONLY/O_NOFOLLOW/O_NONBLOCK. Per-line decoding is
bounded before buffering and rejects duplicate JSON keys/nonfinite JSON. The reader
projects session_meta, task start/completion, turn_context, settings-applied and
response-ID token_usage_record metadata. Prompts, narrative, commands, argv, cwd,
outputs and unknown content are discarded before derivation/publication. It retains
source-prefix digest, device/inode, bytes read, physical line and original event time.
Before/after fstat detects changed snapshots; truncated/changed/limited reads carry
gaps and cannot establish complete history.

The supplied current format is Codex CLI 0.166.0. Other versions get an explicit
unqualified-version gap; no silent compatibility approval follows from coincident
field names. Unsupported providers, child formats, completion forms or paths remain
visible gaps. A relevant child's root_turn_id cannot be silently aggregated as a
main response, and missing native IDs are not replaced with timestamps.

## agent-evidence/2 and origin

The new machine schema is
[agent-evidence-v2.schema.json](../collectors/model_evidence/agent-evidence-v2.schema.json).
It preserves the v1 run/request usage shapes, adds acquisition inventory, exact
projection links, native snapshot bounds, setting observations and per-turn
reconciliation. Its origins are distinct:

| Origin | Meaning |
| --- | --- |
| readonly-acquisition | The explicit reader obtained projection and native snapshots. |
| metadata-replay | Supplied observed metadata, without a new source read. |
| synthetic-replay | Sanitized regression metadata with synthetic identifiers. |

No real acquisition is labeled offline-fixture. No origin, caller flag or successful
read can set `live_qualified`; it remains false. Root's later actual bounded acceptance
is a separate receipt, not a boolean supplied by the exporter. The host stays null.
Version 1 retains its strict offline-fixture origin and independent validator. The
last-good loader dispatches version 2 explicitly; malformed v2 publication is rejected
as a whole, preserving the old export/clocks. Valid source-record neighbors can still
survive derivation with gaps. Version 1's record-local ingestion behavior is unchanged.

Known settings carry orch-request, native-session, native-turn-context or
native-settings-applied basis and source/line/time. These are runtime configuration
observations, **not provider billing/processing attestations**. Requested settings
never fill reported fields. In particular instance remains unknown unless a native
interface actually reports it. Turn aggregates stay unknown for mixed/missing values;
per-response settings and the full observation timeline retain the switches.

A turn context applies to a response only if it is dated earlier or, at the same
timestamp, sits on an earlier line of the same source. Copies that disagree on
that order, or that cannot show it, leave the setting unknown with
`native-settings-order-unknown`. Observation time never substitutes for either.

A settings-applied observation can supply a session-effective setting only when it
precedes the exact native task start. When timestamps tie, precedence requires a
strictly earlier physical line in the same captured source. Copies must agree on
the exact projected task-start occurrence (native identity, typed payload and time);
source line numbers are never compared as a global order. Equal typed setting copies
with absent or conflicting local start/order witnesses leave their affected dimensions
unknown, with an ordering gap, rather than falling back to an older possibly superseded
value. Later independently turn-bound context evidence remains eligible per dimension.
Context records are explicitly keyed by native turn and apply only to that turn. A later next-turn setting never fills a previous
turn's delayed response. Root corrected an earlier fixture extraction: line605 at
22:28:34.586 precedes target task line606 at22:28:34.594; line755 at22:56 belongs to the
next interval. Both original and correction remain immutable private inputs; the
committed regression renames identifiers and contains only technical metadata.

Response IDs are canonical across file copies/resumes before totals. Native logs have
no explicit response revision counter in this interface: `revision` orders observed
event milliseconds, followed by physical source line, rather than inventing a provider
revision. Original session/turn/attempt identity precedes ordering; conflicting owners
cannot allocate a response twice. The shared canonicalizer receives a strict basis
reader for v2; its unchanged default preserves v1 behavior.

## Processed usage and completion gaps

Orchestrator provider_turn.tokenUsage is context occupancy/last-response detail. This adapter
never substitutes it for processed turnTokenUsage. It normalizes each native response
usage, canonicalizes copies/revisions/conflicts and compares all five sums against
complete main_agent turnTokenUsage. Missing optional cache writes remain unknown,
explicit zero stays zero, cache is inside Codex input and reasoning stays inside output.
Native task start/completion and matching turn IDs are required for a complete
reconciliation state. In-progress/post-as-of projection states cannot become completed
historical turns. hasSubagents is distinct from main-agent usageStatus; unknown or
positive child inventory and unbound parent/child turns remain gaps.

Root's actual completed sample has 15 unique response records: input4,375,823,
cache-read4,312,576, cache-write0, output28,370, reasoning13,749; fresh input63,247.
All five match the supplied processed main-agent turn totals. This is one current
interface compatibility fixture, not a claim about the whole fleet or history.
`compatible_turns` counts reconciled samples; it does not mean comparison readiness.

Native `event_msg/item_completed` CommandExecution records actually exist, including
explicit thread/turn/item/process IDs and unified_exec_startup source. This packet
recognizes a precise `unqualified-command-lifecycle` gap. Startup completed/exit0 does
not prove terminal process success or a Git operation. Custom code-mode calls also
carry unsupported-operation-binding gaps. `operations` must be empty in v2: no JS
regex extraction, final narrative, nearby SHA, time fallback or requested role becomes
a producer receipt. Qualification of actual lifecycle/argv/output/repo proof is later
Packet B work, not an assertion that all native command records are absent.

## Bounds, clocks, recovery and validation

Limits are intentionally small: five projects, 90-day maximum window, 500 rows per
projection collection, 4 MiB projected metadata, 32 native files, 2,000 directory
entries, 8 MiB/file, 32 MiB total, 100,000 physical lines/file, 1 MiB/line, 5,000 retained
metadata events/file, 10,000 metadata events total, 5,000 request observations and
4 MiB publication. The SQLite query progress deadline is five seconds; the CLI uses
a fatal five-second SIGALRM (configurable1..30). Limit/unknown cases are gaps or a
rejected source, never false complete empty sources. There are no increased API
budgets or API calls.

Source snapshot time is separate from acquired_at/generated_at. Native snapshot
clocks use observed technical events, not filesystem mtime or export time. Complete-
through remains null and retention gaps explicit: a finite projection/file inventory
cannot establish 90-day historical retention. A successful acquisition therefore does
not claim complete source history. Last-good ingestion retains original source clocks
after missing/invalid/interrupted input; reacquisition does not freshen old events.
Fatal cancellation is distinct from recoverable malformed metadata. Atomic publication
has the previously reviewed single-writer/pre-replace guarantees, not a concurrency
or directory-fsync power-loss guarantee.

Run focused tests with `python3 -m unittest discover -s tests -p 'test_model_evidence*.py' -v`.
Tests use temporary SQLite/native trees and sanitized fixtures. Private Root fixture
replay is recorded separately in the handoff, not as a new real-source acquisition.
Independent full review and then Root's one bounded real read remain outstanding.
