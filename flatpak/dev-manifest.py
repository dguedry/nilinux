#!/usr/bin/env python3
"""Write a development manifest that builds the nilinux module from the
working tree instead of the pinned git tag. Used by build.sh and CI."""
import re, sys
from pathlib import Path
here = Path(__file__).resolve().parent
src = here / "io.github.dguedry.nilinux.yml"
dst = here / "io.github.dguedry.nilinux.dev.yml"
s = src.read_text()
s2 = re.sub(r"      - type: git\n        url: .*\n        tag: .*\n        commit: .*\n",
            "      - type: dir\n        path: ..\n        skip: [.flatpak-builder, build-dir, build-flathub, repo, repo-flathub, .git]\n", s)
if s2 == s: sys.exit("git source block not found in manifest")
dst.write_text(s2); print(dst)
