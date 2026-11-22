# FlightOps

A status view of your work across your GitHub repos, in the macOS menu bar.

FlightOps shows what is moving, what is stuck and what needs you, for one repo or all of them. It reads GitHub through the GitHub CLI you already use. It shows everything and changes nothing.

## What you see

One picker at the top filters every tab: **All repos**, or one repo.

| Tab | Shows |
| --- | --- |
| **Now** | One status line. Open, merged today and needs you, with 14-day sparklines. A stage strip: Draft, Fixing, Waiting for CI, In CI, In review, Approved, Merged today, each with its count and how long the oldest has waited. A bottleneck card when CI or review is Slowing or Stuck, or a wave is flowing through. The PRs that need you. Every open PR, grouped by stage, with what it is waiting on. |
| **Trends** | 7, 30 or 90 days: a stage health map (each stage's worst verdict per day), PRs opened and merged per day, time to merge, how long the slowest tenth of CI jobs waited for a runner, open PRs at the end of each day, merged per repo, and past bottlenecks ranked by hours of extra waiting. |

The menu bar shows the number of open PRs and a dot when something needs you.

Rules it keeps:

- Unknown is never zero. A repo that couldn't be read shows "–" and the total says it is partial.
- Every tab reads the same file, so they always agree.
- Charts come from stored history, so they have no holes for days the app was closed.
- A bottleneck is judged against the stage's own last 28 days, on the slow end of waits, so a big wave that keeps flowing reads as Busy, not Stuck. See [docs/status-file.md](docs/status-file.md#bottlenecks).

## Get started

You need a Mac on macOS 14 or later, Apple's command line tools (`xcode-select --install`, which brings Python 3 and Swift) and the [GitHub CLI](https://cli.github.com) signed in (`gh auth login`).

```
scripts/install.sh
```

It runs the tests, builds the app, installs it to `~/Applications` and opens it. The first time, FlightOps shows sample data and a **Connect your repos** button. Pick your repos and your own board fills in within a minute. 90 days of history and CI detail fill in behind it over the next hour or so.

## How it works

```
GitHub (through gh)  ──▶  collector  ──▶  SQLite store  ──▶  status.json  ──▶  app
                                                                 └──────▶  terminal
```

The app runs the collector every two minutes. The collector reads GitHub, keeps history in one SQLite file, works out stages, waits and trends, and writes one JSON status file. The app only draws that file.

From a terminal:

```
python3 collectors/flightops.py use acme/api acme/web
python3 collectors/flightops.py collect
python3 collectors/flightops.py status
```

[docs/status-file.md](docs/status-file.md) covers the config, what each pass reads, the store and the status file format. `collectors/sample-status.json` is a complete example; it drives demo mode, the tests and the screenshots.

## Config

`~/.config/flightops/config.json`. The app writes it when you pick repos; you can also edit it.

```json
{
  "repos": ["acme/api", {"name": "acme/web", "color": "#4682B4"}],
  "approval_check": "review-gate"
}
```

`approval_check` is optional: a check whose success counts as approval. Without it, approval is GitHub's review decision.

## Development

- `python3 -m unittest discover -s tests`: the collector.
- `python3 tests/make_sample.py`: rebuild the sample status file after changing the collector.
- `swift build && .build/debug/FlightOps --snapshot /tmp/shots`: render every tab in light and dark from the sample file. Add `--status FILE` to render another status file.

CI runs the Python tests on Linux and macOS, runs the collector against this repository, builds the app and renders every tab on GitHub's macOS runners. The snapshots are attached to each run.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in the work by you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or conditions.
