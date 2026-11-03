# Offline model evidence — Packet A

FlightOps owns the `collectors/model_evidence` package and `collectors/runs.py`.
These modules are not connected to the app, a scheduled collector, SSH, Orch,
a database, logs, a hook, a controller, an observer key, or a user cache. Packet A
supplies a reproducible offline evidence core and ingestion contract. It does not
supply PR joins, CI/assignment/anchor derivation, cohorts, statistics, Best/Needs
attention calls, installation, or live-source qualification.

## Run and inspect

From the repository root with the existing Rig Python (stdlib only):

```sh
python3 -m collectors.model_evidence \
  tests/fixtures/model-evidence-v1/complete.bundle.json /tmp/agent-evidence.json
python3 -m unittest discover -s tests -p 'test_model_evidence*.py' -v
```

The CLI requires explicit input/output paths, does not create parent directories,
and uses a five-second POSIX processing deadline (configurable `--timeout 1..30`).
An error exits 2 with a fixed message. Deadline cancellation is a dedicated fatal
exception caught only at the CLI transaction boundary; record recovery cannot turn
an alarm into a successful partial publication. The pure `export_bundle(bundle)` function
has no implicit clock, filesystem, environment, database, network or credential
inputs. `encode` supplies canonical, sorted JSON; identical fixtures produce
identical bytes. No runtime timing or performance claim follows from tests.

## Versioned publication

[agent-evidence.schema.json](../collectors/model_evidence/agent-evidence.schema.json)
is the machine-readable `agent-evidence/1` contract. `schema.py` defines that same
contract and its stdlib validator. Additive fields require a new contract revision:
the consumer rejects unknown publication fields. Input fixture records are projected
through explicit allowlists; prompt/command/output bodies and arbitrary extra fields
are never published. Digests are content identity, not authentication or proof that
a declared source is truthful. Do not add raw-content retrieval to a consumer.

Every envelope carries:

- `schema`, `deriver_version`, explicit `generated_at`, frozen `as_of`, and requested
  `window.start/end`. Times are finite Unix seconds; filesystem mtime is unused.
- `origin.kind = offline-fixture` and a required fixture ID. `source_identity.host`
  is null; its basis is `offline-fixture` and bundle digest is computed from explicit
  input. There is no trusted-environment boolean or live-origin switch.
- Each source's explicit digest, snapshot time, observed history bounds, nullable
  complete-through watermark, and gap codes. Unknown bounds remain null. Evidence
  references include the source ID/digest, line, and original event time.
- Runs with provider-scoped IDs, separate native run/session and attempt IDs,
  parent run/turn evidence, nullable start/end times, status, expected-child
  inventory, membership evidence, and per-turn settings. The provider namespace
  (`codex` or `claude`) selects an identity/usage adapter; it does **not** prove the
  reported/executed provider setting. A duplicate run ID is rejected, not counted
  as another attempt. Missing native IDs are not replaced with time/branch IDs.
- Requests canonicalized globally within the complete supplied bundle, with native
  request ID, original owning run/turn, final revision and source references, copy
  count, conflict, usage and allocation. Cross-publication replay requires the
  future adapter to supply the full bounded inventory; this package is not a
  persistent cross-export ledger.
- Operation receipts with owning run/turn, root parent run, native call/session,
  final completion ID/digest, repository/worktree, full SHA, actual push destination
  ref, typed proof and source provenance. `proven-in-fixture` describes only the
  fixture proof; it is never a verified live producer or PR attribution.
- Sanitized error codes and derived health. `complete` means the supplied fixture
  inventory/usage reconciles, not that settings are known, a PR is complete,
  production evidence exists fleet-wide, or any comparison gate is satisfied.
  `health.live_qualified` is always false.

IDs are bounded literal identifiers; provider-native percent-encoded run IDs are
preserved verbatim. No URL decoding, abbreviation guessing, branch-role inference,
or synthetic producer/current-head inference occurs. SHA support is exactly 40
lowercase hexadecimal characters; other object formats stay unsupported. Every
repository is an explicit `owner/repository` label with source evidence. There is
no filesystem or remote-name resolution of a repository.

## Fixture input, settings and canonical usage

`model-evidence-fixture/1` is a technical fixture format, **not** an assertion that
Orch or a native provider emits these field names. The committed bundles are the
input examples; the paired goldens are immutable expected publications. The separate
`current-interface-shape.json` is a sanitized shape regression derived from a
Root-supplied guarded metadata observation, not a current native-log sample.

A bundle explicitly supplies `sources`, `window`, `runs`, `requests`, `calls`,
`completions`, `observations`, and repository object inventories. An evidence
reference is `{source_id, line, at}`; the public reference adds the declared source
digest. `reported` and `requested` each have five dimensions: provider, instance,
model_id, effort, service_tier. Each known setting has a value, a distinct
`fixture-request` or `fixture-report` basis, and evidence. An absent field becomes
`{value:null,basis:"unknown",evidence:null}`. Requested model/effort/tier, current
thread config, a namespace or a service-tier default cannot fill reported fields.
Switches remain separate turns; request-reported settings are separately retained.

Requests require an explicit original owner, even when observed in a later copy
or resumed span. The adapter must supply stable request IDs and explicit revision
ordering. Within each source, select its final `(revision, at, line)`; across
sources select the highest `(revision, at)`. Only equally authoritative final
copies refine each other's missing fields. Superseded revisions cannot restore a
removed input/model field or leave an obsolete usage conflict. Disagreeing final
values or original owners invalidate usage and retain a conflict; later observation
order cannot silently reallocate a request. Copies are counted but usage is emitted
once. No allocation weights or disjoint splitting are invented: one distinct PR
membership is exclusive, more than one is shared, and none is unknown. The future
allocation adapter may introduce a separately versioned, evidenced disjoint policy.

Native fixture usage keys:

| Namespace | Input / read / write | Output / reasoning |
| --- | --- | --- |
| codex | input_tokens / cached_input_tokens / cache_creation_tokens | output_tokens / reasoning_tokens |
| claude | input_tokens / cache_read_input_tokens / cache_creation_input_tokens | output_tokens / thinking_tokens |

Codex input includes cache reads/writes; Claude input is fresh input. Both export
fresh_input, cache_read, cache_write, output, reasoning, per-bucket knownness and
containing input/output totals. Reasoning is a subset of output, never added twice.
An absent optional Codex cache-write count is unknown, not zero; fresh input is
unknown until it can be subtracted safely. Explicit zero remains known. Negative,
boolean, unsafe integer, cache-component overflow and reasoning overflow values are
rejected rather than clamped. All five buckets must be known for this contract's
conservative usage completeness. There is no pricing or USD estimate.

`usage.codex_turn_updates` is a pure **fixture** cumulative-baseline control, not a
native Orch log decoder. It keys updates by their actual turn; a late finished turn
cannot be charged to a newer one. Missing/reset baselines use latest-response scope.
When a late old update makes the newer baseline uncertain, it is invalidated and
subsequent output remains explicitly latest-response scoped, never a made-up whole
turn total. `turn_coverage_complete` is false. Native request IDs are never invented
from cumulative turn totals; this helper does not feed synthetic totals into the
canonical request ledger.

Complete main-agent usage does not complete children. Expected children must be an
explicit list, including an explicit empty list when proved; null is an unknown
inventory. Parent links, has_subagents, child status, requests on every expected
turn, source watermarks and all errors affect health independently of usageStatus.

## Call/completion safety and retained code

The selected lexical shell grammar comes from an earlier internal shell helper. Hook imports, hook JSON, physical filesystem resolution, acquisition,
output exit-status scraping and PR URL parsing were removed. Paths are lexical;
physical resolution is unavailable. The exported operation layer further narrows
creation options and rejects unknown/abbreviated options, implicit dry-run formats,
and log-only amend. Unknown long push options are rejected. No old installer,
pricing code, collector integration or old patch series was ported.

Only explicit typed fixture call objects are accepted. Code-mode JavaScript and
native harness JSON are not decoded here: a future adapter must prove that its
single literal command and completion belong together. Final completion requires
explicit exit code 0 and matching provider namespace, call ID, native session and
process session (when present). Conflicting/missing final completions fail closed.
Nonfinal poll output cannot establish success. No command or output body survives
export; each has a digest instead.

Production requires a narrowly parsed create-capable command plus its own unambiguous
commit output, an immediate bound HEAD print, or explicit fixture pre/post change
with parent proof. Exit 0 and an arbitrary HEAD observation are insufficient.
Abbreviations are resolved against **all supplied objects for that repository**;
ambiguous or unknown objects cannot become receipts. Push evidence uses the actual
reported ref and object; a successful push to another ref cannot become the PR's
publication receipt. Dry-run, rejected and new-branch-without-object pushes do not
bind. No-op/fast-forward merges do not claim production. Two owners for the same
repository/object are ambiguous. One call's failed proof cannot leave an earlier
partial receipt from that call behind.

Optional observations are explicit fixture objects with their own source basis,
not authenticated live observer records or an instruction to install a hook. Their
presence cannot override unsupported shell structure, unchanged HEAD, wrong repo,
wrong completion, or missing parent proof. Call ownership is resolved before parsing,
from explicit run/turn and parent identities. Every child call retains its owner,
including observer-only/tab-separated calls after the parent ends; time never
reattributes an identified or unowned child to a newer run. No PR anchors are emitted
in Packet A; the quoted multiargument gh-title test is a grammar safety control only.

Production output witnesses are bound to their parsed descriptor routing. A commit
line needs the commit's captured stdout; a HEAD witness needs the immediately bound
HEAD command's captured stdout. Known descriptor duplication/restoration is tracked.
Hidden HEAD output cannot borrow an unrelated visible SHA. Additional visible
rev-parse/log/show commands invalidate production proof, while genuinely discarded
quiet companions remain supported. Expanded output paths, descriptor device paths
and unresolved descriptors cannot serve as witnesses. Push range evidence requires
captured stderr. Ordinary commit+HEAD, commit+push and bound commit+HEAD+push remain
supported. Merge creation needs its own captured creation line and bound HEAD.
These are conservative fixture proof rules, not native stream-decoder qualification.

## Ingestion and atomic last-good state

`accept_envelope` checks shape, origin, clocks, source lineage, settings basis,
ownership, allocation and usage invariants, and recomputes health. Malformed records
are dropped while valid neighbors survive; dependent requests/operations with removed
owners also fail. Duplicate IDs cannot become independent samples. Any dropped
record adds an error and invalidates completeness. Timestamp checks bound exact
built-in integers/floats before any floating conversion, including excessive JSON
integers; booleans are rejected. Finite malformed neighbor timestamps remain local
record errors. Nonfinite JSON is invalid as a whole and rejected by the decoder.
Header/source/schema failures reject the whole incoming publication. Retained records are never relabeled as
freshly observed simply because a file was rewritten.

`collectors.runs.ingest(previous, raw, now=...)` is a pure state transition. Missing,
invalid, interrupted, regressed or future input keeps the prior export and its
original source/as_of and received-at clocks, with a dated failure. Successful partial
input replaces the prior export with validated partial evidence; invalid records
are not silently merged with older records. Recovery clears the active failure but
cannot freshen original source timestamps. `status` exposes error, partial, stale,
source age and last receipt independently. Age uses the oldest source snapshot and
as_of, never fetch time or generated_at.

`refresh` requires an explicit file path OR injected `EvidenceTransport`, an explicit
publication path and `now`. The file reader caps input at 4 MiB. It writes only the
specified publication through a same-directory temporary file, fsync and atomic
replace. Process interruption/failure before replacement preserves the old file;
temporary-file cleanup is attempted. This is a single-writer contract, not concurrent
merge support or a promise of directory-fsync durability across power loss. Corrupt
existing cache files cannot be reconstructed without another valid publication.

Limits: 4 MiB input/output/cache, 5,000 input records, 32 sources, 500 turns/run,
8,192 command characters, 65,536 output characters and 512 shell tokens. Excess or
unsupported records fail closed. The CLI has the POSIX deadline; pure helpers bound
work structurally and do not install signal handlers. The transport interface
requires a 1..30 second deadline and 4 MiB bound **before buffering**; no transport
implementation ships here. A callback is trusted to honor those budgets; this module
does not claim it can forcibly preempt an arbitrary injected Python callback.

## Exact future adapter facts still needed

Root supplied one private guarded current-thread/config metadata observation captured
2026-10-29 22:26:05 UTC. It exposes `recentRuns.runId/ordinal/status/providerInstanceId/
model/requestedAt/startedAt/completedAt`, thread IDs/current model/instance and current
`modelSelection.options`. It supplies no historical requested options, provider-reported
model/tier/effort, native provider turn/session identity, usage, tool calls or completions.
Its empty `items` result was a scoped metadata read, not evidence of no messages.
The committed sanitized regression changes identifying values and proves this object
cannot be mistaken for a fixture bundle or backfill historical settings.

Before a separate live adapter can be reviewed, its owner must supply bounded,
sanitized examples and field-level provenance for:

1. Current run/attempt -> native provider thread/session/turn mappings; original
   parent-run/parent-turn links, expected-child inventories/completion and per-turn
   start/end/status, including resumes and late children. The guarded run ID is
   available; these native projection fields are not in the supplied metadata.
2. Per-turn historical **requested** option observations and actual provider-reported
   provider/model/version/effort/tier, with their event IDs. Absent reported fields
   stay unknown. Current config and instance labels are insufficient.
3. Native Codex code-mode/unified-exec call, delayed wait/poll, final completion,
   process-session and child call event shapes; call/session identity fields,
   explicit exit status and output framing. Equivalent Claude tool-use/result
   records, stable request IDs, original owning turn and final-revision ordering.
4. Actual usage field availability (especially optional cache creation), main-agent
   scope/hasSubagents, turn-ID cumulative snapshots, late finished-turn updates,
   reset/baseline semantics and any native source reconciliation records. The pinned
   official v0.1.5 semantics in `Orch-015-EVIDENCE-ADAPTER-FACTS.md` constrain this design;
   they are not live-runtime fixtures or a verified decoding path.
5. A nonsecret resolver for the actual configured provider source location. Pinned
   v0.1.5 `CodexHomeLayout.ts` permits configured shared/direct or authOverlay layouts;
   no hard-coded default path is valid qualification. No auth entries, key reads,
   config mutation or live location discovery is part of this packet.
6. Explicit full repository/worktree/object inventory and operation-time repo
   provenance, stable tool completion associations, exact output/observation source
   basis, inventory/pagination/retention gaps, bounded allowed source paths, original
   snapshot clocks and complete-through watermarks for the proposed window. Optional
   feed/observer inputs must have their own source identity and remain optional.

None of these missing technical fixtures blocks using the offline core. They block
claims of native decoding, qualified live transport, actual producer fleet coverage
or runtime acceptance. Packet B/C/D and Swift integration remain separate work.

## Current-source adapter

The offline v1 contract remains intact. A current Orch/Codex read-only acquisition
implementation and `agent-evidence/2` are documented in
[model-evidence-native.md](model-evidence-native.md).
