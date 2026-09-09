#!/usr/bin/env bash
# Cut a release: bump the version, add a metainfo entry, commit, tag, push.
# CI (on the v* tag) builds nilinux.flatpak and attaches it to a GitHub release,
# which the README's "latest release" download link then serves.
#
# Usage:  scripts/release.sh <version> "<one-line changelog>"
#         scripts/release.sh 0.1.2 "Share /tmp so DAW plugins reach the NTK daemon"
#
#   --dry-run   make the edits and show them, but do not commit/tag/push
#               (reverts the working-tree edits afterwards)
set -euo pipefail

DRY=0
args=()
for a in "$@"; do
  if [ "$a" = "--dry-run" ]; then DRY=1; else args+=("$a"); fi
done
set -- "${args[@]}"

VERSION="${1:-}"
NOTE="${2:-}"
die() { echo "release: $*" >&2; exit 1; }

[ -n "$VERSION" ] || die "usage: scripts/release.sh <version> \"<changelog>\" [--dry-run]"
[ -n "$NOTE" ]    || die "a one-line changelog is required (it goes in the AppStream release notes)"
echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || die "version must be X.Y.Z, got '$VERSION'"

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

INIT="nilinux/__init__.py"
PYPROJECT="pyproject.toml"
METAINFO="data/io.github.dguedry.nilinux.metainfo.xml"

# --- preflight ---------------------------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || die "not on main (on '$BRANCH'); release from main"
if [ "$DRY" = 0 ]; then
  git diff --quiet -- "$INIT" "$PYPROJECT" "$METAINFO" \
    || die "version/metainfo files have uncommitted changes; commit or stash first"
fi
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null && die "tag v$VERSION already exists"
grep -q "version=\"$VERSION\"" "$METAINFO" && die "$METAINFO already has a $VERSION release entry"

CUR="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INIT")"
echo "release: $CUR -> $VERSION"

# --- edits -------------------------------------------------------------------
python3 - "$VERSION" "$NOTE" "$INIT" "$PYPROJECT" "$METAINFO" <<'PY'
import sys, datetime, re
ver, note, init, pyproject, metainfo = sys.argv[1:6]

def sub_one(path, pattern, repl):
    s = open(path).read()
    s2, n = re.subn(pattern, repl, s, count=1)
    if n != 1: sys.exit(f"release: could not update {path} (pattern not found once)")
    open(path, "w").write(s2)

sub_one(init, r'__version__ = "[^"]*"', f'__version__ = "{ver}"')
sub_one(pyproject, r'(?m)^version = "[^"]*"', f'version = "{ver}"')

# insert a new <release> as the first child of <releases>
today = datetime.date.today().isoformat()
from xml.sax.saxutils import escape
entry = (f'    <release version="{ver}" date="{today}">\n'
         f'      <description>\n'
         f'        <p>{escape(note)}</p>\n'
         f'      </description>\n'
         f'    </release>\n')
sub_one(metainfo, r'(<releases>\n)', r'\1' + entry.replace('\\', '\\\\'))
print("edited", init, pyproject, metainfo)
PY

# --- validate the metainfo ---------------------------------------------------
if command -v flatpak >/dev/null && flatpak info org.flatpak.Builder >/dev/null 2>&1; then
  echo "release: linting metainfo"
  flatpak run --command=flatpak-builder-lint org.flatpak.Builder appstream "$METAINFO" >/dev/null \
    || die "metainfo failed appstream lint (edits left in the working tree for inspection)"
fi

if [ "$DRY" = 1 ]; then
  echo "release: --dry-run, showing diff then reverting"
  git --no-pager diff -- "$INIT" "$PYPROJECT" "$METAINFO"
  git checkout -- "$INIT" "$PYPROJECT" "$METAINFO"
  exit 0
fi

# --- commit, tag, push -------------------------------------------------------
git add "$INIT" "$PYPROJECT" "$METAINFO"
git commit -q -m "Release $VERSION

$NOTE

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git tag -a "v$VERSION" -m "nilinux $VERSION"
git push origin main "v$VERSION"

echo
echo "release: pushed v$VERSION. CI will build and attach nilinux.flatpak to the release."
echo "  watch:   gh run watch \$(gh run list --branch v$VERSION --limit 1 --json databaseId --jq '.[0].databaseId')"
echo "  release: https://github.com/dguedry/nilinux/releases/tag/v$VERSION"
