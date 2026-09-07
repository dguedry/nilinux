#!/usr/bin/env bash
# setup-native-access.sh — make Native Access 3.x work in a Bottles (Flatpak) Wine bottle.
#
# Fixes applied (all idempotent, safe to re-run):
#   1. PE stack patch:   Native Access.exe reserves 8MB stack; V8 needs 64MB.
#   2. Fonts:            installs DejaVu/Liberation/Ubuntu/Noto into the bottle and
#                        maps Segoe UI etc. to them (prevents GDI handle exhaustion).
#   3. VC runtime:       real ucrtbase.dll + matched VC++ 2022 runtime with native
#                        overrides (Wine builtins crash NI's code).
#   4. NTK Daemon:       extracts NA's bundled helper (its MSI crashes under Wine),
#                        installs it manually, registers it as an auto-start service.
#   5. NA config:        enables hardware acceleration (avoids GDI software path).
#
# Prerequisites:
#   - Bottles flatpak (com.usebottles.bottles) with a bottle that has
#     Native Access 3.x already installed in it (run the NA installer in the
#     bottle first; use Windows 10 mode).
#   - Host tools: python3, 7z (p7zip-full), cabextract, curl
#
# Usage:  ./setup-native-access.sh [bottle-name]     (default: Music)

set -euo pipefail

BOTTLE="${1:-Music}"
BOTTLES_DATA="$HOME/.var/app/com.usebottles.bottles/data/bottles"
PREFIX="$BOTTLES_DATA/bottles/$BOTTLE"
DRIVE_C="$PREFIX/drive_c"
NA_DIR="$DRIVE_C/Program Files/Native Instruments/Native Access"
NAEXE="$NA_DIR/Native Access.exe"
S32="$DRIVE_C/windows/system32"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# winetricks ucrtbase2019 source (last VC2019 redist that still ships ucrtbase.dll)
UCRT_URL="https://download.visualstudio.microsoft.com/download/pr/85d47aa9-69ae-4162-8300-e6b7e4bf3cf3/52B196BBE9016488C735E7B41805B651261FFA5D7AA86EB6A1D0095BE83687B2/VC_redist.x64.exe"
UCRT_SHA256="52b196bbe9016488c735e7b41805b651261ffa5d7aa86eb6a1d0095be83687b2"
VC2022_URL="https://aka.ms/vs/17/release/vc_redist.x64.exe"

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo -e "\n==> $*"; }

# --- sanity checks -----------------------------------------------------------
[ -d "$PREFIX" ] || die "bottle '$BOTTLE' not found at $PREFIX"
[ -f "$NAEXE" ]  || die "Native Access.exe not found — install Native Access in the bottle first"
for t in python3 7z cabextract curl; do command -v "$t" >/dev/null || die "missing host tool: $t"; done

RUNNER=$(grep -oP '^Runner:\s*\K\S+' "$PREFIX/bottle.yml" 2>/dev/null || true)
[ -n "$RUNNER" ] || die "could not read Runner from $PREFIX/bottle.yml"
# GE-Proton 11+ ships only "wine" (unified wow64); older runners have wine64
WINE=""
for w in files/bin/wine64 files/bin/wine bin/wine64 bin/wine; do
  [ -x "$BOTTLES_DATA/runners/$RUNNER/$w" ] && WINE="$BOTTLES_DATA/runners/$RUNNER/$w" && break
done
[ -n "$WINE" ] || die "wine binary not found for runner $RUNNER"
say "bottle=$BOTTLE runner=$RUNNER"

# Normalize the bottle to fsync: this script (and launch/update) run wine with
# WINEFSYNC=1, and a bottles-cli launch honoring a different bottle.yml sync
# setting against the same wineserver breaks Electron child-process IPC.
if grep -qE "^\s*sync: (wine|esync)" "$PREFIX/bottle.yml"; then
  sed -i 's/^\(\s*\)sync: \(wine\|esync\)/\1sync: fsync/' "$PREFIX/bottle.yml"
  echo "  bottle sync setting normalized to fsync"
fi

# All wine calls go through the flatpak sandbox with fsync enabled to match
# the wineserver that bottles-cli starts.
bwine() {
  flatpak run --command=bash com.usebottles.bottles -c \
    "export WINEPREFIX='$PREFIX'; export WINEFSYNC=1; exec '$WINE' $*"
}

say "Stopping any running Native Access"
pkill -f "Native Access.exe" 2>/dev/null || true
sleep 2

# --- 1. PE stack patch -------------------------------------------------------
say "Patching Native Access.exe stack reserve to 64MB"
cp -n "$NAEXE" "$NAEXE.bak" 2>/dev/null || true
python3 - "$NAEXE" <<'EOF'
import struct, sys
p = sys.argv[1]
with open(p, 'r+b') as f:
    hdr = f.read(0x400)
    e_lfanew = struct.unpack_from('<I', hdr, 0x3c)[0]
    opt = e_lfanew + 24
    assert struct.unpack_from('<H', hdr, opt)[0] == 0x20b, "not PE32+"
    off = opt + 72
    reserve = struct.unpack_from('<Q', hdr, off)[0]
    if reserve < 0x4000000:
        f.seek(off); f.write(struct.pack('<Q', 0x4000000))
        print(f"  patched {reserve:#x} -> 0x4000000")
    else:
        print("  already patched")
EOF

# --- 2. Fonts ----------------------------------------------------------------
say "Installing fonts into the bottle"
FONTS="$DRIVE_C/windows/Fonts"
mkdir -p "$FONTS"
copyfonts() { for f in "$@"; do [ -f "$f" ] && cp -u "$f" "$FONTS/"; done; }
# IMPORTANT: copy ONLY the 4 base faces per family. Extra same-family faces
# (DejaVu Condensed/ExtraLight etc.) poison Wine's font matching for the
# substituted UI font and cause a GDI-handle storm — NA window never appears.
copyfonts /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf \
          /usr/share/fonts/truetype/liberation/*.ttf \
          /usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf \
          /usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf \
          /usr/share/fonts/truetype/ubuntu/Ubuntu-RI.ttf \
          /usr/share/fonts/truetype/ubuntu/Ubuntu-BI.ttf \
          /usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf \
          /usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf \
          /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf
# purge any same-family extras from a previous run of this script
rm -f "$FONTS"/DejaVuSansCondensed*.ttf "$FONTS"/DejaVuSans-ExtraLight.ttf \
      "$FONTS"/DejaVuSansMono-Oblique.ttf "$FONTS"/DejaVuSansMono-BoldOblique.ttf \
      "$FONTS"/Ubuntu-MI.ttf "$FONTS"/Ubuntu-M.ttf 2>/dev/null
echo "  $(ls "$FONTS" | wc -l) fonts in bottle"

cat > "$DRIVE_C/na-setup.reg" <<'EOF'
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\Software\Microsoft\Windows NT\CurrentVersion\FontSubstitutes]
"Segoe UI"="DejaVu Sans"
"Segoe UI Light"="DejaVu Sans"
"Segoe UI Semibold"="DejaVu Sans"
"Segoe UI Semilight"="DejaVu Sans"
"Segoe UI Black"="DejaVu Sans"
"Segoe UI Symbol"="Noto Sans Symbols"
"Segoe UI Emoji"="Noto Color Emoji"
"Segoe MDL2 Assets"="Noto Sans Symbols2"
"Segoe Fluent Icons"="Noto Sans Symbols2"
"Tahoma"="DejaVu Sans"
"Verdana"="DejaVu Sans"
"Microsoft Sans Serif"="DejaVu Sans"
"Calibri"="Liberation Sans"
"Cambria"="Liberation Serif"
"Consolas"="DejaVu Sans Mono"

[HKEY_CURRENT_USER\Software\Wine\DllOverrides]
"ucrtbase"="native,builtin"
"msvcp140"="native,builtin"
"msvcp140_1"="native,builtin"
"msvcp140_2"="native,builtin"
"msvcp140_atomic_wait"="native,builtin"
"msvcp140_codecvt_ids"="native,builtin"
"vcruntime140"="native,builtin"
"vcruntime140_1"="native,builtin"
"concrt140"="native,builtin"
"vcomp140"="native,builtin"
EOF

# --- 3. VC runtime -----------------------------------------------------------
say "Installing real ucrtbase.dll (winetricks ucrtbase2019 recipe)"
curl -sL -o "$WORK/vc2019.exe" "$UCRT_URL"
echo "$UCRT_SHA256  $WORK/vc2019.exe" | sha256sum -c - >/dev/null || die "ucrtbase download hash mismatch"
cabextract -q -d "$WORK" -F 'a10' "$WORK/vc2019.exe"
cabextract -q -d "$WORK" -F 'ucrtbase.dll' "$WORK/a10"
cp -n "$S32/ucrtbase.dll" "$S32/ucrtbase.dll.wine-builtin.bak" 2>/dev/null || true
cp "$WORK/ucrtbase.dll" "$S32/ucrtbase.dll"

say "Installing matched VC++ 2022 x64 runtime set"
curl -sL -o "$WORK/vc2022.exe" "$VC2022_URL"
cabextract -q -d "$WORK" "$WORK/vc2022.exe" 2>/dev/null || true
# a12 = "Visual C++ 2022 X64 Minimum Runtime" cab; files are named <dll>_amd64
RT_CAB=$(grep -l "vcruntime140.dll_amd64" "$WORK"/a* 2>/dev/null | head -1 || true)
if [ -z "$RT_CAB" ]; then
  for c in "$WORK"/a*; do
    7z l "$c" 2>/dev/null | grep -q "vcruntime140.dll_amd64" && { RT_CAB="$c"; break; }
  done
fi
[ -n "$RT_CAB" ] || die "could not locate x64 runtime cab in VC redist"
mkdir -p "$WORK/rt" && cabextract -q -d "$WORK/rt" "$RT_CAB"
for f in "$WORK"/rt/*_amd64; do
  d="$(basename "${f%.dll_amd64}").dll"
  cp -n "$S32/$d" "$S32/$d.bak" 2>/dev/null || true
  cp "$f" "$S32/$d"
done
echo "  runtime installed"

say "Importing registry (font substitutes + DLL overrides)"
bwine "regedit 'C:\\na-setup.reg'" >/dev/null 2>&1
echo "  imported"

# --- 4. NTK Daemon -----------------------------------------------------------
say "Installing NTK Daemon helper service"
NTK_SETUP=$(find "$NA_DIR/resources/daemon" -iname "NTKDaemon*Setup*.exe" | head -1)
[ -n "$NTK_SETUP" ] || die "NTK Daemon setup exe not found under $NA_DIR/resources/daemon"
mkdir -p "$WORK/ntk" && (cd "$WORK/ntk" && 7z x -y "$NTK_SETUP" >/dev/null)
NTK_EXE=$(find "$WORK/ntk/data" -iname "NTKDaemon.exe" | head -1)
[ -n "$NTK_EXE" ] || die "NTKDaemon.exe not found in extracted setup"
NTK_DEST="$DRIVE_C/Program Files/Common Files/Native Instruments/NTKDaemon"
mkdir -p "$NTK_DEST"
cp -r "$(dirname "$NTK_EXE")/." "$NTK_DEST/"
bwine "sc create NTKDaemon binPath= '\"C:\\Program Files\\Common Files\\Native Instruments\\NTKDaemon\\NTKDaemon.exe\"' start= auto" >/dev/null 2>&1 || true
bwine "sc start NTKDaemon" >/dev/null 2>&1 || true
if bwine "sc query NTKDaemon" 2>/dev/null | grep -q RUNNING; then
  echo "  NTKDaemon service RUNNING"
else
  echo "  WARNING: NTKDaemon not confirmed running — it should auto-start on next bottle session"
fi

# --- 5. NA config ------------------------------------------------------------
say "Enabling hardware acceleration in NA settings"
# NA stores settings in a hash-named json; flip the flag in whichever file has it.
NA_ROAM="$DRIVE_C/users/steamuser/AppData/Roaming/Native Instruments/Native Access"
if [ -d "$NA_ROAM" ]; then
  grep -l '"disableHardwareAcceleration": true' "$NA_ROAM"/*.json 2>/dev/null | while read -r f; do
    sed -i 's/"disableHardwareAcceleration": true/"disableHardwareAcceleration": false/' "$f"
    echo "  updated $(basename "$f")"
  done
fi

# --- 6. Desktop launcher (best effort) ---------------------------------------
if command -v wrestool >/dev/null && command -v icotool >/dev/null; then
  say "Installing desktop launcher with the Native Access icon"
  ICO="$WORK/na-ico"
  mkdir -p "$ICO"
  wrestool -x --type=14 -o "$ICO/" "$NAEXE" 2>/dev/null || true
  if ls "$ICO"/*.ico >/dev/null 2>&1; then
    icotool -x -o "$ICO/" "$ICO"/*.ico 2>/dev/null || true
    for png in "$ICO"/*x32.png; do
      [ -f "$png" ] || continue
      w=$(basename "$png" | grep -oE '[0-9]+x[0-9]+' | head -1 | cut -dx -f1)
      d="$HOME/.local/share/icons/hicolor/${w}x${w}/apps"
      mkdir -p "$d" && cp "$png" "$d/native-access.png"
    done
    LAUNCHER="$(cd "$(dirname "$0")" && pwd)/launch-native-access.sh"
    mkdir -p "$HOME/.local/share/applications"
    cat > "$HOME/.local/share/applications/native-access.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Native Access
Comment=Native Instruments product installer (Wine/Bottles)
Exec=$LAUNCHER $BOTTLE
Icon=native-access
Terminal=false
Categories=AudioVideo;Audio;
Keywords=Native Instruments;Kontakt;NI;
StartupNotify=false
StartupWMClass=steam_proton
DESK
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    echo "  launcher installed (app menu: Native Access)"
  else
    echo "  could not extract icon, skipping launcher"
  fi
else
  say "Skipping desktop launcher (install icoutils for the icon: sudo apt install icoutils)"
fi

say "Done. Launch with:  ./launch-native-access.sh (or bottles-cli run -b $BOTTLE -e \"\$NAEXE\")"
