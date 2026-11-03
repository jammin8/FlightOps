# Pure evidence-to-head joins, version 1

`joins.empty_input(agent, generated_at=...)` constructs an explicit empty bundle.
`joins.derive(bundle)` validates and derives `evidence-head-joins/1` without reading
files, clocks, configuration, feeds or credentials. `joins.refresh(previous, bundle,
now=...)` is a pure last-good state transition; it retains original result/source
clocks and receipt time after missing, invalid or regressed input. No transport,
scheduler, live adapter, merge permission or UI integration is included.

The new input and output schemas are evidence-head-input-v1.schema.json and
evidence-head-output-v1.schema.json. Both reference the unchanged strict
agent-evidence/2 contract. Runtime validation dispatches that field separately and
validates all remaining fields with closed typed schemas, bounded clocks/counts,
full lowercase SHA1 object IDs and lowercase owner/repository identity. Arrays have
explicit caps; canonical input/output size is at most4MiB. Unknown fields, raw
commands/body fields, source digest mismatches, source namespace collisions and
unsupported forms are rejected rather than coerced into exact facts.

Join sources declare synthetic or supplied origin, original generated and snapshot
clocks, digest, gaps and optional completeness. These values are supplied evidence,
not authentication. The core checks typed lineage and internal consistency; it
cannot authenticate arbitrary caller claims, receipt digests or actor identity.
Every result says authentication=not-performed and merge_authority=false. Synthetic
edges stay synthetic; other edges are unverified-supplied. Structural eligibility
does not establish actual native producer attestation, policy authenticity, a safe
merge, a model winner or a qualified live feed.

Operation receipts require distinct owning run/attempt/session/turn, actor,
invocation/completion/process bindings, source digests, completion and repository
references, terminal outcome, full repository/SHA and typed creation/publication
witness fields. Creation requires a different before SHA and matching parent.
Publication requires its explicit matching destination repository and supported
branch ref. It never proves creation. No command parsing, narrative, trailers,
project UUID, branch/title or proximity-to-SHA heuristic is used. Conflicting
producers, invocation bindings or duplicate final revisions remain excluded.

`normalize_pr_record(record, repository, evidence, policy_id=None,
policy_read=None)` projects the existing receipts.pr_record/pr-facts/2 shape. The
canonical repository mapping must be explicit; there is no built-in alias map or
feed path. It preserves full head identities, original read/refresh-failed clocks,
R0 assertions (including numeric receipt IDs), anchor, sync/replaced state, first
attempt/completed/latest and first-decided as distinct fields. It discards trailers
and family as attribution. Preserve the original immutable artifact and its clocks
alongside the supplied digest; normalization does not independently reattest it.

CI receipts preserve stable check, suite, App, workflow/event, run number and
optional workflow run_attempt. Missing run_attempt is never filled from run number,
timestamps or check order. Check-ID ordering can still support a descriptive first
check attempt separately. Required policy/job histories must be explicit and
consistent with head summaries for structural verified-R0 CI eligibility. Candidate
or supported R0, first-completed/first-decided, missing history, policy clocks,
failed head refreshes and conflicting reused R0 receipts cannot stand in for it.
First failure remains distinct from a later passing rerun. A terminal check cannot
be silently rewritten as passing through a receipt revision. Original normalized
summaries and receipts remain visible even when recomputed eligibility is unknown.

Rejected contradictory check receipts retain their repository, full head and
App/job/mechanism scope in CI evaluation. A later passing survivor and updated head
summary cannot restore the affected required history; its outcomes stay unknown and
R0 eligibility is withheld. Conflicts on unrelated heads or unrequired jobs do not
invalidate an independently complete required history.

Assignments declare a task/revision, exact repository/PR/head, actor/run/turn, role,
trigger reference, assigned/resolved times and supersession. Intent alone is not a
producer receipt. Independent review requires a linked producer with a different
bound actor, explicit reviewed SHA and terminal review result. Rework requires an
exact preceding CI failure or qualified changes-requested review. Missing, cyclic,
superseded, conflicting or dangling relationships retain reasons; an invalid
review cannot qualify a downstream rework trigger.
Every successor independently validates its own predecessor's task, repository,
PR and strict assignment-time ordering. Predecessor deactivation is tracked
separately and cannot authorize another successor's invalid supersession edge.

Deduplication is deterministic by receipt ID/final revision, preserving conflicts,
superseded dispositions and immutable owner/target identity. Exact duplicate copies
and input ordering do not change derived output. Agent requests, original owners,
revisions, bucket knownness, settings provenance, source clocks and gaps are retained
unchanged. Multiple PR memberships are shared, never allocated or counted twice as
exclusive usage. Per-request exact linkage and attempt-level whole-main usage are
separate: whole-main eligibility additionally requires reconciled complete turns
and explicit empty child inventory. It is not whole-PR or historical completeness.

Separate denominators cover selected projection runs/turns, accepted turns,
captured responses, known response observations, reconciled main turns, exact linked
and shared responses, PR-head observations, claimed verified R0 versus eligible CI
heads, explicit roles/reviews, known child allocation and complete retention.
Current agent-evidence/2 complete-through stays null, so retention remains unknown.
No90-day cohort, common-support/precision gate, calibration or decision API changes
are implemented or implied here; aligned cohorts/kernel/UI remain later components.

Focused tests use only synthetic typed fixtures. Actual-input controls are separately
sealed outside source: accepted agent snapshot alone retains199 known response
observations and zero proven operation/role/PR links. The explicitly supplied later
453-PR/1887-head artifact adds no producer/assignment or stable attempt attestation;
its installed-deriver R0 assertions do not become eligible CI heads. Its later
capture clock never freshens the earlier agent snapshot. Correct accepted run
status accounting is144 complete,3 in progress and4 unknown (7 combined).

Remaining actual adapter facts are completed invocation/process/repository/object
proof, authoritative actor/assignment/review/rework events and stable original check
attempt/policy history. This core has functioning structural paths demonstrated by
synthetic receipts; it does not install capture instrumentation or replace missing
facts with caller qualification booleans. New full independent source review is
required before later adapter/consumer integration.
