# Collector, store and status file

FlightOps has one collector. It reads GitHub through your `gh` login, keeps history in one SQLite file, and writes one JSON status file. The app and the terminal only read that file.

```
python3 collectors/flightops.py use acme/api acme/web   # pick repos (writes the config)
python3 collectors/flightops.py collect                  # one pass: read GitHub, write the status file
python3 collectors/flightops.py status                   # print the status line and stages
python3 collectors/flightops.py status --json            # print the whole file
python3 collectors/flightops.py repos                    # list repos your gh login can see (for the picker)
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
| `stages` | Draft, Fixing, Waiting for CI, In CI, In review, Approved, Merged today: `count`, `oldest_since`, `oldest_pr`, `verdict` (`null` until bottleneck verdicts land) |
| `prs`, `needs_you` | PR ids in this view, in stage order, oldest first |
| `trends` | 90 days: `days`, `opened`, `merged`, `open` at the end of each day, `ci_jobs`, `ci_wait_p90_min` (the slowest tenth of queue waits; `null` under 5 jobs) |
| `history` | `complete` once 90 days of PRs, runs and jobs are read; `oldest` day held |

Rules:

- Unknown is never zero. A repo that was never read has `null` counts, and the total says it is partial.
- A stage is decided from the PR as it is now: draft; failing checks or changes requested (Fixing); a check running (In CI); a check queued (Waiting for CI); approved; otherwise In review. A cancelled check isn't failing, and a repo with no CI goes straight to review.
- Needs you: your review is requested, a PR is approved and not merged, or CI has failed for 6 hours with no new push.
