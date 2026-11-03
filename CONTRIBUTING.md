# Contributing

## Keep private details out

This repository is public. Before every commit:

```sh
scripts/leak-check.sh --staged
```

It runs Gitleaks (if installed) plus checks for private network addresses, home-directory paths and any private words you list in a local `.leak-denylist` (copy `.leak-denylist.example`; the real file is gitignored). CI runs Gitleaks on every push and pull request.

Machine names, hostnames, accounts, tokens and paths belong in local, gitignored configuration, never in code, tests, fixtures or docs.

To check a directory before importing it:

```sh
scripts/leak-check.sh --tree path/to/dir
```

## Pull requests

- One focused change per pull request, squash-merged.
- No explanatory comments or docstrings in new or changed code; names should carry the meaning.

## License

Contributions are dual licensed under MIT OR Apache-2.0, as described in the README.
