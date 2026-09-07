#!/usr/bin/env bash
# update-native-access.sh — apply a pending Native Access self-update under Wine.
#
# NA's in-app updater downloads an NSIS installer and runs it on quit, but the
# installer aborts under Wine (exit 2: it expects an NSIS-installed app to
# uninstall first). This script does what the installer would have done:
# extracts the new app files, swaps the install dir, re-applies the PE stack
# patch, and updates the NTK Daemon if the new app bundles a newer one.
#
# Usage: ./update-native-access.sh [bottle-name]   (default: Music)
# Run after clicking "restart to update" in NA leaves the app dead.

set -euo pipefail

BOTTLE="${1:-Music}"
BOTTLES_DATA="$HOME/.var/app/com.usebottles.bottles/data/bottles"
PREFIX="$BOTTLES_DATA/bottles/$BOTTLE"
DRIVE_C="$PREFIX/drive_c"
NI="$DRIVE_C/Program Files/Native Instruments"
PENDING="$DRIVE_C/users/steamuser/AppData/Local/nativeaccess2-updater/pending/Native-Access-latest.exe"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo -e "\n==> $*"; }

[ -f "$PENDING" ] || die "no pending update at $PENDING — trigger the update in NA first"
for t in python3 7z; do command -v "$t" >/dev/null || die "missing host tool: $t"; done
RUNNER=$(grep -oP '^Runner:\s*\K\S+' "$PREFIX/bottle.yml")
# GE-Proton 11+ ships only "wine" (unified wow64); older runners have wine64
WINE=""
for w in files/bin/wine64 files/bin/wine bin/wine64 bin/wine; do
  [ -x "$BOTTLES_DATA/runners/$RUNNER/$w" ] && WINE="$BOTTLES_DATA/runners/$RUNNER/$w" && break
done
[ -n "$WINE" ] || die "wine binary not found for runner $RUNNER"

bwine() {
  flatpak run --command=bash com.usebottles.bottles -c \
    "export WINEPREFIX='$PREFIX'; export WINEFSYNC=1; exec '$WINE' $*"
}

say "Stopping Native Access"
pkill -f "Native Access.exe" 2>/dev/null || true
sleep 2

say "Extracting new app from updater"
7z e -y -o"$WORK" "$PENDING" '$PLUGINSDIR/app-64.7z' >/dev/null
mkdir -p "$WORK/app"
7z x -y -o"$WORK/app" "$WORK/app-64.7z" >/dev/null
[ -f "$WORK/app/Native Access.exe" ] || die "extracted archive has no Native Access.exe"

say "Swapping install directory"
rm -rf "$NI/Native Access.prev"
mv "$NI/Native Access" "$NI/Native Access.prev"
mv "$WORK/app" "$NI/Native Access"
echo "  previous version kept at 'Native Access.prev' (delete when happy)"

say "Re-applying 64MB stack patch"
python3 - "$NI/Native Access/Native Access.exe" <<'EOF'
import struct, sys
with open(sys.argv[1], 'r+b') as f:
    hdr = f.read(0x400)
    opt = struct.unpack_from('<I', hdr, 0x3c)[0] + 24
    off = opt + 72
    r = struct.unpack_from('<Q', hdr, off)[0]
    if r < 0x4000000:
        f.seek(off); f.write(struct.pack('<Q', 0x4000000))
        print(f"  patched {r:#x} -> 0x4000000")
    else:
        print("  already patched")
EOF

say "Checking bundled NTK Daemon"
NTK_SETUP=$(find "$NI/Native Access/resources/daemon" -iname "NTKDaemon*Setup*.exe" 2>/dev/null | head -1 || true)
if [ -n "$NTK_SETUP" ]; then
  echo "  bundled: $(basename "$NTK_SETUP")"
  mkdir -p "$WORK/ntk" && (cd "$WORK/ntk" && 7z x -y "$NTK_SETUP" >/dev/null)
  NTK_EXE=$(find "$WORK/ntk/data" -iname "NTKDaemon.exe" | head -1)
  if [ -n "$NTK_EXE" ]; then
    bwine "sc stop NTKDaemon" >/dev/null 2>&1 || true
    sleep 2
    cp -r "$(dirname "$NTK_EXE")/." "$DRIVE_C/Program Files/Common Files/Native Instruments/NTKDaemon/"
    bwine "sc start NTKDaemon" >/dev/null 2>&1 || true
    echo "  daemon files updated and service restarted"
  fi
fi

say "Done. Launch with: ./launch-native-access.sh $BOTTLE"
