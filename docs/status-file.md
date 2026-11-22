# Collector, store and status file

FlightOps has one collector. It reads GitHub through your `gh` login, keeps history in one SQLite file, and writes one JSON status file. The app and the terminal only read that file.

```
python3 collectors/flightops.py use acme/api acme/web   # pick repos (writes the config)
python3 collectors/flightops.py collect                  # one pass: read GitHub, write the status file
python3 collectors/flightops.py status                   # print the status line and stages
python3 collectors/flightops.py status --json            # print the whole file
python3 collectors/flightops.py repos                    # list repos your gh login can see (for the picker)
python3 collectors/flightops.py replay 2026-09-20 2026-10-31   # replay bottleneck verdicts day by day from the store
```

It needs Python 3 (standard library only) and `gh` signed in. It never writes to GitHub.

## Config

`~/.config/flightops/config.json` (or `$FLIGHTOPS_CONFIG`):

```json
{
  "repos": ["acme/api", {"name": "acme/web", "color": "#4682B4"}],
  "approval_check": "review-gate",
  "machines": []
}
```

| Key | Meaning |
| --- | --- |
| `repos` | `owner/name` strings, or objects with a `color`. Each repo's default branch is read from GitHub. Repos without a colour get one from a fixed palette. |
| `approval_check` | Optional. A check or commit status whose success counts as approval. Without it, approval is GitHub's review decision. |
| `machines` | Reserved for other machines (SSH names). Not read yet. |

## Each pass

A pass runs at most 90 seconds and stops early when GitHub's remaining budget drops under 500.

1. **Now.** One GraphQL query per repo: open PRs with their checks, reviews and review requests, and the default branch.
2. **History.** PRs updated in the last 90 days, 50 at a time, with ready, draft, review-requested, merged and closed events and every review. Later passes read only what changed.
3. **CI.** Workflow runs from the last day on every pass (a quiet repo answers 304 for free), then six more days of older runs per pass until 90 days are covered.
4. **CI jobs.** Up to 30 finished runs per pass, newest first, for each job's queued, started and completed times and its runner.

The first pass gives a full Now view. History and CI detail fill in behind it over the next passes.

## Store

`~/.local/share/flightops/flightops.sqlite` (or `$FLIGHTOPS_DATA_DIR`). Its PR and CI tables keep the column names of the earlier evidence ledger so old history can be copied in.

| Table | Holds |
| --- | --- |
| `prs` | One row per PR: state, draft, created, merged, closed, author, title |
| `pr_timeline` | Ready, draft, review-requested, merged, closed and reopened events, and reviews (`type = 'Review'`, state in `detail`) |
| `ci_run_attempts` | Each workflow run attempt, and whether its jobs were read |
| `ci_jobs` | Each job's queued, started and completed times, conclusion and runner |
| `snapshots` | The latest open-PR answer per repo |
| `reads` | When each part of each repo last read well or failed, and why |
| `passes`, `cursors`, `etags`, `meta` | What each pass spent and where it left off |

No prompts, code, logs or credentials are stored.

## Status file, version 2

`~/.local/state/flightops/status.json` (or `$FLIGHTOPS_STATUS_FILE`). `collectors/sample-status.json` is a complete example; it drives demo mode and the tests (`python3 tests/make_sample.py` rebuilds it).

| Key | Holds |
| --- | --- |
| `schema` | `flightops-status/2` |
| `generated_at`, `viewer` | When it was written, and your GitHub login |
| `config` | `state` (`ok`, `missing`, `unreadable`) and how many repos are configured |
| `repos` | One entry per repo: `id` (`owner/name`), `name`, `color`, `state`, `read_at`, `error`, `default_branch` |
| `sources` | `github`, `agent_logs` and `machines`, each with a `state` |
| `views` | `all`, plus one per repo id. Every view has the same shape |
| `prs` | Every open PR: `id`, `repo`, `number`, `title`, `url`, `author`, `stage`, `stage_since`, `waiting_on`, `ci`, `review`, `needs_you` |
| `usage`, `machines` | `null` until the Models and Machines sources land |

A view:

| Key | Holds |
| --- | --- |
| `state` | `ok`, `stale` (older than 10 minutes, or the last read failed) or `unknown` (never read) |
| `partial` | Some repos in the view have never been read |
| `status_line` | One sentence for the top of the panel |
| `counts` | `open`, `merged_today` (since local midnight), `needs_you` |
| `stages` | Draft, Fixing, Waiting for CI, In CI, In review, Approved, Merged today: `count`, `oldest_since`, `oldest_pr`, `verdict` for Waiting for CI, In CI and In review (see Bottlenecks), `null` for the rest |
| `bottlenecks` | Up to 20 past and ongoing Slowing or Stuck episodes in 90 days: `stage`, `state` (the worst reached), `start`, `end` (`null` while ongoing), `hours_lost` (waiting beyond normal, summed over the items caught in it), largest first |
| `prs`, `needs_you` | PR ids in this view, in stage order, oldest first |
| `trends` | 90 days: `days`, `opened`, `merged`, `open` at the end of each day, `ci_jobs`, `ci_wait_p90_min` (the slowest tenth of queue waits; `null` under 5 jobs), `merge_hours_p50` and `merge_hours_p90` (open to merge for PRs merged that day; `null` under 3 and 5 merges), `verdicts` (each judged stage's worst verdict per day) |
| `history` | `complete` once 90 days of PRs, runs and jobs are read; `oldest` day held |

Rules:

- Unknown is never zero. A repo that was never read has `null` counts, and the total says it is partial.
- A stage is decided from the PR as it is now: draft; failing checks or changes requested (Fixing); a check running (In CI); a check queued (Waiting for CI); approved; otherwise In review. A cancelled check isn't failing, and a repo with no CI goes straight to review.
- Needs you: your review is requested, a PR is approved and not merged, or CI has failed for 6 hours with no new push.

## Bottlenecks

Three stages get a verdict: Waiting for CI and In CI (from CI job times), and In review (from a PR being ready to its first review by someone else, or its merge). Cancelled and skipped jobs, PRs closed without a review, and bot PRs are left out: none of them was waiting on anyone.

Each verdict compares the stage now with its own normal:

- **Normal** is the lower third, over the last 28 days, of each day's slowest tenth of waits (days with at least 5 items), so a jam lasting most of a month still doesn't become normal. A re-run job's wait starts when the re-run does. With fewer than 20 items in 28 days the verdict is `learning`.
- **Load** is items that entered in the last 24 hours against the 28-day daily average.
- **Wait** is how long items have waited now, and the slowest tenth of items that left in the last 6 hours (once at least 10 have). Items waiting over 2 days (3 for review) count as parked and are left out: that is a forgotten item, not a queue.

One forgotten item is not a bottleneck, so a wait only counts when at least 2 CI jobs, or 3 PRs in review, share it.

The bar is normal or a floor, whichever is larger (15 minutes for both CI stages, 4 hours for review), so a small wait is never an alarm.

| Verdict | Rule |
| --- | --- |
| `stuck` | At least 2 items (3 for review) have waited over 3 times the bar, or the slowest tenth has |
| `slowing` | The same, over 1.5 times the bar |
| `busy` | Load is at least twice usual and at least 10, and waits are normal: a wave that is flowing |
| `clear` | None of the above |

A verdict holds for 30 minutes so a stage doesn't flicker. Each `verdict` holds `state`, `reason` (one sentence), `since` (when an ongoing Slowing or Stuck episode began), `oldest_pr`, `load_24h`, `usual_load`, `oldest_min`, `slow_end_min` and `normal_min`.

Time to clear was tried and left out: CI jobs and reviews run side by side, so items waiting divided by the rate leaving misreads a normal burst as a slowdown.

The app shows a card for any stage that is Busy, Slowing or Stuck, ranks `bottlenecks` on Trends, and sends one notification when a stage turns Stuck and one when it clears.
