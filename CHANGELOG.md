# Changelog

## Unreleased

### Development

- Run the Python suite on Linux and the native Swift build, CLI checks and logic checks on macOS for pull requests and main-branch pushes. Workflows use fixture configuration and read-only repository permissions.
- Verify frozen comparison code by its complete file inventory and content hashes without requiring private commit history. Modified, additional and removed kernel files fail the guard; historical policy replay remains pinned.
- Build the ledger-error test fixture with one `errors` field so JSON duplicate-key behavior cannot hide the expected collector failure.

These changes establish development checks for the source preview; they do not publish a product release or establish runtime acceptance.
