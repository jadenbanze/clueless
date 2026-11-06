#!/usr/bin/env bash
# Fails if any given file contains a term from .denylist (case-insensitive).
#
# .denylist is untracked on purpose: listing your employer's internal names in a
# public repo would leak exactly what this hook exists to keep out. One term per
# line; blank lines and lines starting with # are ignored.
#
# Usage: scripts/denylist-check.sh [files...]   (no files = every tracked file)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
DENYLIST="${DENYLIST:-.denylist}"
[ -f "$DENYLIST" ] || exit 0

patterns="$(mktemp)"
trap 'rm -f "$patterns"' EXIT
grep -vE '^\s*(#|$)' "$DENYLIST" > "$patterns" || true
[ -s "$patterns" ] || exit 0

if [ "$#" -eq 0 ]; then
    set -- $(git ls-files)
fi

status=0
for file in "$@"; do
    [ -f "$file" ] || continue
    case "$file" in
        "$DENYLIST"|*.icns|*.png|*.jpg|*.dmg|*.zip) continue ;;
    esac
    if matches="$(grep -inF -f "$patterns" -- "$file")"; then
        # Show where, not what: the matched text is the sensitive part.
        while IFS=: read -r line _; do
            echo "denylist: $file:$line contains a denylisted term"
        done <<< "$matches"
        status=1
    fi
done
exit $status
