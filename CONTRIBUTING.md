# Contributing

## Keep private details out

This repository is public. Before every commit:

```sh
scripts/leak-check.sh --staged
```

Install Gitleaks before running the gate; a missing or failed scanner blocks the check. The gate checks exact staged Git content and filenames, plus private network addresses, home-directory paths and any private words you list in a local `.leak-denylist` (copy `.leak-denylist.example`; the real file is gitignored). Denylist entries are case-insensitive Python regular expressions. Secret rules retain the staged files' original paths. Matches and tool output are withheld so diagnostics cannot disclose private data. CI separately runs Gitleaks on pushes and pull requests.

Machine names, hostnames, accounts, tokens and paths belong in local, gitignored configuration, never in code, tests, fixtures or docs.

To check a directory before importing it:

```sh
scripts/leak-check.sh --tree path/to/dir
```

Tree checks inspect ordinary UTF-8 files. Unsupported binaries and archives, symbolic links, special files, unreadable inputs and oversized inventories fail visibly. Current limits are 10,000 files, 16 MiB per file and 64 MiB total. The existing icon therefore prevents a full-tree clean result until binary inspection is separately qualified. Do not treat a refused scan as approval or exclude an artifact merely to pass the gate.

Keep the Gitleaks configuration reviewed. These checks supplement manual inspection of the exact diff, commit metadata and PR text; they cannot identify every personal detail. New artifact formats require a qualified privacy check before publication.

## Pull requests

- One focused change per pull request, squash-merged.
- No explanatory comments or docstrings in new or changed code; names should carry the meaning.

## License

Contributions are dual licensed under MIT OR Apache-2.0, as described in the README.
