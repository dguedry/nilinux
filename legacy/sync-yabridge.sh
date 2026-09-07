#!/usr/bin/env bash
# sync-yabridge.sh — bridge Windows VST2/VST3/CLAP plugins in a Bottles bottle
# to Linux DAWs via yabridge.
#
# Run after installing/updating plugins (Native Access or any third-party
# installer). It:
#   1. Installs yabridge (latest GitHub release) if not present.
#   2. Registers plugin directories with yabridgectl:
#        - the standard Windows locations (NI, Steinberg, Common Files VST2/VST3/CLAP)
#        - the VST2 path recorded in the bottle registry (HKLM\Software\VST\VSTPluginsPath)
#        - any directory holding a .vst3 or .clap bundle under Program Files
#          (those extensions are unambiguous; VST2 .dlls are not, so VST2 relies
#          on the standard list + registry + explicit extra dirs)
#        - extra directories given on the command line
#   3. Warns if the host wine is older than the bottle's runner (bridged plugins
#      refuse to load when the prefix was touched by a newer Wine).
#   4. Syncs, so bridged .so/.vst3/.clap plugins appear in ~/.vst, ~/.vst3,
#      ~/.clap where Linux DAWs (Ardour, REAPER, Bitwig, ...) pick them up.
#
# Idempotent — safe to run after every install session.
#
# Usage: ./sync-yabridge.sh [bottle-name] [extra-plugin-dir ...]
#        extra dirs may be absolute host paths or relative to the bottle's drive_c
# Requires: curl, tar, and a host wine on PATH (yabridge runs plugins with it).

set -euo pipefail

BOTTLE="${1:-Music}"; shift $(( $# > 0 ? 1 : 0 ))
EXTRA=("$@")
BOTTLE_DIR="$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/$BOTTLE"
DRIVE_C="$BOTTLE_DIR/drive_c"
YCTL="$HOME/.local/share/yabridge/yabridgectl"

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo -e "\n==> $*"; }

[ -d "$DRIVE_C" ] || die "bottle '$BOTTLE' not found"
command -v wine >/dev/null || die "no host wine on PATH — yabridge needs one (e.g. sudo apt install wine)"

# --- 1. Install yabridge if missing ------------------------------------------
if [ ! -x "$YCTL" ]; then
  say "Installing yabridge (latest release)"
  command -v curl >/dev/null || die "missing curl"
  URL=$(curl -sL https://api.github.com/repos/robbert-vdh/yabridge/releases/latest \
        | grep -oE '"browser_download_url": *"[^"]*yabridge-[0-9.]+\.tar\.gz"' \
        | grep -v 32bit | head -1 | cut -d'"' -f4)
  [ -n "$URL" ] || die "could not resolve latest yabridge release URL"
  echo "  $URL"
  T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
  curl -sL -o "$T/yabridge.tar.gz" "$URL"
  mkdir -p "$HOME/.local/share"
  tar -xzf "$T/yabridge.tar.gz" -C "$HOME/.local/share"   # extracts to ~/.local/share/yabridge
  mkdir -p "$HOME/.local/bin"
  ln -sf "$YCTL" "$HOME/.local/bin/yabridgectl"
  echo "  installed to ~/.local/share/yabridge"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "  NOTE: add ~/.local/bin to PATH to use 'yabridgectl' directly";; esac
else
  say "yabridge present: $("$YCTL" --version 2>/dev/null || echo unknown)"
fi

# --- 2. Wine version check ---------------------------------------------------
# yabridge runs plugins with the host `wine`; if that is older than the runner
# that created/updates the prefix, plugins fail with "prefix was updated by a
# newer Wine". Compare major versions and warn.
say "Wine versions"
HOST_WINE=$(wine --version 2>/dev/null || true)
RUNNER=$(sed -n 's/^Runner: *//p' "$BOTTLE_DIR/bottle.yml" 2>/dev/null | head -1)
host_major=$(echo "$HOST_WINE" | grep -oE '[0-9]+' | head -1 || true)
runner_major=$(echo "$RUNNER" | grep -oE '[0-9]+' | head -1 || true)
echo "  host:   ${HOST_WINE:-unknown}"
echo "  runner: ${RUNNER:-unknown}"
if [ -n "$host_major" ] && [ -n "$runner_major" ] && [ "$host_major" -lt "$runner_major" ]; then
  echo "  WARNING: host wine $host_major.x is older than runner major $runner_major."
  echo "           If bridged plugins refuse to load (\"prefix was updated by a newer Wine\"),"
  echo "           install a host wine >= $runner_major (e.g. wine-staging from winehq.org)."
fi

# --- 3. Collect plugin directories -------------------------------------------
say "Collecting plugin directories"
declare -a DIRS=()
add_dir() {  # add_dir <host path>  — dedupe, refuse over-broad roots
  local d="$1"
  case "$d" in
    "$DRIVE_C"|"$DRIVE_C/"|"$DRIVE_C/Program Files"|"$DRIVE_C/Program Files/Common Files"|"$DRIVE_C/Program Files (x86)")
      echo "  ! refusing over-broad directory: ${d#$DRIVE_C/}"; return;;
  esac
  for x in "${DIRS[@]:-}"; do [ "$x" = "$d" ] && return; done
  DIRS+=("$d")
}
winpath_to_host() {  # C:\foo\bar -> $DRIVE_C/foo/bar
  local p="${1//\\//}"; p="${p#[Cc]:/}"; p="${p#[Cc]:}"
  printf '%s\n' "$DRIVE_C/$p"
}

# 3a. standard 64-bit locations (created if missing so the config survives
#     being set up before the first plugin install)
for rel in \
  "Program Files/Native Instruments/VSTPlugins 64 bit" \
  "Program Files/VstPlugins" \
  "Program Files/Steinberg/VstPlugins" \
  "Program Files/Common Files/VST2" \
  "Program Files/Common Files/Steinberg/VST2" \
  "Program Files/Common Files/VST3" \
  "Program Files/Common Files/CLAP"; do
  mkdir -p "$DRIVE_C/$rel"; add_dir "$DRIVE_C/$rel"
done

# 3b. VST2 path recorded in the registry (installers honour/update this)
for reg in "$BOTTLE_DIR/system.reg" "$BOTTLE_DIR/user.reg"; do
  [ -f "$reg" ] || continue
  while IFS= read -r line; do
    v=$(printf '%s' "$line" | sed -n 's/^"VSTPluginsPath"="\(.*\)"$/\1/p'); [ -n "$v" ] || continue
    v="${v//\\\\/\\}"   # .reg escapes backslashes
    p=$(winpath_to_host "$v")
    [ -d "$p" ] && { echo "  registry VSTPluginsPath: $v"; add_dir "$p"; }
  done < <(grep -A20 -E '^\[Software\\\\(Wow6432Node\\\\)?VST\]' "$reg" 2>/dev/null | grep '"VSTPluginsPath"' || true)
done

# 3c. discover VST3 / CLAP bundles anywhere under Program Files by extension
while IFS= read -r f; do
  add_dir "$(dirname "$f")"
done < <(find "$DRIVE_C/Program Files" -maxdepth 6 \( -iname '*.vst3' -o -iname '*.clap' \) \
           -not -path '*/Program Files (x86)/*' 2>/dev/null | sort)

# 3d. explicit extras
for e in "${EXTRA[@]:-}"; do
  [ -n "$e" ] || continue
  case "$e" in /*) p="$e";; *) p="$DRIVE_C/$e";; esac
  [ -d "$p" ] || die "extra dir not found: $p"
  add_dir "$p"
done

# --- 4. Register ---------------------------------------------------------------
say "Registering plugin directories"
for d in "${DIRS[@]}"; do
  "$YCTL" add "$d" >/dev/null 2>&1 || true
  n=$(find "$d" -maxdepth 1 \( -iname '*.dll' -o -iname '*.vst3' -o -iname '*.clap' \) 2>/dev/null | wc -l)
  echo "  + ${d#$DRIVE_C/}  ($n candidate file(s))"
done

# --- 5. Sync -------------------------------------------------------------------
say "Syncing bridges"
"$YCTL" sync --prune

say "Status"
"$YCTL" status || true

cat <<'EOF'

Done. Bridged plugins land in ~/.vst/yabridge (VST2), ~/.vst3/yabridge (VST3)
and ~/.clap/yabridge (CLAP) — point your DAW's plugin search paths there if it
doesn't scan them by default. Re-run this script after every plugin install
(Native Access or third-party) so new products get bridged. Installed a plugin
somewhere unusual? Pass its folder:  ./sync-yabridge.sh Music "Program Files/Vendor/Plugin"
EOF
