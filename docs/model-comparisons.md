# Offline cohort and comparison kernel

This is the offline cohort and comparison kernel. It does not acquire evidence, import `model_evidence`, read a database, call an API,
run a model, publish app data, or qualify the fleet. It has no Swift implementation.
All successful test calls concern synthetic implementation fixtures.

The future receipt-join adapter owns qualification of actual sources. This kernel
checks the **representation and bindings of those assertions**, not whether a
caller telling it `qualified: true` is truthful. Digests provide replay integrity,
not authentication. A bundle supplied by an arbitrary caller is not live-source
attestation. Source qualification, independent review, integration, UI acceptance,
native packaging and Mac installation are separate checkpoints.

## Boundary and versions

The sole public functions are `build_manifest(normalized_input)` and
`decide(manifest)`. The CLI rebuilds the manifest from the complete normalized
input before replay, rather than trusting derived flags and a recomputed hash.

| Object | Version |
| --- | --- |
| Normalized PR input | `flightops-normalized-pr/1` |
| Normalized proof representation | `flightops-pr-proof/1` |
| Frozen cohort manifest | `flightops-cohort/1` |
| Freeze file (input plus derived manifest) | `flightops-analysis-freeze/1` |
| Decision envelope | `flightops-decision/1` |
| Statistical and cohort policy | `joint-strata-max-bootstrap/2`; `/1` replays only |

`contract.py` defines typed setup, measure, anchor, anchored-size, outcome and PR
observation interfaces. `tests/fixtures/model-analysis-kernel/membership-input.json`
is a complete small example of the JSON contract. It is deliberately synthetic.
No shared acquisition schema or import is established by this boundary.

Required input envelope:

- `as_of`: UTC event-time cutoff; `frozen_at`: UTC knowledge cutoff, at or after
  `as_of`; `snapshot_id`; optional `previous_manifest_hash`; the fixed policy.
- `inventory`: explicit `complete`, `partial`, or `unavailable` and source IDs.
  An unavailable inventory is not a known empty cohort.
- `sources`: unique ID, versioned upstream schema identity, SHA-256 content digest,
  `snapshot_at`, `history_start`, `complete_through`, `complete`, and `gaps`.
  No file mtime substitutes for coverage. Proof sources must cover the entire
  90-day window through `as_of`, with no asserted gaps. The adapter must ensure
  this representation includes retention, pagination, expected child and full
  relevant PR-history coverage, including history before the window if necessary.
- `setups`: IDs and separate provider/model/effort/tier dimensions, each with
  nullable `value`, `basis` (`reported`, `requested`, `unknown`), and evidence IDs.
  Values remain verbatim. Every dimension needs reported evidence for the same PR
  and exact tuple. No requested/default fill. Two IDs for the same exact tuple
  refuse. Unknown dimensions make the setup non-exact.
- `measures`: ID, rate or positive amount, higher/lower direction, unit, semantics,
  and required receipt claims. Units never mix in one measure. Generic measures
  require `measure_verified` and `exact_producer`. CI additionally requires
  `verified_anchor`, `original_attempt_complete`, `exact_producer`; rework requires
  `candidate_inventory_complete`, `assignment_complete`, `resolution_complete`,
  `exact_producer`; usage requires `usage_complete`, `usage_exclusive`,
  `exact_producer`. Semantics cannot weaken these requirements. A generic measure
  is not a way to label unqualified CI, rework, or usage as those measures.
- `observations`: one canonical lowercase `owner/repository#PR` identity, full
  repository and PR number, explicit revision/observed time, first-ready time or
  null, membership proofs, setup or null and possible setup IDs, task stratum,
  maturity time or null, anchor, anchored size or null, `shared` flag, complete
  independence keys, and per-measure value/effective time/evidence/reason.

Identical transport copies deduplicate. Different records with the same PR,
source or proof ID, **including different revisions**, refuse the entire snapshot;
there is no last writer wins. The upstream adapter must explicitly reconcile
revisions and expose its canonical record and provenance. Reopenings, opening
copies and CI reruns are not new PR units. It must not choose a later ready event
or successful rerun to rewrite the original anchor/outcome.

Each proof has an ID, exact proof schema, source ID, upstream raw-content digest,
claim, qualified boolean, canonical PR ID, full unit anchor SHA, effective time,
and recorded time. These bind every included value to source identity, schema
identity and covered source provenance. Additional normalized bindings are:

| Claim | Required binding |
| --- | --- |
| `first_ready` | effective time equals the observation's first-ready time |
| `actual_setup` | `setup_id` and `reported_settings` equal all four declared values |
| `verified_anchor` | `base_sha`, `anchor_kind` match the observation |
| `anchored_size` | `base_sha`, `changed_lines` match fixed base/r0 size |
| Every measure claim | `measure_id`, `value_hash` of `{measure_id,value,effective_at}` |

`value_hash` uses SHA-256 of sorted, compact UTF-8 JSON (`canonical()`), not an
independently invented serialization. Source and proof digests are preserved as
upstream references; the adapter owns their verification against raw evidence.
An upstream receipt spanning several heads is represented here as a qualified
assertion about a particular PR's anchored statistical unit. The adapter must
retain actual original/head/job/app/attempt/request/actor/assignment identities
in its source packet and prove the claim; this kernel does not re-derive them.
Changing a tuple, size, PR, anchor or value without changing its normalized proof
cannot qualify it. Missing, unsupported, unqualified, future-effective or
wrong-identity proofs keep a value missing.

## Frozen populations and anchored strata

Selection is inclusive `[as_of - 90 days, as_of]`, by **first ready**, before any
outcome test. Separate `description_28d` flags use the analogous 28-day interval.
Only proven outside-window membership is excluded. Unknown membership, even an
unproven old/future timestamp, remains a potential cohort member. Explicit known
setup members retain denominators when immature, missing size, unverified,
shared, correlated or outcome-missing. Unknown setups enter each possible group;
an empty candidate list means all family groups. A candidate with unknown actual
settings cannot narrow allocation to itself. Possible denominators therefore may
overlap: they are bounds, never independent observations or fleet totals.

The manifest stores all member/potential IDs, excluded IDs and reasons, included
and missing IDs per measure/setup, eligibility lists, reason histograms, raw
verified rate numerators, maturity, anchors, joint strata, source watermarks,
proofs, record/input hashes, policy, and manifest hash. A late receipt produces a
new snapshot/hash and can link the prior hash. Old freeze files are sufficient
for replay and never consult newer source files. Unknown inventory blocks calls.

Size is changed lines from the **fixed base...r0 diff**, with matching full base
and r0 SHAs and qualified anchored-size/anchor proofs. It is not a sum of commit
churn. Later current-head/rework size is ignored. Missing base or unreproducible
size is unknown. Supported push reconstruction is not a verified anchor; admitted
kinds are opened, first-ready, dispatch or observer receipts. This code performs
no git diff or bounded source acquisition; the adapter supplies a bounded,
qualified anchored diff result.

Conservative v1 size bins (a concrete local policy, not a claim of inherited
historical thresholds): xs 0–20, s 21–100, m 101–500, l 501–2000, xl >2000.
The caller predeclares a full **joint** repository/task/anchored-size reference
mix, positive weights summing to one. No outcome-derived bin fitting. Each common
cell needs at least two independent included PRs in every frozen family group;
its original reference weight contributes to support. At least 70% of the mix
must have common support. Estimates directly standardize the conditional means
using supported weights normalized to one. Uncovered strata and mass are emitted.
Calls explicitly concern this common support, not extrapolation to uncovered work.

Independence keys name canonical `run:`, `request:`, `stack:` or `cluster:` units.
The adapter must enumerate all overlapping allocations, tasks and stacked work,
including other-window PRs if they share units. Every PR sharing any supplied key
is missing for inference; shared PRs are also missing. V1 deliberately excludes
whole correlated components instead of pretending clustered PRs are independent.
Missing units remain denominators. Keys are assertions; an omitted relationship
cannot be discovered from an arbitrary normalized file.

## Decision policy

The input freezes family, fixed reference setup, direction, unit, joint mix,
policy version and seed before testing. V1 does not select or trim the comparison
family by favorable outcomes. Every declared family group must be exact and have
at least 20 independent supported PRs. A bad family member withholds the family;
the caller cannot quietly remove it after seeing results. Other declared groups
are explicitly listed as excluded. A preregistered subset is a separate analysis.
Predeclaration is an upstream governance assertion, not authenticated by hashing.

The estimand is an arithmetic rate/amount mean on common joint support. Positive
amount comparisons use log means and log ratios; values and intervals retain
explicit scale fields so a future renderer can format amounts/ratios. Rates allow
explicit 0 and 1. Missing, malformed, negative, overflow, zero amount and underflow
amount have separate reasons. Positive amounts must be in `[1e-100,1e100]` to keep
sums, variances and log ratios numerically representable; out-of-domain values
remain missing, **not clipped** or turned into an upper bound on missing usage.

For each measure:

1. Check complete inventory, two exact reported setups, verified normalized
   measure representation, at least 20 independent PRs per group and common support.
2. Resample PRs independently **within joint cells**, preserving cell counts.
   A simultaneous bootstrap maximum covers standardized absolute deviations for
   all group estimates and every pair contrast. Its empirical `(1-alpha)` quantile
   uses the conservative upper order statistic; a predeclared Bonferroni normal
   critical floor prevents a degenerate empirical zero radius. The scales are
   fixed plug-in standard errors (nonzero floors), not refitted winner thresholds.
   `alpha=.05/number_of_measures` additionally adjusts the frozen measure family.
   Weighted sums use exact-rounded summation. A rate estimate or bound that rounds
   past 0 or 1 is held at that end, and support mass is published as at most 1,
   so roundoff in a valid mix never becomes an out-of-range rate. The consumer's
   range checks stay as strict as before. Support mass is a plain fold over the
   supported weights sorted by value, computed the same way by the engine and the
   reader, and the 70% gate is `mass >= 0.7 - 1e-12`. Sorting makes it independent of
   `reference_mix` order, and the slack, far above the fold's roundoff, keeps an exact
   70% from depending on it. `/2` also visits cells in sorted order, so reordering the
   mix leaves every measure byte-identical, bootstrap and calibration included. Input,
   manifest and decision hashes still differ, because they bind the input as given.
3. Rate group intervals also envelop weighted cell Wilson intervals with
   Bonferroni cell/group/measure adjustment. Contrasts envelop the corresponding
   group differences. This conservative boundary safeguard complements, and
   **does not replace**, comparative max-statistic adjustment.
4. Precision requires 95% simultaneous rate half-width ≤0.20 for every group.
   For amounts, both group and pair log-interval distances from the point estimate
   must be ≤log(2): each bound is within ×2 of the estimate. Twenty is permission
   to bootstrap, not sufficient precision.
5. `minimum_fraction` is `included/(known denominator + possible extra)` for **each
   group**, counted on the common support the comparison uses. It is descriptive.
   The threshold route needs more: under `/2`, `threshold_pass` requires at least 90%
   in **every supported cell** of that group, compared in whole numbers as
   `10n >= 9(n+m)`. Inference weights cells by the frozen mix, so a plain PR fraction
   can hide a heavily weighted cell: 216/240 can be 55% under the mix. Requiring every
   cell implies 90% under any weights and 90% raw, needs no new field, and lets the
   reader recheck it exactly from the calibration shapes. A cell's `m` is the
   calibration `missing_shape`: unknown-cell PRs count against every supported cell
   and possible setups against every group they might join. The price is more
   refusals, since a cell with fewer than 9 measured PRs tolerates no missing PR and
   an amount has no bounded fallback. Neither this nor any 90% rule proves a call
   against arbitrary missing outcomes: for amounts a small missing fraction can still
   reverse a mean. Only the directional worst-case route survives every completion,
   and rates alone can bound it. A PR whose known joint cell
   is outside that support leaves the decision's populations and appears in their
   `excluded` list as `outside_common_support`, so it can neither raise nor lower
   coverage. PRs with an unknown cell stay in every group they might belong to.
   The manifest keeps the complete cohort populations as the separate descriptive
   fact. Coverage rises as well as falls under this rule: missing PRs in a cell
   that lost support stop counting too, and the 70% support gate and the reported
   uncovered strata carry that loss instead. With no common support at all nothing
   is compared, so the decision keeps the cohort populations and their denominators.
   If every group passes, use the threshold route. Otherwise a particular
   call must also survive directional worst-case bounds. Each missing PR with a
   known cell is adversarial in that cell; unknown-cell missing PRs may occupy
   each supported cell in separate conservative extrema. Unknown possible setup
   allocations count against every affected group. Rate extrema assign 0/1;
   amount extrema allow arbitrarily large missing amounts (null upper endpoint
   means unbounded, not zero). Sampling uncertainty extends the extrema. No
   invented amount cap or missing-at-random assumption enables the bounds route.
6. Best requires the favorable simultaneous **group** interval to separate from
   every other comparable group's interval. NeedsAttention requires the unfavorable
   simultaneous contrast bound against the **fixed reference** to exceed 0.10
   for rates or log(1.5) for amounts. A reference cannot flag its own deficit.
   Bounds-route calls require the analogous worst-case separation/deficit too.
7. Actually calibrate the complete call path under seeded null simulations of
   this cohort shape. Rate scenarios: pooled conditional joint-stratum rates,
   0.01, 0.50, 0.99. Amount scenarios: pooled conditional empirical distribution,
   lognormal sigma 0.5 and 1.5 with identical conditional means across groups.
   PR/cell counts, missingness, possible allocations, imbalance, family size,
   reference and direction stay fixed. Each simulation repeats bootstrap,
   precision, coverage/bounds and effect checks. Count **any** false flag anywhere
   in the family per simulation. Every scenario's one-sided Wilson upper 95%
   bound, adjusted simultaneously over the named scenarios, must be ≤10%.
   Record simulation counts, observed false flags, failure indices/actions,
   uncertainty, seeds, shapes and calibration hash. This is finite-scenario null
   calibration, not a distribution-free guarantee about every possible fleet.

Seeds are derived by hashing the fixed seed and measure/scenario/simulation labels.
No rerolling, threshold tuning or winner-selected calibration. All prerequisites
are held constant in simulation; failed prerequisites cannot yield a real call.
The envelope reports actual counts, support, coverage, simultaneous intervals,
contrasts, effect checks, critical statistic, bootstrap-maxima hash, calibration,
per-group gates/unmet requirements, and replay hashes. The coverage threshold is
reported separately from its allowed directional alternative; a valid bounds
call does not have a falsely red mandatory 90% gate.

Statuses are `InsufficientEvidence`, `NoClearDifference`, `Best`, `NeedsAttention`.
A `calls` list permits independently earned labels. NoClearDifference requires
usable calibrated evidence and threshold coverage, but no qualifying effect.
Every call has all applicable mandatory gates passing. Withheld precision or
calibration is not a clear-difference conclusion. There is no cross-measure score.

## Policy versions

The policy version names the statistical algorithm, separately from the schemas in
the table above, which did not change. `/2` covers the coverage and numeric repairs
below; `/1` is the original algorithm. `decide` dispatches on the manifest's
hash-bound version, so one freeze always replays under the algorithm that made it.
`engine_v1.py` is that engine with only its version import changed, and a test holds
it to that. A `/1` freeze therefore reproduces its original decision, including the
defects `/2` repairs: `test_a_v1_freeze_replays_to_the_decision_the_pre_fix_engine_made`
pins the hash the real pre-fix engine produced for the outside-support freeze.
`build_manifest(packet)` and `freeze` refuse `/1`, so new freezes use `/2`; only
`replay` passes `replaying=True`. A manifest relabelled to another version does not
replay from its normalized input. Replay keeps `/1` bytes exact for audit, and that is
all `/1` output is for. The Swift reader validates each version by its own rules, but
a `/1` decision never reads as current: its calls carry a superseded label with no
supported wording, and a live Best publication under `/1` is refused.

## Replay and resource bounds

From the repository root, using only existing Python standard library:

```sh
python3 -m collectors.model_analysis freeze input.json --output frozen.json
python3 -m collectors.model_analysis replay frozen.json --output decision.json
python3 -m unittest discover -s tests -p 'test_model_analysis*.py' -v
```

Output files must be new; overwrite is refused. CLI exit 0 means a file was
produced, **not** that any setup earned a call. Refusals exit 2 with bounded error
text. Duplicate JSON keys/nonfinite constants and changed freeze/manifest hashes
refuse. Byte-identical replay is tested using an immutable freeze and new output
paths even after replacing the original source input. Keep Python's runtime
version with the source packet: exact floating-point golden hashes were validated
on Python 3.14.8/Linux, not certified across Python releases or architectures.

Limits: 8 MiB normalized input, 32 MiB freeze input/output, 5,000 transport PR
rows, 8 setups, 8 measures, 24 joint cells, 64 sources, 30,000 proofs. Canonical
JSON serialization also enforces the normalized input limit for direct API calls.
The builder refuses after 2,000,000 proof lookups and checks a 45-second wall
budget during major loops and after sealing. The decision engine limits estimated
resampled PR draws across all measures to 45,000,000, and checks a 45-second wall
budget during every simulation and every 20 bootstrap replicates. Bootstrap
replicates are 199–999; null simulations are 120–400 **per scenario**. The estimate
includes observed and null bootstrap draws; generation and scalar summaries are
additional bounded work. Budgets are internal constants, not caller-supplied
escape knobs. More demanding cohorts require a separately reviewed policy,
not an automatic cap increase. A budget failure explicitly withholds calibration
and every call. Wall checks are cooperative, not OS real-time scheduling promises.

No new tests of unchanged collectors/Swift are implied by these commands. Reuse
existing 303-Python/531-logic component evidence and integrated source review as
background only. This candidate still needs full independent Astra review before
integration/result acceptance. An adapter to qualified real evidence and final
UI/native/install acceptance remain separate work owned elsewhere.
