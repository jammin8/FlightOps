# FlightOps

A mission control view for vibe coding, CI, and agents.

> **Being rebuilt.** FlightOps is becoming a simple status view of your work across your GitHub repos and machines: Now, Trends and Models tabs, one repo filter, one collector and one status file. The model comparison ("Best"), the launch forecast, the MCP server and the headless CLI have been removed. The app described below still builds while the new one lands.

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

## Main branches

Each repo's main is failing (a check failed, even while others run), passing, running, cancelled or unknown
(no check state reported: shown with a question mark and counted as a gap). Only passing recovers a failure.
A repo that runs no checks on main by design can say so in `~/.config/flightops/repos.json`:

```json
{"main_without_checks": ["ios"]}
```

Its check-less main is then "no checks by design" (neutral, not a gap, not recovery evidence).

## Data sources and refresh

| Collector | Source | Every |
|---|---|---|
| host collectors | site-specific: the app runs `collectors/rig.py` (CI host and agents VM), `collectors/vm.py` (VM-side snapshot) and `collectors/mac.py` (local CI lanes, GitHub and Codex budgets) when they exist, and shows their sources as never seen otherwise. Stand-ins live in `collectors/examples` (see *Host collectors*). Their JSON shapes are the ones `Ingest.swift` decodes. | 20–30 s |
| `collectors/repos.py` | one GitHub GraphQL query for all three repos: PR checks (latest attempt per check), review gate, merge state, queued CI jobs (cached 15 min; not the REST budget the CI hosts use), plus GitHub `rate_limit` on every run, so the budget needs no Mac | 15 min |

The host collectors depend on your own infrastructure (CI watcher heartbeats, an orchestrator API token);
nothing in this repository assumes a particular host.

Optional: `defaults write io.github.jammin8.flightops orchThreadURL 'https://<orch host>/thread/{id}'` makes agent rows and
notifications open the thread; without it they copy the thread id.

## Host collectors

The real host collectors depend on your own machines, so they are not in this repository. `collectors/examples`
has stand-ins with the same JSON shapes that report only what they can measure on the machine they run on and
leave everything else unknown:

| Stand-in | Reports | Leaves unknown |
|---|---|---|
| `rig.py` | this machine as the CI host: CPU, memory, load, uptime, disk | temperature, fan, swap, network, CI guests and lanes, the agents VM |
| `vm.py` | this machine as the agents VM: memory, load, CPUs, disk | the orchestrator, its token, agent sessions and the agent list |
| `mac.py` | the GitHub API budget through `gh api rate_limit` | local CI jobs, Codex usage, agent counts |

To use them, copy them next to the other collectors (the copies are gitignored, so a site-specific version never
gets committed by accident) and enable the ones you want:

```
cp collectors/examples/{machine,rig,vm,mac}.py collectors/
FLIGHTOPS_COLLECTORS_ENABLED=repos,rig,vm,mac flightops collect
```

Replace a stand-in with your own script when you have a real source; keep the same output shape and leave
anything you can't read out (or `null`) rather than reporting zero.

## Configuration

Site-specific names live in a local JSON file that is never committed. The collectors read, in order,
`$FLIGHTOPS_CONFIG`, `flightops.local.json` at the repository root (gitignored), then
`~/.config/flightops/config.json`; every key is optional and falls back to `config.example.json`. Tests ignore all of these and
use `tests/fixtures/site-config.json`.

The UI says "CI host" for the machine that runs self-hosted CI and "Orchestrator" for the agent orchestrator whose
shell and thread API the Models and Agents views read. Internally, and in the JSON the collectors exchange, these
are still `rig` and `orch` (source ids, check ids and keys such as `rig_status` or `vm.orch`), so existing
collectors and readers of the published status keep working.

## Build and install

```
scripts/install.sh
```

It runs the collector tests and the logic checks, builds a release binary, assembles `FlightOps.app`
(binary, collectors, icon), signs it ad hoc, installs it to `~/Applications` and starts it. Use the panel's
"Open at Login" item to start it at login. Logs go to the unified log:
`log stream --predicate 'subsystem == "io.github.jammin8.flightops"'`.

Tests:

- `python3 -m unittest discover -s tests` — the collectors.
- `FlightOps --snapshot <dir>` — renders every tab in light and dark to PNGs (its own monitor; never
  notifies or changes the running app's state: it keeps its panel state in its own defaults suite).

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in the work by you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or conditions.
