#!/usr/bin/env bash
# Publishes the committed tree as a brand-new repository with a single commit,
# so none of this repository's history (and nothing ever removed from it)
# leaves this machine.
#
#   scripts/publish-oss.sh --dry-run              scan + export only; prints the file list
#   scripts/publish-oss.sh OWNER/NAME             create OWNER/NAME (public) and push
#   scripts/publish-oss.sh --private OWNER/NAME   same, but private, and no release is cut
#
# Options:
#   --private                create a private repository; skips the GitHub release
#                            and the Build release run (do that at public launch)
#   --author "Name <email>"  commit identity (default: your GitHub noreply address,
#                            so a work email never lands in public metadata)
#   --yes                    skip the confirmation prompt
#
# What it refuses to do:
#   - publish with uncommitted changes (git archive exports HEAD, so they would
#     be silently left out);
#   - publish without a .denylist (the scan that catches employer-specific names);
#   - publish over this repository's own remote, or into a repository that
#     already exists.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
ROOT="$(pwd)"

fail() { echo "publish-oss: $*" >&2; exit 1; }

DRY_RUN=0
YES=0
VISIBILITY=public
TARGET=""
AUTHOR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes) YES=1 ;;
        --private) VISIBILITY=private ;;
        --author) AUTHOR="${2:?--author needs a value}"; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        -*) fail "unknown option $1" ;;
        *) TARGET="$1" ;;
    esac
    shift
done
[ "$DRY_RUN" = 1 ] || [ -n "$TARGET" ] || fail "give OWNER/NAME, or --dry-run"
if [ -n "$TARGET" ] && ! [[ "$TARGET" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]]; then
    fail "target must look like OWNER/NAME"
fi

VERSION="$(node -p "require('./overlay/package.json').version")"
EXPORT="$(mktemp -d)/clueless"
mkdir -p "$EXPORT"

# ---------------------------------------------------------------- export
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    if [ "$DRY_RUN" = 1 ]; then
        echo "==> working tree has uncommitted changes; dry run previews tracked + new files"
        git ls-files -z --cached --others --exclude-standard \
            | while IFS= read -r -d '' f; do [ -e "$f" ] && printf '%s\0' "$f"; done \
            | rsync -a --from0 --files-from=- ./ "$EXPORT/"
    else
        git status --short | head -20 >&2
        fail "commit or stash your changes first: only HEAD is published"
    fi
else
    echo "==> exporting HEAD ($(git rev-parse --short HEAD))"
    git archive --format=tar HEAD | tar -x -C "$EXPORT"
fi

# ---------------------------------------------------------------- scans
echo "==> scanning $(find "$EXPORT" -type f | wc -l | tr -d ' ') files"
problems=0

# Files that must never be public, whatever their contents.
while IFS= read -r f; do
    echo "  forbidden file: ${f#"$EXPORT"/}"; problems=1
done < <(find "$EXPORT" -type f \( -name '.env' -o -name '.env.*' ! -name '.env.example' \
    -o -name '*.p12' -o -name '*.p8' -o -name '*.pem' -o -name '*.key' -o -name '.denylist' \
    -o -name 'AGENTS.md' -o -name 'PLAN.md' -o -name 'settings.json' -o -name 'api-token' \))

while IFS= read -r f; do
    echo "  file over 5 MB: ${f#"$EXPORT"/}"; problems=1
done < <(find "$EXPORT" -type f -size +5M)

if [ -f "$ROOT/.denylist" ]; then
    files=()
    while IFS= read -r -d '' f; do files+=("$f"); done < <(find "$EXPORT" -type f -print0)
    if ! scripts/denylist-check.sh "${files[@]}" | sed "s|$EXPORT/||"; then problems=1; fi
else
    echo "  no .denylist: create one (one organisation-specific term per line) before publishing"
    [ "$DRY_RUN" = 1 ] || problems=1
fi

if command -v gitleaks >/dev/null; then
    gitleaks dir --no-banner --redact --config "$ROOT/.gitleaks.toml" "$EXPORT" || problems=1
elif command -v pre-commit >/dev/null; then
    # Same pinned gitleaks as the commit hook, run over the export.
    (cd "$EXPORT" && git init -q && git add -A \
        && pre-commit run gitleaks --all-files --config "$ROOT/.pre-commit-config.yaml") || problems=1
    rm -rf "$EXPORT/.git"
else
    echo "  neither gitleaks nor pre-commit is installed; cannot scan for secrets"; problems=1
fi

[ "$problems" = 0 ] || fail "scan failed; nothing was published"
echo "    clean"

if [ "$DRY_RUN" = 1 ]; then
    echo
    echo "==> dry run: $EXPORT"
    (cd "$EXPORT" && find . -type f | sed 's|^\./||' | sort)
    exit 0
fi

# ---------------------------------------------------------------- publish
command -v gh >/dev/null || fail "gh is required"
if git remote get-url origin 2>/dev/null | grep -qiE "github\.com[:/]${TARGET}(\.git)?$"; then
    fail "$TARGET is this repository's own remote; rename it first (e.g. gh repo rename clueless-private)"
fi
# GitHub keeps resolving a renamed repository's old name through a redirect, so
# compare the canonical name: a redirect alone does not make the name taken.
existing="$(gh api "repos/$TARGET" --jq .full_name 2>/dev/null || true)"
if [ -n "$existing" ] && [ "$(echo "$existing" | tr '[:upper:]' '[:lower:]')" = "$(echo "$TARGET" | tr '[:upper:]' '[:lower:]')" ]; then
    fail "$TARGET already exists; choose a new name"
fi

if [ -z "$AUTHOR" ]; then
    login="$(gh api user --jq .login)"
    id="$(gh api user --jq .id)"
    AUTHOR="$(git config user.name) <${id}+${login}@users.noreply.github.com>"
fi
NAME="${AUTHOR% <*}"
EMAIL="${AUTHOR##*<}"; EMAIL="${EMAIL%>}"

if [ "$VISIBILITY" = public ]; then
    RELEASE_LINE="  tag:        v$VERSION, with a GitHub Release built by CI"
    WARNING="This cannot be undone by deleting files later: anything pushed is public."
else
    RELEASE_LINE="  tag:        v$VERSION (no release yet)"
    WARNING="Private: only you and the collaborators you add can see it."
fi
cat <<EOF

About to create a $(echo "$VISIBILITY" | tr '[:lower:]' '[:upper:]') repository:
  repository: https://github.com/$TARGET
  contents:   $(find "$EXPORT" -type f | wc -l | tr -d ' ') files from $(git rev-parse --short HEAD), one commit
  author:     $NAME <$EMAIL>
$RELEASE_LINE
$WARNING
EOF
if [ "$YES" != 1 ]; then
    read -r -p "Type the repository name to continue: " answer
    [ "$answer" = "$TARGET" ] || fail "aborted"
fi

TODAY="$(date +%Y-%m-%d)"
sed -i '' "s/^## ${VERSION} (unreleased)$/## ${VERSION} (${TODAY})/" "$EXPORT/CHANGELOG.md"

cd "$EXPORT"
git init -q -b main
git add -A
GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" \
    git commit -q -m "feat: initial release"
git tag -a "v$VERSION" -m "v$VERSION"

echo "==> creating $TARGET ($VISIBILITY)"
gh repo create "$TARGET" "--$VISIBILITY" --source . --remote origin \
    --description "Local-only meeting assistant for macOS: on-device transcription and answers on a hotkey" \
    --push
git push -q origin "v$VERSION"

# Private vulnerability reporting is what SECURITY.md tells people to use, and
# release-please needs Actions to be allowed to open pull requests.
gh api -X PUT "repos/$TARGET/private-vulnerability-reporting" >/dev/null || echo "    warning: enable private vulnerability reporting by hand"
gh api -X PUT "repos/$TARGET/actions/permissions/workflow" \
    -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true >/dev/null \
    || echo "    warning: allow Actions to create pull requests by hand (Settings → Actions → General)"

if [ "$VISIBILITY" = public ]; then
    notes="$(awk -v v="## ${VERSION} " 'index($0, v) == 1 {on=1; next} /^## / && on {exit} on' CHANGELOG.md)"
    gh release create "v$VERSION" --repo "$TARGET" --title "Clueless $VERSION" --notes "$notes"
    gh workflow run build-release.yml --repo "$TARGET" -f tag="v$VERSION" \
        || echo "    warning: start Actions → Build release for v$VERSION by hand"
fi

echo
echo "==> created https://github.com/$TARGET ($VISIBILITY)"
if [ "$VISIBILITY" = public ]; then
    echo "    The DMG is attached to the v$VERSION release when the Build release workflow finishes."
fi
