#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: scripts/leak-check.sh [--staged | --tree DIR]" >&2
  exit 2
}

mode=repo
target=
case "${1:-}" in
  "") ;;
  --staged) mode=staged ;;
  --tree) [ $# -eq 2 ] || usage; mode=tree; target=$2 ;;
  *) usage ;;
esac
if [ "$mode" != tree ]; then
  target=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "leak-check: not inside a git checkout; use --tree DIR" >&2; exit 2; }
fi

root=$(cd "$(dirname "$0")/.." && pwd)
config="$root/.gitleaks.toml"
denylist="${FLIGHTOPS_DENYLIST:-$root/.leak-denylist}"
status=0

if command -v gitleaks >/dev/null 2>&1; then
  case $mode in
    repo) gitleaks git --config "$config" --redact --no-banner "$target" || status=1 ;;
    staged) gitleaks git --pre-commit --staged --config "$config" --redact --no-banner "$target" || status=1 ;;
    tree) gitleaks dir --config "$config" --redact --no-banner "$target" || status=1 ;;
  esac
else
  echo "leak-check: gitleaks not installed; running pattern checks only" >&2
fi

files() {
  case $mode in
    repo) git -C "$target" ls-files -z ;;
    staged) git -C "$target" diff --cached --name-only --diff-filter=ACMR -z ;;
    tree) (cd "$target" && find . -type f -not -path './.git/*' -print0) ;;
  esac
}

patterns=(
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '/(Users|home)/[A-Za-z0-9._-]+/'
  '\b[A-Za-z0-9-]+\.ts\.net\b'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)

if [ -f "$denylist" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in ''|'#'*) continue ;; esac
    patterns+=("$line")
  done < "$denylist"
fi

content() {
  if [ "$mode" = staged ]; then
    git -C "$target" show ":$1"
  else
    cat "$target/$1"
  fi
}

listed=$(mktemp)
trap 'rm -f "$listed"' EXIT
files > "$listed" || { echo "leak-check: could not list files to check" >&2; exit 2; }

hits=0
while IFS= read -r -d '' f; do
  [ "$mode" = staged ] || [ -f "$target/$f" ] || continue
  case $f in ./.leak-denylist|.leak-denylist|./scripts/leak-check.sh|scripts/leak-check.sh|./.gitleaks.toml|.gitleaks.toml) continue ;; esac
  for p in "${patterns[@]}"; do
    if content "$f" 2>/dev/null | grep -IEni -e "$p" >/dev/null; then
      content "$f" | grep -IEni -e "$p" | cut -d: -f1 | sed "s|^|leak-check: $f:|; s|\$|: matches a private pattern|"
      hits=$((hits + 1))
    fi
  done
done < "$listed"

if [ "$hits" -gt 0 ] || [ "$status" -ne 0 ]; then
  echo "leak-check: FAILED" >&2
  exit 1
fi
echo "leak-check: clean"
