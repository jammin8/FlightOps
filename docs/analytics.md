# Delivery analytics

How FlightOps turns what it already collects into bottleneck signals. Everything is computed locally from
three bounded histories; nothing is uploaded.

## Data

| History | Recorded | Kept | Holds |
|---|---|---|---|
| Flow samples | each GitHub refresh (≥10 min apart) | 7 days | open PRs per status, main status per repo, API/AI budget readings |
| PR timelines | each GitHub refresh | 7 days after a PR ends | each PR's status spans (from, to), flaky checks seen, opened/merged times |
| Lane timing | each host refresh (from the host's status JSON) | — | typical run time, median queue wait, last job finished |

Statuses, in pipeline order: draft → fixing → waiting CI → in CI → in review → approved → merged.

## Signals

- **Cycle-time phases.** For PRs active in the window, the median and 85th percentile of time spent in each
  status. Shown as one stacked bar (median) with the p85 on click.
- **Bottleneck sentence.** The status holding the largest share of PR-time in the window, compared with the
  window before it: "In CI held 58% of PR time, up from 41%."
- **Aging.** A board card turns amber when its time in the current status passes that status's p85.
- **Rework.** A PR moving backward in the pipeline order (CI fails after passing, review asks for changes,
  a conflict after it was clean). Counted per PR (loops, time spent after the first backward move until it
  regains its furthest status) and in total (share of PRs with rework, share of PR time spent on rework).
- **CI queue vs run.** Per lane: median queue wait against median run time, and the oldest PR waiting for CI.
- **Review capacity.** PRs entering review vs PRs leaving it (approved or merged) over the last 24 h. More
  arriving than leaving means review is the constraint.
- **Flaky checks.** A check that failed and then passed on the same commit. Counted per check name, with the
  PRs it hit.
- **Main red time.** Share of the window each repo's main branch was failing, and the last time back to green.
- **Budget projections.** From recent budget readings: when each budget reaches its limit at the current pace,
  and when it resets.

Every signal says "not enough history yet" until it has at least a few hours of samples.

## Model publication integrity

The external model publisher supplies a plain PR dictionary. FlightOps observes it at startup, on manual refresh,
when a live repository refresh succeeds, and every minute independently of GitHub success. The Models page and
Copy Status show read failures, retained historical data, file age (including files older than 30 minutes), malformed
PR evidence and conflicting run identities. File modification time is only local file age: even a newly written
legacy report has **unknown run/head freshness**. Neither its time nor a thread-to-branch association proves that
all work on the current PR head was covered. No producer coverage-end or actual-setting field is invented.

Malformed fields and run elements are isolated so valid neighboring PRs still load. A warm read retains the affected
PR's last good historical record; that record's complete usage is unavailable until a valid replacement arrives.
Any dropped evidence invalidates that PR's complete token claims, including otherwise valid aggregate totals.
A missing or malformed whole publication keeps the last successful inventory and its original file time, with a
visible error. A valid empty dictionary replaces the inventory and is distinct from a missing source.

Token counts exclude cache reads. Existing provider fixtures include output-only and other partial component
objects; absent components retain their historical zero contribution when at least one fresh-usage component is
explicitly present. This is a legacy representation rule, not proof of producer completeness. An empty or cache-only
object is unknown. Explicit zero remains zero. Negative values in any bucket, malformed integers and sums beyond
`Int.max` are unknown. Addition across runs is checked; averages are formatted without converting rounded large
floating-point values back to integers.

Identified review/rework runs deduplicate within each role. Inventory and role projections can omit fields such as
role; only contradictory supplied identity fields conflict. One PR-level resolver combines compatible same-ID facts
across inventory, review, rework and identified-builder projections before selecting a role or subtotal. Unknown usage
in any same-ID copy stays unknown; shared allocation or explicit incomplete usage also constrains every consumer of
that identity. Original role membership and known work counts remain available even when usage is excluded. An
unrelated run's unknown usage does not suppress independently valid role/builder subtotals. Unidentified legacy
occurrences are not linked merely because their setup/usage fields match. Conflicting copies, a run assigned to both exclusive
roles, or an identity reused across PRs with any exclusive allocation make affected usage and run counts unknown.
Checks include PRs outside the displayed window. The Board rework line uses the same validated summary, including an unknown count when every supplied rework record is unreadable. Explicitly shared usage is excluded from PR totals. Historical
runs without IDs remain separate reported occurrences; their identities cannot be independently deduplicated.
Repeated inventory IDs invalidate a precomputed complete aggregate rather than guessing whether it was doubled.
Supplied constituent usage that is unknown or explicitly incomplete also vetoes a complete PR aggregate. Missing
legacy per-run completeness flags do not invalidate otherwise known usage. A separately complete builder subtotal
can remain known when unrelated review usage is incomplete; a switched builder projection is not credited with the
all-setup total even when a complete inventory establishes that total.

Checked aggregate arithmetic includes a known identified builder present only in `built`, deduplicating its ID when
it is also represented by inventory/role projections. An unidentified builder-thread summary may overlap multiple
runs and is not blindly added. A distinct builder plus other known usage beyond `Int.max` cannot establish a complete
PR total; exact-boundary, zero and same-ID totals remain valid, and a separately complete builder subtotal is retained.

Missing GitHub receipts display dashes; a decoded empty inventory can display zero, alongside its visible coverage
and freshness qualifications. Charts and Copy Status keep missing merge counts unknown. A missing/truncated open
inventory cannot establish a merged share. A nonnil lossy array is not a complete inventory: per-repo/global shares
and Copy Status retain PR/repository decode warnings. Unassignable dropped rows withhold all affected denominators
and label retained counts observed/partial. Valid empty arrays remain known zero counts. Empty `0/0` cohorts have no
measured share.

Best / Needs attention explicitly blocks unverified legacy publication coverage and still requires verified measure inputs, coverage or conservative missingness bounds,
precision, comparable task-size support, adjusted effects and calibration. These consumer checks constrain
historical inputs; final independent review and native acceptance remain separate, and missing head-to-producer
receipts still prevent unsupported model-winner claims.
Linux non-UI regressions cover these contracts. Native SwiftUI rendering and the external publisher remain separate
acceptance boundaries.
