# FlightOps

A mission control view for vibe coding, CI, and agents.

FlightOps is a macOS menu-bar app that shows a software delivery system at a glance: pull requests, CI queues and runs, coding agents and the models behind them, and the machines doing the work. It shows everything and controls nothing.

## Overview

A macOS menu bar dashboard for a small team's coding agents, CI and launch plan. It tells one story: what's wrong right now, what the launch looks
like, then what's moving. The menu bar shows a robot with the number of agents working, a test tube with the
number of CI jobs running ("–" when unknown) and a mark when something needs you now. Clicking it opens a
panel with four tabs (⌘1–⌘4), built from cards; cards with a chevron open in place.

- **Home**: the *launch* (target and countdown, on track /
  tight / at risk, progress, the finish range and how it moved in a week; opens to the launch projects); one
  line of activity (agents, CI jobs, PRs in review, merges in the last 24 h); *Agents and CI* (opens to every
  Orchestrator turn and CI job).
- **Plan**: the launch forecast first (chance by the target, finish range, burn-up with the projection, the
  assumptions in view), then initiatives by priority (open to their projects), other projects, the
  company-wide total, issues in flight and recently done.
- **Repos**: one card per repository: main's checks and its open PRs to main as one bar, exceptions named
  first (failing, ready but not merging). A card opens to chips that filter by segment and the PRs grouped by
  what they need (with merge blockers and how long they've been ready or failing), and its latest merges.
  *Ready* means GitHub proved the current head mergeable with every requirement met (CLEAN/HAS_HOOKS);
  approved PRs with a blocker or without that proof are "approved, not ready". A failed check keeps a PR (or
  main) failing while other checks still run.
- **Systems**: health (exceptions first, opens to every check, one per CI lane), the CI host (CPU and temperature
  sparklines, memory and storage bars, opening to history), the virtual machines, the accounts (GitHub API
  budget, Codex usage) and *Sources*: when each source last answered and why it didn't.

The header names what needs attention now: the item itself for one ("Backend CI · no heartbeat for 58m"),
"N problems" for several, "All clear" for none, plus "partial data" when something it depends on is unknown.
Mission, the header, the menu and the export read one verdict about the system: every check, coverage gap, and
need for now or today. Owner actions and later-tier items never change it; they show as waiting on you.
Dismissing only acknowledges: the health stays, and the line says "acknowledged". Clicking it opens Systems.

## Truthfulness rules

- Unknown is never healthy or zero. A number whose source is out of date shows "–"; a missing heartbeat or
  sensor reading is unknown; stale data says how old it is; the footer shows a dot per source (grey: current,
  orange: out of date, hollow: never seen). "All clear" is only said when everything it depends on is known:
  every source current, no unknown check, a current Orchestrator agent list, no part a collector couldn't read, no list
  cut off, readiness proven for every approved PR. Otherwise the header adds "partial data" (Copy Status lists
  the gaps).
- A failed command on the Mac is an error, not an empty answer (pgrep's "no match" is a real empty answer);
  job ages come from macOS `ps -o etime`; a missing Codex percentage is an error, never 0%.
- One bad field never blanks a source: collectors report each part on its own, and the app decodes records
  leniently (a malformed one is dropped and noted under Systems → Sources).
- The forecast says what it assumes: the pace of the last 28 days, scope as it is today, a burn-up rebuilt
  from today's issues. Thin history (under 8 completions or 5 active days) gives no date; a finish beyond the
  two-year simulation horizon says so (the simulation reaches the target, up to ten years; beyond that there is
  no chance rather than a made-up one); chances are "<1%" / ">99%", never a flat 0% or 100%.

## What needs you

The needs model ranks everything waiting on you. The header shows the *now* tier; Copy Status (footer menu)
exports every tier.

| Tier | What |
|---|---|
| Now | failed checks and warnings worth an interruption (CI host unreachable or its network down, a CI lane without a heartbeat for 30 min, a lane not loaded, Orchestrator down, its collector token expiring or expired, a CI host storage state that isn't OK, GitHub budget under 500, Codex over 90%, a disk over 85%, a failing main branch); agents waiting for you |
| Today | other warnings; PRs proven merge-ready for 2 hours (by the head's own checks) that haven't merged, as one row; owner actions overdue, due within a week or urgent |
| Later | other owner actions; PRs failing for over a day that no agent is working on ("ownership unknown" while the agent list is) |

Items are joined by issue, PR and thread before they're ranked or grouped: a waiting agent absorbs the
owner action and PRs of its issue or link, and an owner action the ready PR of its issue, so each shows once.
Items carry a flag when they belong to the launch.

## Notifications

Episodes, not a stream. Every alertable check reports a problem, known good or unknown, with the time of
its own evidence (a cached GitHub or Codex value keeps its fetch time). A problem notifies once two distinct
pieces of evidence at least 60 s apart show it (after launch or wake); problems that appear together come as
one notification, a newly failing part notifies again at most every 10 minutes, with a reminder every 2
hours. A problem has recovered when two known-good pieces of evidence over 2 minutes show it; a calm reading
that proves nothing (a main branch whose checks are still running, cancelled or not run) is not such
evidence. When all have recovered and nothing else is failing, not even a problem still being confirmed, one
"All clear" closes the episode with the time they recovered; a new failure that confirms first joins the
episode instead. A failure seen and not yet seen clear is held across restarts and sleep: launch and wake
only restart confirmation and recovery counting, and nothing but fresh clear evidence releases the hold. Unknown (a missing reading, a stale cache) holds everything as it was, so a relaunch, a blip
or a gap never announces anything. An agent newly waiting for you
gets a time-sensitive notification. "Snooze 1 Hour" mutes everything; what happened meanwhile is told
afterwards, recoveries only once confirmed. A notification the system refuses is posted again later with its
original times. Open goes to the item's page, or opens the panel on the right tab.

## The launch

The launch cohort is the non-canceled projects under the Linear initiative named by `linear_initiative` in the
local config (see *Configuration*). Its
target is the earliest target date among them, else 2026-12-01. To override either, write
`~/.config/flightops/launch.json`:

```json
{"project_ids": ["<Linear project id>", "..."], "target": "2026-12-01"}
```

Both keys are optional. The cohort is established or the launch says why not: an override naming unknown
or canceled projects, an unreadable launch.json, an initiative whose project list wasn't fetched in full,
or a cohort without projects or issues is an error with no verdict, never a smaller scope or "done". Each
Linear fetch records the day's numbers in
`~/Library/Caches/flightops/launch-history.json`; drift ("since Oct 16: finish +9 d · scope +14") appears
once a snapshot of the same project set is a week old.

## Main branches

Each repo's main is failing (a check failed, even while others run), passing, running, cancelled or unknown
(no check state reported: shown with a question mark and counted as a gap). Only passing recovers a failure.
A repo that runs no checks on main by design can say so in `~/.config/flightops/repos.json`:

```json
{"main_without_checks": ["ios"]}
```

Its check-less main is then "no checks by design" (neutral, not a gap, not recovery evidence).

## Models

The Models tab compares agent setups (model with its version, effort level and harness), every measure per PR. It
makes no call the data can't back (Models design, Phase 0):

- **Best / Needs attention** shows the computed decision for each measure (first-try CI, rework, tokens) from the VM
  snapshot, with measured counts, setup labels, computed blockers and traced PRs. On today's data every measure is
  insufficient evidence.
- **Builders** are exact (model and effort) only when the orchestrator saw the builder thread working on the PR's
  head branch. Builders known only by family (a branch prefix, an old report) or not at all are listed apart as
  PR counts over the window's total, in name order, with no outcomes. Every PR is in a group: a thread that switched models while building is Mixed, a missing model or
  effort is Unknown.
- **No CI fix and rework** come from the status sampled at each refresh and are marked `*`: they describe, they don't
  rank. "No CI fix" is the share of PRs that ran CI and were never sent back to fixing after it (a merge conflict also
  sends a PR to fixing, so it is not a pass rate).
- **Tokens** are one measure on the Models tab and the PR card: input, cache writes and output, no cache reads. A
  number appears only when every run's usage is known and the PR's whole history was read; a run shared by several
  PRs counts in none. The PR card counts every run linked to the PR; the Models tab counts the builder thread, and
  only when it ran one setup (a follow-up on another model or effort is never credited to the builder's).
- `pr-models.json` version 2 (`"schema": "pr-models/2"`) reports each run's own model, effort and usage, read from
  the agent logs by Orchestrator's turn ids (`agent-tokens.py` version 3 on the agents VM), and whether the PR's history is
  complete. Version 1 files still load; their setups and tokens show as inferred.

GitHub's own records (Models design, Phase 1; `pr-facts.json`, written by `collectors/prfacts.py`):

- **CI receipts.** Every head of a PR, with every check attempt (not only the latest), ordered by GitHub's check run
  id (so an attempt cancelled before it started still comes first), under main's required checks as its branch
  protection and rulesets say (read daily, kept only when GitHub returned them whole; a ruleset's app is matched by
  its id); with none, every job but the review gate counts ("tracked"). A required name obliges both its check runs
  and a commit status with that name: a passing run can't hide a failing status. When the rule names an app, a
  status counts only with evidence it came from that app (GitHub's isRequired for the PR, or the app's bot as its
  creator); one from another source doesn't count, and one with no evidence leaves the check unknown. SUCCESS is
  the only pass; cancelled, queued, skipped, stale or missing jobs leave a head unknown with the reason. No mode
  passes on history GitHub didn't return explicitly whole (a missing connection isn't an empty one, an answer with
  an error isn't stored), on attempts with no known order, or when main's rules couldn't be read. A required commit
  status keeps only its latest state, so it can decide the latest outcome but never the first attempt. A head whose
  last read failed keeps its older history, dated, and its PR stays incomplete until a read succeeds. First
  attempt, first completed and latest are kept apart.
- **The first-ready commit** (r0 in the design and the files): verified only from the repository's `opened` event
  naming the head of a PR that opened ready. Push events that the feed covers without a gap make it *from push
  events*, never verified. Otherwise the first PR workflow run on the PR's branch after opening is a *candidate*,
  shown with its event and delay. A draft never made ready has none; a PR whose timeline wasn't fully read has none
  either (its first ready-for-review might be the page missing).
- **The Models tab** shows a coverage line (PRs first ready in the window; first-ready commits by evidence; heads with
  full CI history) that says when it's partial (PRs not fetched yet or changed since, discovery not finished), when
  the events feed had gaps, and when the receipts are old or the last refresh failed. The commit-trailer card
  is labelled as attribution that is not model evidence, with fitness "Do not use": Co-Authored-By labels are PR counts
  over one denominator, in name order, with no outcome beside them. First try is told once for all PRs, by evidence, as
  same-head CI with its own fitness label. No trailer feeds a rate, a ranking or a gate. The CI gates in Best / Needs attention count verified first-ready commits, decided first
  tries and exact producers (none until commits are tied to runs).
- **The PR card** has CI lines (the first-ready commit, its evidence and first try; the first decided work head; the
  policy when main's rules weren't read or require nothing; how old an open PR's history is; what its evidence
  lacks) and a heads strip in observation order: ● ✓ passed, ● ✗ failed, ○ no result, ◇ merge from main, ⇡ exact
  push time, the first-ready commit underlined, force-pushed heads struck through, ┊ over an hour, ◆ merged.
- **Verify:** `python3 collectors/verify.py --offline [PR …]` says what it checked, replays the stored receipts
  through the same rules, and prints every PR's numbers and the window's Models numbers. It separates "reproduced"
  from "inputs complete". Exit 0 verified (reproduced from complete inputs); 1 a contradiction (the same inputs give
  a different record); 2 incomplete: no readable publication, nothing in scope, inputs missing, newer than the
  publication or not complete (including a head whose last read failed), or (for every PR) PRs still waiting to be
  fetched, head histories due but held by the budget, or errors in the run that published. `--refresh PR …` fetches up to 10
  PRs again first, within the shared budget.

## Data sources and refresh

| Collector | Source | Every |
|---|---|---|
| host collectors | site-specific and not part of this repository: the app runs `collectors/rig.py` (CI host and agents VM over SSH), `collectors/vm.py` (VM-side snapshot) and `collectors/mac.py` (local CI lanes, GitHub and Codex budgets) when they exist, and shows their sources as never seen otherwise. Their JSON shapes are the ones `Ingest.swift` decodes. | 20–30 s |
| `collectors/linear.py` | Linear GraphQL API: every issue's state and project, initiatives, the launch (cached 30 min) | 15 min |
| `collectors/repos.py` | one GitHub GraphQL query for all three repos: PR checks (latest attempt per check), review gate, merge state, queued CI jobs (cached 15 min; not the REST budget the CI hosts use), plus GitHub `rate_limit` on every run, so the budget needs no Mac | 15 min |
| `collectors/prfacts.py` | GitHub receipts for the Models tab and PR cards: the repository events feed (`ghevents.py`: `If-None-Match`, so a quiet poll is a free 304; at most 36 requests an hour, each counted before it's sent and refunded only for a confirmed 304; receipts on disk before the state that acknowledges them; a gap is recorded when more events arrive between polls than the pages read), main's required checks (daily), PR facts and full check history (`ghfacts.py`: discovery every 15 min; an open PR again when it changed, with every head's history; running, empty or incomplete histories every 15 min, the current head hourly; a merged PR once, sealed only when everything came back whole; at most 100 GraphQL points an hour, each query reserving its documented cost before it's sent, under a lock shared with `verify.py --refresh`, with older ledger lines and a line cut short still charged; a run held by the budget doesn't count as a GitHub read, and the head histories it held are reported), then `receipts.py` writes `pr-facts.json`. Stored in `~/Library/Application Support/FlightOps/github-events` and `github-facts`; commit messages keep only their headline and trailers | 5 min |

The Models tab and PR card show the VM's `models` snapshot fact while it is current.

On the VM, `collectors/ledger.py` keeps CI evidence and branch history in one SQLite file. Nothing reads it yet: see `docs/evidence-ledger.md`.
Otherwise they fall back to `pr-models.json`, the historical report from the retired `pr-models.py`, which is read
again at each GitHub refresh. `agent-runs.json` is retired. `pr-facts.json` is read after each `prfacts.py` run.

GitHub cost, measured on successful calls (2026-10-25), as planning figures. The caps above are the bounds.
- The events feed's first poll reads 3 pages per repo (9 counted calls); the polls measured afterwards took 0–1
  counted call per repo, and a busy stretch can need 2–3.
- Discovery is 1 GraphQL point per 100 PRs, main's rules 1 point a day, facts about 1 point per 10 PRs, history about
  5 points per 30 heads.
- Backfilling 28 days takes about 600 points at today's volume: 6 hours at the cap at best. Reservations, the hourly
  re-reads of open PRs' current heads and retries make it longer; open PRs and the newest come first.
- GitHub may charge more than its stated cost for a query it times out; the collector keeps such a query's
  reservation but can't see any extra charge.

The host collectors depend on your own infrastructure (CI watcher heartbeats, an orchestrator API token);
nothing in this repository assumes a particular host.

Optional: `defaults write io.github.jammin8.flightops orchThreadURL 'https://<orch host>/thread/{id}'` makes agent rows and
notifications open the thread; without it they copy the thread id.

## Configuration

Site-specific names live in a local JSON file that is never committed. The collectors read, in order,
`$FLIGHTOPS_CONFIG`, `flightops.local.json` at the repository root (gitignored), then
`~/.config/flightops/config.json`; every key is optional and falls back to `config.example.json`. Tests ignore all of these and
use `tests/fixtures/site-config.json`.

The UI says "CI host" for the machine that runs self-hosted CI and "Orchestrator" for the agent orchestrator whose
shell and thread API the Models and Agents views read. Internally, and in the JSON the collectors exchange, these
are still `rig` and `orch` (source ids, check ids and keys such as `rig_status` or `vm.orch`), so existing
collectors and readers of the published status keep working.

## Headless CLI collection

Build the CLI with `scripts/build-cli.sh`. On macOS and Linux, `flightops collect` enables only the
GitHub collector, `repos`. Configure your GitHub repositories in the local configuration and authenticate
GitHub CLI before collecting. The default does not contact a machine host, orchestrator, relay or issue
tracker. Optional scripts are never enabled merely because they exist.

Choose optional integrations explicitly with `FLIGHTOPS_COLLECTORS_ENABLED`, a comma-separated list that
replaces the default. For example, `FLIGHTOPS_COLLECTORS_ENABLED=repos,linear flightops collect` adds Linear;
`FLIGHTOPS_COLLECTORS_ENABLED=repos,ledger flightops collect --publish` also records GitHub evidence and
publishes a local snapshot. Configure each selected source before enabling it. `pull`, `models` and `best`
require their own supported integration and retained inputs. The `rig`, `vm` and `mac` host adapters are
site-specific scripts absent from this public source tree; selecting one without supplying it reports
`collector missing`.

`flightops status --json` labels an omitted source with no retained input as
`collector disabled (not configured)`. Its readings remain unknown. Retained inputs keep their original
clocks and existing freshness rules. An optional source failure leaves independent GitHub facts usable.
These defaults apply to the headless CLI; native app collection and onboarding remain separate follow-up
work.

## Connect Linear

Create a personal API key in Linear (Settings → Account → Security & access → Personal API keys, read
access is enough) and store it in the login keychain; the command prompts for it without echoing:

```
security add-generic-password -U -s flightops-linear -a linear -w
```

## Build and install

```
scripts/install.sh
```

It runs the collector tests and the logic checks, builds a release binary, assembles `FlightOps.app`
(binary, collectors, icon), signs it ad hoc, installs it to `~/Applications` and starts it. Use the panel's
"Open at Login" item to start it at login. Logs go to the unified log:
`log stream --predicate 'subsystem == "io.github.jammin8.flightops"'`.

Tests:

- `python3 -m unittest discover -s tests` — the collectors. `tests/fixtures/pr-facts.json` and its summary are shared
  with the Swift checks (regenerate with `python3 tests/test_receipts.py --write`).
- `scripts/check-logic.sh` — the app's rules without UI (`Models.swift`, `Logic.swift`: lenient decoding,
  health, needs, the notification engine); needs only a Swift toolchain, so it also runs on Linux.
- `FlightOps --snapshot <dir>` — renders every tab in light and dark to PNGs (its own monitor; never
  notifies or changes the running app's state: it keeps its panel state in its own defaults suite).

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in the work by you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or conditions.
