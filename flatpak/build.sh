#!/usr/bin/env bash
# Build and install the Flatpak for the current user from the WORKING TREE.
# The committed manifest points Flathub at a git tag; this script swaps the
# nilinux module's source for the local directory before building.
set -euo pipefail
cd "$(dirname "$0")"
ID=io.github.dguedry.nilinux
python3 - "$ID.yml" "$ID.dev.yml" <<'PY'
import re, sys
src, dst = sys.argv[1:3]
s = open(src).read()
s = re.sub(r"      - type: git\n        url: .*\n        tag: .*\n        commit: .*\n",
           "      - type: dir\n        path: ..\n        skip: [.flatpak-builder, build-dir, repo, .git]\n", s)
open(dst, "w").write(s)
PY
flatpak-builder --user --install --force-clean --ccache --repo=repo build-dir "$ID.dev.yml" "$@"
echo "Run with: flatpak run $ID"
