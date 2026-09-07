#!/usr/bin/env bash
# Launch Native Access in a Bottles (Flatpak) Wine bottle.
# Usage: ./launch-native-access.sh [bottle-name]   (default: Music)
#
# See README.md for the full playbook. Two fixes matter at launch time:
#
# 1. Stack: wine sizes the main thread from the exe's PE header
#    (SizeOfStackReserve = 8MB), which V8 overflows. ulimit -s does NOT
#    help — the PE header must be patched to 64MB. NA self-updates
#    replace the exe, so we re-patch on every launch if needed.
#
# 2. Fonts + runtime + NTK Daemon: one-time setup applied by
#    setup-native-access.sh (fonts/FontSubstitutes, real VC runtime with
#    overrides, helper service). Run that first on a fresh bottle.

BOTTLE="${1:-Music}"
NAEXE="$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/$BOTTLE/drive_c/Program Files/Native Instruments/Native Access/Native Access.exe"

[ -f "$NAEXE" ] || { echo "Native Access.exe not found in bottle '$BOTTLE'" >&2; exit 1; }

# Kill any stale Wine/NA processes first
pkill -f "Native Access" 2>/dev/null
sleep 1

# Clear stale NI scan mutexes (boost named mutexes aren't crash-safe: a
# crashed holder wedges every other NI app). Only when no NI app is running.
if ! pgrep -f "drive_c.*(Kontakt|Native Instruments).*\.exe" >/dev/null 2>&1; then
  rm -f "$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/$BOTTLE/drive_c/ProgramData/boost_interprocess/"*/* 2>/dev/null
fi

# Re-apply the 64MB stack reserve patch if an NA update replaced the exe
python3 - "$NAEXE" <<'EOF'
import struct, sys
p = sys.argv[1]
with open(p, 'r+b') as f:
    hdr = f.read(0x400)
    e_lfanew = struct.unpack_from('<I', hdr, 0x3c)[0]
    opt = e_lfanew + 24
    if struct.unpack_from('<H', hdr, opt)[0] != 0x20b:
        sys.exit("not a PE32+ exe, skipping stack patch")
    off = opt + 72
    reserve = struct.unpack_from('<Q', hdr, off)[0]
    if reserve < 0x4000000:
        f.seek(off)
        f.write(struct.pack('<Q', 0x4000000))
        print(f"patched stack reserve {reserve:#x} -> 0x4000000")
EOF

# Dependency-check patch (see README "Requires Kontakt" section): NA prefers
# an owned-but-uninstalled full Kontakt over the installed Player. The patch
# rewrites app.asar + the exe's Integrity hash; re-apply after NA updates.
if ! grep -a -q NA_DEP_PATCH "$(dirname "$NAEXE")/resources/app.asar" 2>/dev/null; then
  python3 "$(dirname "$0")/patch-na-dependency-check.py" "$BOTTLE" || echo "dependency patch failed (continuing)" >&2
fi

# --disable-gpu: NA >= 3.25 ships a Chromium whose GPU process crash-loops
# under Wine (blank window); software compositing renders correctly.
flatpak run --command=bottles-cli com.usebottles.bottles run -b "$BOTTLE" -e "$NAEXE" -- --disable-gpu
