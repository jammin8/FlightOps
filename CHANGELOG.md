# Changelog

## Unreleased

### Comparison publication

- Encode oversized or whitespace-containing source-thread independence identities with stable, domain-separated hashes. Shared-thread PRs remain correlated; valid short keys and the comparison kernel's existing limits are unchanged.
- Return fixed diagnostic categories for contract, storage and malformed-input refusals without including arbitrary exception text, source values or filesystem paths.
- Add synthetic regressions for key boundaries, correlation, original source clocks and retained last-good publication. This repairs a producer/contract mismatch; it does not qualify evidence, establish a winner or claim deployed acceptance.

### Development

- Run the Python suite on Linux and the native Swift build, CLI checks and logic checks on macOS for pull requests and main-branch pushes. Workflows use fixture configuration and read-only repository permissions.
- Verify frozen comparison code by its complete file inventory and content hashes without requiring private commit history. Modified, additional and removed kernel files fail the guard; historical policy replay remains pinned.
- Build the ledger-error test fixture with one `errors` field so JSON duplicate-key behavior cannot hide the expected collector failure.

These changes establish development checks for the source preview; they do not publish a product release or establish runtime acceptance.
