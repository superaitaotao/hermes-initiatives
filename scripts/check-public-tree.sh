#!/usr/bin/env bash
# Pre-publish privacy gate over every tracked file (this repo is public).
# Fails on a token-shaped string, an absolute home path, or one of YOUR private identifiers.
#
#   PRIVATE_PATTERNS='name|project-slug|chat-id|topic-id' scripts/check-public-tree.sh
#
# PRIVATE_PATTERNS is an extended regex passed in at run time, never committed, so the check itself leaks nothing.
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

files=$(git ls-files | grep -vE '^scripts/check-public-tree\.sh$' || true)
status=0

scan() {  # label, regex, [grep flags]
  local hits
  if hits=$(printf '%s\n' "$files" | xargs -r grep -nIE ${3:-} -- "$2" 2>/dev/null); then
    echo "$1:"; echo "$hits" | cut -c1-160; status=1
  fi
}
# Token shapes. Each needs its vendor prefix or structure, so ordinary words like task-20261018-partial don't trip it.
scan "token-shaped strings" '(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|\bsk-(ant-|proj-)?[A-Za-z0-9_-]{32,}|\bAKIA[0-9A-Z]{16}\b|\bxox[bpa]-[A-Za-z0-9-]{10,}|\b[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|op://[A-Za-z0-9]|AIza[0-9A-Za-z_-]{35})'
scan "absolute home paths" '/(home|Users)/[a-z][a-z0-9_-]*/'
if [ -n "${PRIVATE_PATTERNS:-}" ]; then
  scan "private identifiers" "$PRIVATE_PATTERNS" -i
else
  echo "note: PRIVATE_PATTERNS unset; only token shapes and home paths were checked" >&2
fi
[ "$status" -eq 0 ] && echo "public tree check: clean ($(printf '%s\n' "$files" | grep -c .) files)"
exit "$status"
