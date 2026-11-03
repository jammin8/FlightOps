# Evidence ledger

`evidence.sqlite` in the VM data directory keeps CI evidence and branch history that GitHub would otherwise lose, or that no one was reading. GitHub deletes checks, runs and statuses after 14 days on all three repositories. The ledger is written only by the collect pass. The snapshot carries only its progress and errors. The Best pass reads `pr_witness`, `gaps` and `passes` read-only as descriptive evidence, and the ledger ranks no model.

## What it holds

| Table | Holds | Rule |
|---|---|---|
| `ci_run_attempts` | Each workflow run attempt: run, attempt, workflow, event, conclusion, tested SHA, head repository, PR numbers, requested and started times | Append-only. A changed observation adds a row. Each source keeps its own row |
| `ci_jobs` | Each job of each attempt: queued, started and completed times, runner, labels, check run, names of unsuccessful steps | Append-only |
| `ci_check_runs` | Check runs: App, suite, tested SHA, conclusion, times | Append-only |
| `ci_statuses` | Commit statuses, such as `review-gate` | Append-only |
| `activity_events` | Repository Activity: repo, ref, full before and after SHAs, event ID, type, time, actor | Append-only |
| `policy_reads`, `head_policy` | Required checks as read, and the policy attached to each head when first seen | A later policy never rewrites a head |
| `branch_incarnations` | One row per lifetime of a branch name | Rebuilt from events |
| `prs`, `pr_timeline`, `pr_heads`, `pr_witness` | PR inventory, ready and draft events, every head during the PR, derived fields | `pr_witness` is rebuilt from events |
| `gap_events`, `activity_coverage`, `passes`, `import_files` | Every gap opening, change and closing, what each page covered, what each pass spent, what each import read | `gap_events` is append-only |

Triggers refuse any update or delete on the append-only tables, so a green rerun can't remove the first failure. Views: `ci_attempt_latest`, `ci_job_latest`, `ci_attempt_clocks` and `ci_retry_outcomes`.

There are no raw logs, prompts, credentials, PR titles, commit messages, check output or status descriptions in the file.

## Labels

Conclusions are GitHub's words, unchanged. The only derived label is `retry recovered`: an attempt failed, and a later attempt of the same run or job passed, with no failure after it. It says what happened. It says nothing about cause.

## Clocks

| Field | Comes from | Unknown when |
|---|---|---|
| Requested | Run `created_at` | Never seen |
| Queued | Job `created_at`; for an attempt, the earliest job | Jobs not read |
| Started | Run `run_started_at`, job and check `started_at` | Not reported |
| Completed | Job and check `completed_at`; for an attempt, the latest job, only when every job has one | Any job unfinished or unread |

Clocks are stored as GitHub sends them, even when a skipped job completes before it starts. `tested_sha` is the SHA GitHub reports for the run or check. For `pull_request` runs the workflow may test a merge ref, and GitHub doesn't report that SHA here.

## PR fields

Head at open and first-ready are separate fields, each with its own status and basis.

**Binding.** A PR binds to Activity only if its head repository is this repository, and then only to the branch incarnation alive when it opened. A reused branch name is a new incarnation. If two fit, only the one holding the final head counts. A fork or a deleted head repository stays unknown.

| Status | Means |
|---|---|
| `exact` | The Activity chain gives one SHA at that moment |
| `tied` | A push landed in the same second. No SHA, and the candidates are listed |
| `gap` | The chain breaks around that moment. No SHA, and the candidates are listed |
| `pending` | Activity doesn't yet cover that moment, or the timeline was cut short and shows no draft or ready event |
| `unknown` | The basis says why, such as `before-activity-feed` or `head-repository-differs` |
| `not-applicable` | Never ready, with the reason in the basis |

A push after the moment never counts, even by one second. `first_ready_at_basis` is `ready-for-review-event`, `opened-ready`, `opened-ready-before-draft-event`, `still-draft`, `closed-as-draft`, `draft-state-unknown` or `timeline-incomplete`. Readiness at opening is inferred only from a complete timeline, because a cut-off timeline can hide a draft-to-ready event. Late Activity on a branch resolves its PRs again.

`final_head_check` says whether the final head is in the bound incarnation (`in-incarnation`, `other-incarnation`, `absent`, `not-checked`). It is a check. It doesn't make any other field complete. Activity shows what GitHub recorded, so it doesn't prove that every intermediate push is present: a missing push shows up only as a chain break.

## How a pass spends

Order: budget, policy (daily), Activity, PR inventory, then local reconciling, then CI, then the import. Each stage fails on its own. Every response commits with its cursor in one transaction, so a killed pass resumes where it stopped.

| Limit | Value |
|---|---|
| Stop all reads below | 1,000 remaining, REST and GraphQL, read from every response |
| Budget unknown | No reads |
| REST requests per pass, 304s included | 80, the run feed first, then half for heads, then attempts and jobs |
| Run feed per pass | 12 requests, taken first |
| Activity pages per repository per pass | 6 |
| PR inventory pages per pass | 4 |
| Secondary limit or `Retry-After` | The pass stops |

Activity, run, check and status reads are conditional where a stored ETag exists. A 304 costs no GitHub quota, but it counts against the pass's limit, and the pass records 304s separately. Activity reads back 30 minutes past its last read, so late events are still found. Heads are read in this order: open PR heads, heads inside the 14 days oldest first, then older heads newest first. An unfinished head is reread every 15 minutes and an open PR's head hourly. A finished superseded head settles after 6 hours. It is not read again on its own: the run feed brings it back.

## Run feed

Two reads share the 12 requests, per repository, and work at any head age. Both record only runs for known heads. Any new observation records the attempt, queues earlier attempts and jobs, and returns a finished head for a full read.

- **Created feed.** Runs created since the last cursor, less a 30-minute overlap, conditional when quiet.
- **Sweep.** The unfiltered run listing, a few pages a pass from a saved page number, to its end and over again. GitHub keeps a rerun's original `created_at` and the `created` filter follows it, so only this read finds a rerun of an old run. A full sweep of everything GitHub holds takes about 4 hours.

Gaps are `feed-behind`, on subject `hole` for created runs the feed couldn't finish, closed by the next sweep begun after it, `front` after an hour without a created read, and `sweep` after a day without a finished sweep. A rerun that GitHub deleted before a sweep reached it is not seen. A check or status change with no run change is not seen on a finished head.

## Import

`~/ci-program/data/daily/<date>/flightops-<repo>/runs.json` is imported once. The file list freezes on first read, so later snapshots are ignored. Files are opened read-only and streamed, so the 512M limit holds, in slices of up to 15 seconds that resume on the exact byte. Rows carry `source = ci-program-daily`, with the snapshot's end as their clock. Each file's limits are stored in `import_files`: run level only, the attempt GitHub listed that day, no jobs, checks or policy, and conclusions can be stale.

## Gaps

`gaps` is a view with one row per episode of a known hole, built from append-only `gap_events`. A reopened gap is a new episode, and each keeps its original time and detail. `source` is `activity`, `prs`, `ci`, `policy` or `import`.

| Kind | Means |
|---|---|
| `walk-open` | A paged read is unfinished. The detail names the unread range |
| `read-failed`, `row-malformed`, `cursor-lost` | A read failed, a row was refused, or a cursor stopped working. A 404, 410 or 422 on a listing is a failure, never an empty result: a feed keeps its last good clock, and a head retries daily, then its state is `unreadable` once it is past 14 days. A head's gap closes only when run, check and status listings all succeed in one read |
| `chain-break`, `incarnation-start-unseen` | Activity doesn't connect |
| `timeline-truncated`, `runs-truncated`, `checks-truncated`, `statuses-truncated`, `jobs-truncated` | More rows exist than were kept |
| `not-retained` | GitHub holds nothing for a head older than 14 days |
| `none-seen` | No CI evidence inside retention |
| `feed-behind` | The run feed fell behind, as above |
| `attempt-not-retained`, `jobs-not-retained`, `fetch-failed` | One attempt or job list couldn't be read |
| `source-missing`, `source-changed`, `file-unreadable` | An import file problem |

`attempts_unseen` in the report counts runs with an attempt number above what the ledger holds.

## Progress

The `ledger` fact in the status file gives the last finished pass's `backfill_heads_remaining`, `backfill_fetches_remaining`, `heads_in_window`, `heads_in_window_unread`, `oldest_unread_in_window`, `run_feed`, `walks_open`, `import_files_pending`, `gaps_open` and `last_pass`. The ledger runs after publication, so a pass shows up in the next file. A failure is the fact's `error`, the last progress stays, and the fact goes stale after an hour. Publication doesn't depend on it.

## Schema

The file is schema 2. There are no migrations: a file of any other version is refused and left unchanged, and the reason appears as the `ledger` fact's error. To rebuild, move `evidence.sqlite` aside.

## Reading it

```bash
python3 collectors/ledger.py ~/.local/share/flightops --report
```

This opens the file read-only and prints rows, coverage, open gaps and the last pass's spend. `raw/ledger.json` holds the last pass's report, and `raw/ledger.error` its failure. A stand-alone pass takes `collect.lock`, so it can't overlap a timer pass.

Any other reader should open the file with `mode=ro`.
