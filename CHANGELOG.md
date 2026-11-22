# Changelog

## Unreleased

### Bottlenecks

- Waiting for CI, In CI and In review each get a verdict against their own 28-day normal: Clear, Busy (a wave that is flowing), Slowing or Stuck. Now shows a card naming the stage and its oldest PR, Trends ranks past slowdowns by hours of extra waiting, and the app sends one notification when a stage turns Stuck and one when it clears.
- A simpler, richer panel: Now opens with open, merged today and needs you, each with a sparkline. Trends adds a stage health map, time to merge and merged per repo. The Models tab and the Machines card are hidden until their data is connected.
- `flightops.py replay FROM TO` prints the verdicts day by day from stored history.

### The new app

- The app is rebuilt to draw only the status file: Now, Trends and Models tabs, one repo filter, and a first-run repo picker that starts on sample data.
- One collector (`collectors/flightops.py`) replaces the repos, ledger and pull collectors and the host collector stand-ins. Repos come from an `owner/name` list, each repo's default branch is read from GitHub, and approval is GitHub's review decision unless a check is named.
- The old app, its alerts, launch forecast, lane timers, budgets and the Best view are gone.

### Removed

- The model comparison: `best.py`, `model_evidence`, `model_analysis` and the GitHub receipts collectors (`receipts.py`, `prfacts.py`, `ghfacts.py`, `ghevents.py`, `verify.py`). It never produced an answer.
- The Linear collector, the orchestrator and model collectors (`linear.py`, `orch_api.py`, `models.py`, `runs.py`, `failures.py`).
- The headless CLI and its MCP server (`cli/`, `build-cli.sh`, `check-cli.sh`). The new collector will print the status file itself.
- The ledger's required-checks reads, which only the comparison used.
- The Swift logic and decision checks, which test the app being replaced.

### Fixed

- A truncated CI listing no longer keeps its first page's ETag, so the next read can't close the truncation gap without reading the rest.
- A PR inventory page with a missing or repeated cursor keeps its read-failed gap open.
- `install.sh` creates `~/Applications` when it doesn't exist.

### Privacy checks

- Check exact staged Git blobs and filenames, so changing or deleting the working copy cannot conceal private staged content. Preserve file paths for secret rules that depend on their location.
- Require Gitleaks and refuse unavailable tools, unreadable inputs, links, unsupported binary or archive formats and inputs beyond bounded scan limits. Scanner output never prints matched content or filenames.
- Add synthetic regressions, including real Gitleaks integration, for index/worktree divergence, path-sensitive rules, safe diagnostics and nonregular files. Source checks do not establish privacy of signed binaries or other unqualified artifacts.

### Collection

- Enable only GitHub repository collection by default in the headless CLI on macOS and Linux. Host, orchestrator, issue-tracker, comparison and ledger integrations require explicit selection; absent private adapters no longer cause default collection failures.
- Label omitted sources without retained inputs as disabled and unknown. Explicitly enabled missing adapters still fail visibly, while independent GitHub facts remain available. Retained inputs keep their original clocks and freshness rules.
- Verify these defaults with optional-adapter sentinels and missing-source checks in the CLI suite. Native app onboarding remains separate work.

### Comparison publication

- Encode oversized or whitespace-containing source-thread independence identities with stable, domain-separated hashes. Shared-thread PRs remain correlated; valid short keys and the comparison kernel's existing limits are unchanged.
- Return fixed diagnostic categories for contract, storage and malformed-input refusals without including arbitrary exception text, source values or filesystem paths.
- Add synthetic regressions for key boundaries, correlation, original source clocks and retained last-good publication. This repairs a producer/contract mismatch; it does not qualify evidence, establish a winner or claim deployed acceptance.

### Development

- Run the Python suite on Linux and the native Swift build, CLI checks and logic checks on macOS for pull requests and main-branch pushes. Workflows use fixture configuration and read-only repository permissions.
- Verify frozen comparison code by its complete file inventory and content hashes without requiring private commit history. Modified, additional and removed kernel files fail the guard; historical policy replay remains pinned.
- Build the ledger-error test fixture with one `errors` field so JSON duplicate-key behavior cannot hide the expected collector failure.

These changes establish development checks for the source preview; they do not publish a product release or establish runtime acceptance.
