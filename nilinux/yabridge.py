"""Bridge the prefix's Windows plugins to Linux DAWs with yabridge.

yabridge runs plugins with the `wine` on PATH (or WINELOADER). We point it at
the app's own wine so prefix and plugin host can never drift apart.
"""
import json, os, re, shutil, subprocess, tarfile
from pathlib import Path
from . import paths
from .download import fetch, text
from .progress import null_reporter
from .wine import Prefix

YAB_DIR = Path.home() / ".local/share/yabridge"
YCTL = YAB_DIR / "yabridgectl"
STANDARD_DIRS = [
    "Program Files/Native Instruments/VSTPlugins 64 bit",
    "Program Files/VstPlugins",
    "Program Files/Steinberg/VstPlugins",
    "Program Files/Common Files/VST2",
    "Program Files/Common Files/Steinberg/VST2",
    "Program Files/Common Files/VST3",
    "Program Files/Common Files/CLAP",
]
BROAD = ("", "Program Files", "Program Files/Common Files", "Program Files (x86)")

def _yctl_env(p: Prefix) -> dict:
    """yabridgectl env: our wine first on PATH; config in the *host* ~/.config so
    a yabridgectl run outside the sandbox sees the same plugin dirs."""
    e = p.env()
    # Inside Flatpak XDG_* are remapped to ~/.var/app/<id>/...; yabridgectl must see
    # the host locations (its libs in ~/.local/share/yabridge, config in ~/.config)
    # because the DAW-side yabridge on the host reads the same places.
    e["XDG_CONFIG_HOME"] = str(Path.home() / ".config"); e["XDG_DATA_HOME"] = str(Path.home() / ".local/share")
    return e

def installed() -> str | None:
    if not YCTL.exists(): return None
    if (m := build_marker()):
        return f"{m.get('yabridge_commit', '?')} (git {m.get('yabridge_ref', 'master')}, built for wine {m.get('wine_version', '?')})"
    try: return subprocess.run([str(YCTL), "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception: return "unknown"

# --- which yabridge works with which Wine ------------------------------------------------------
# yabridge's last release (5.1.1, Nov 2024) predates the window-management changes
# in Wine 9.22: with newer Wine, mouse clicks in bridged plugin GUIs land in the
# wrong place, in every DAW. The fix lives in yabridge's master branch, so nilinux
# builds master against its pinned Wine (scripts/build-yabridge.sh, published with
# each nilinux release) and installs that instead of the upstream release.
NILINUX_REPO = "dguedry/nilinux"
MARKER = YAB_DIR / "nilinux-build.json"
WINE_NEEDS_MASTER = (9, 22)

def build_marker() -> dict | None:
    """Metadata of a nilinux-built yabridge, or None for an upstream release."""
    try: return json.loads(MARKER.read_text()) if MARKER.exists() else None
    except (OSError, ValueError): return None

def pinned_wine_version() -> str:
    from .wine import WINE_BUILD
    m = re.search(r"[0-9]+(?:\.[0-9]+)+", WINE_BUILD["name"])
    return m.group(0) if m else ""

def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

def needs_master(wine_version: str) -> bool:
    return bool(wine_version) and _vtuple(wine_version) >= WINE_NEEDS_MASTER

def pick_asset(assets: list[dict], wine_version: str) -> dict | None:
    """The nilinux-built yabridge tarball for exactly this Wine version, from a
    GitHub release's asset list ([{'name', 'browser_download_url'}, ...])."""
    pat = re.compile(rf"^yabridge-[0-9a-f]+-wine-{re.escape(wine_version)}\.tar\.gz$")
    for a in assets:
        if pat.match(a.get("name", "")): return a
    return None

def compatibility() -> tuple[bool, str]:
    """(ok, detail) for the installed yabridge against the pinned Wine."""
    if not YCTL.exists(): return False, "not installed"
    pinned = pinned_wine_version()
    if (m := build_marker()):
        if pinned and str(m.get("wine_version", "")).startswith(pinned): return True, f"nilinux build {m.get('yabridge_commit', '?')} for wine {pinned}"
        return False, f"built for wine {m.get('wine_version', '?')}, but this app pins wine {pinned}"
    if not needs_master(pinned): return True, f"upstream release; fine with wine {pinned}"
    return False, (f"upstream release {installed()} predates Wine 9.22's window changes: with wine {pinned} mouse clicks in "
                   "bridged plugin GUIs land in the wrong place, in every DAW")

def _find_build_tarball(wine_version: str, r) -> tuple[Path | None, str]:
    """A nilinux-built yabridge for this Wine: NILINUX_YABRIDGE_TARBALL, else the
    asset attached to the latest nilinux release. Returns (local path, label)."""
    override = os.environ.get("NILINUX_YABRIDGE_TARBALL")
    if override:
        f = Path(override).expanduser()
        if f.is_file(): return f, f"{f.name} (NILINUX_YABRIDGE_TARBALL)"
        r.log(f"NILINUX_YABRIDGE_TARBALL={override} does not exist; ignoring")
    try:
        rel = json.loads(text(f"https://api.github.com/repos/{NILINUX_REPO}/releases/latest"))
    except Exception as e:
        r.log(f"could not read nilinux releases: {str(e)[:80]}"); return None, ""
    a = pick_asset(rel.get("assets", []), wine_version)
    if not a: return None, ""
    return fetch(a["browser_download_url"], paths.DOWNLOADS / a["name"], reporter=r, label="yabridge"), f"{a['name']} (nilinux release {rel.get('tag_name', '')})"

def _install_tarball(tgz: Path, r):
    """Replace ~/.local/share/yabridge with the tarball's yabridge/ directory."""
    if YAB_DIR.exists():
        bak = YAB_DIR.with_name(YAB_DIR.name + ".bak")
        if bak.exists(): shutil.rmtree(bak)
        YAB_DIR.rename(bak)
    YAB_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz) as t: t.extractall(YAB_DIR.parent, filter="tar")
    (Path.home() / ".local/bin").mkdir(parents=True, exist_ok=True)
    link = Path.home() / ".local/bin/yabridgectl"
    if not link.exists(): link.symlink_to(YCTL)

def install(reporter=None, force=False) -> str:
    """Install yabridge, or replace an upstream release that cannot work with the
    pinned Wine by nilinux's own build of yabridge master. Idempotent."""
    r = null_reporter(reporter)
    pinned = pinned_wine_version()
    marker = build_marker()
    if YCTL.exists() and not force:
        if marker and pinned and str(marker.get("wine_version", "")).startswith(pinned): return installed() or "present"
        if not marker and not needs_master(pinned): return installed() or "present"
    r.step("Installing yabridge" + (f" for wine {pinned}" if pinned else ""))
    tgz, label = _find_build_tarball(pinned, r)
    if tgz is not None:
        _install_tarball(tgz, r)
        if not MARKER.exists():
            MARKER.write_text(json.dumps({"yabridge_ref": "unknown", "yabridge_commit": "unknown", "wine_version": pinned}))
        r.ok(label); return installed() or "present"
    if YCTL.exists():
        r.skip(f"keeping {installed()}; no nilinux build for wine {pinned} is published yet"); return installed() or "present"
    rel = json.loads(text("https://api.github.com/repos/robbert-vdh/yabridge/releases/latest"))
    url = next(a["browser_download_url"] for a in rel["assets"]
               if re.fullmatch(r"yabridge-[0-9.]+\.tar\.gz", a["name"]))
    tgz = fetch(url, paths.DOWNLOADS / Path(url).name, reporter=r, label="yabridge")
    _install_tarball(tgz, r)
    r.ok(rel["tag_name"] + (" -- NOTE: too old for this wine, see Health" if needs_master(pinned) else "")); return rel["tag_name"]

def registry_vst_path(p: Prefix) -> Path | None:
    for hive in ("system.reg", "user.reg"):
        f = p.path / hive
        if not f.exists(): continue
        txt = f.read_text(errors="ignore")
        for m in re.finditer(r"^\[Software\\\\(?:Wow6432Node\\\\)?VST\][^\[]*", txt, re.M | re.S):
            v = re.search(r'"VSTPluginsPath"="(.*)"', m.group(0))
            if v:
                host = p.to_host(v.group(1).replace("\\\\", "\\"))
                if host.is_dir(): return host
    return None

def plugin_dirs(p: Prefix, extras=()) -> list[Path]:
    """Standard dirs (created), registry VST2 path, discovered .vst3/.clap dirs, extras."""
    out: list[Path] = []
    def add(d: Path):
        d = Path(d)
        try: rel = str(d.resolve().relative_to(p.drive_c.resolve()))
        except ValueError: rel = None
        if rel is not None and rel.rstrip("/") in BROAD or rel == ".": return
        if d not in out: out.append(d)
    for rel in STANDARD_DIRS:
        d = p.drive_c / rel; d.mkdir(parents=True, exist_ok=True); add(d)
    rp = registry_vst_path(p)
    if rp: add(rp)
    pf = p.drive_c / "Program Files"
    for f in sorted(pf.rglob("*")):
        if f.suffix.lower() in (".vst3", ".clap") and "Program Files (x86)" not in str(f):
            if len(f.relative_to(pf).parts) <= 6: add(f.parent)
    for e in extras:
        d = Path(e) if str(e).startswith("/") else p.drive_c / e
        if not d.is_dir(): raise FileNotFoundError(f"extra plugin dir not found: {d}")
        add(d)
    return out

def sync(p: Prefix, reporter=None, extras=()) -> dict:
    r = null_reporter(reporter)
    install(r)
    # Never hand plugins to a DAW that would run them with another Wine: its
    # prefix update replaces this prefix's DLLs (seen with the host's wine 9.0).
    configure_daw_environment(p, r)
    st, detail = daw_environment_status(p)
    if st != "active":
        r.step("Bridging plugins"); r.skip(f"not yet — {detail}")
        return {"dirs": [], "returncode": 0, "output": "", "summary": f"plugins not bridged: {detail}", "skipped": st}
    dirs = plugin_dirs(p, extras)
    r.step("Registering plugin directories")
    env = _yctl_env(p)
    for d in dirs:
        subprocess.run([str(YCTL), "add", str(d)], capture_output=True, env=env)
    r.ok(f"{len(dirs)} directories")
    # The nilinux that runs owns the bridges. Directories of *other* nilinux prefixes
    # (the Flatpak vs the source install, an old app id) would make same-named plugins
    # link into a prefix whose NTK daemon is not the one running -- and hang in every
    # DAW. Third-party prefixes the user registered themselves are left alone.
    from . import prefixes
    foreign = prefixes.foreign_yabridge_dirs(p, status(p))
    removed = []
    if foreign:
        r.step("Unregistering other nilinux prefixes' plugin directories")
        for d in foreign:
            cp0 = subprocess.run([str(YCTL), "rm", d], capture_output=True, text=True, env=env)
            if cp0.returncode == 0: removed.append(d)
            else: r.log(f"could not remove {d}: {(cp0.stderr or cp0.stdout).strip()[:100]}")
        owners = sorted({prefixes.short(prefixes.prefix_of_dir(d)) for d in removed if prefixes.prefix_of_dir(d)})
        r.ok(f"{len(removed)} from {', '.join(owners)}" if removed else "nothing removed")
    r.step("Syncing yabridge")
    cp = subprocess.run([str(YCTL), "sync", "--prune"], capture_output=True, text=True, env=env, timeout=1800)
    summary = next((l for l in cp.stdout.splitlines() if l.startswith("Finished")), "")
    if not summary: summary = (cp.stderr or cp.stdout).strip().splitlines()[-1:] or [""]; summary = summary[0][:160]
    (r.ok if cp.returncode == 0 else r.fail)(summary)
    for l in cp.stdout.splitlines():
        if l.startswith("WARNING"): r.log(l)
    return {"dirs": [str(d) for d in dirs], "removed_dirs": removed, "returncode": cp.returncode, "output": cp.stdout, "summary": summary}

def status(p: Prefix) -> str:
    if not YCTL.exists(): return "yabridge not installed"
    return subprocess.run([str(YCTL), "status"], capture_output=True, text=True, env=_yctl_env(p), timeout=120).stdout

def daw_environment_file() -> Path:
    """Legacy: an earlier release wrote WINELOADER here, which routed *every*
    wine on the machine to the app's build. Removed by configure_daw_environment."""
    return Path.home() / ".config/environment.d/50-nilinux.conf"

WINE_SHIM = Path.home() / ".local/bin/wine"
SHIM_MARK = "# nilinux: DAWs run yabridge plugins with the app's wine (same build as the prefix)"

def legacy_daw_environment_content(p: Prefix) -> str:
    return f"WINELOADER={p.build.wine}\nWINEFSYNC=1\n"

def shim_content(p: Prefix) -> str:
    """A `wine` on PATH that routes by prefix: WINEPREFIX inside the app's prefix
    runs the app's wine (yabridge sets WINEPREFIX from the plugin's location);
    any other prefix -- ~/.wine, the user's own -- runs the next wine on PATH,
    so a host Wine keeps serving its prefixes exactly as before. If the app's
    wine is gone (uninstalled) everything falls through as well."""
    return f"""#!/bin/sh
{SHIM_MARK}
# Only the nilinux prefix is routed to the app's wine. Every other prefix runs
# the next wine on PATH after this file, so your own Wine keeps working.
NILINUX_PREFIX="{p.path.resolve()}"
NILINUX_WINE="{p.build.wine}"
wp=$(cd "${{WINEPREFIX:-$HOME/.wine}}" 2>/dev/null && pwd -P)
case "$wp" in
  "$NILINUX_PREFIX"|"$NILINUX_PREFIX"/*)
    if [ -x "$NILINUX_WINE" ]; then unset NILINUX_SHIM_SEEN; export WINEFSYNC=1; exec "$NILINUX_WINE" "$@"; fi ;;
esac
# Fall through to the next wine on PATH. Only shell builtins below: a DAW's
# PATH may be minimal. NILINUX_SHIM_SEEN stops a fork loop if self-detection
# ever fails (e.g. the shim reached through a path we cannot compare).
case "$0" in */*) selfdir=${{0%/*}} ;; *) selfdir=. ;; esac
self=$(cd "$selfdir" 2>/dev/null && pwd -P)
if [ "${{NILINUX_SHIM_SEEN-}}" = "$self" ]; then
  echo "wine: the nilinux shim $0 would run itself again; check PATH" >&2; exit 127
fi
export NILINUX_SHIM_SEEN="$self"
IFS=:
for d in $PATH; do
  [ -n "$d" ] || d=.
  [ "$d/wine" -ef "$0" ] && continue
  [ "$(cd "$d" 2>/dev/null && pwd -P)" = "$self" ] && continue
  if [ -f "$d/wine" ] && [ -x "$d/wine" ]; then unset IFS NILINUX_SHIM_SEEN; exec "$d/wine" "$@"; fi
done
echo "wine: command not found (the nilinux shim $0 found no other wine on PATH)" >&2
exit 127
"""

def shim_is_ours(p: Prefix) -> bool:
    f = WINE_SHIM
    try:
        if f.is_symlink(): return f.resolve() == p.build.wine.resolve()
        return f.is_file() and SHIM_MARK in f.read_text() and str(p.build.wine) in f.read_text()
    except OSError: return False

def configure_daw_environment(p: Prefix, reporter=None) -> bool:
    """Make DAWs run this prefix's plugins with the app's wine: a ~/.local/bin/wine
    shim that routes by prefix (see shim_content). yabridge resolves `wine`
    through PATH, and ~/.local/bin precedes /usr/bin on Debian/Ubuntu/Mint/Fedora
    desktops, so it is effective for every wine started from now on, no re-login.
    Also removes the legacy environment.d WINELOADER file (it routed every prefix).
    Returns True if anything was written."""
    r = null_reporter(reporter)
    r.step("DAWs use this wine for this prefix (~/.local/bin/wine shim)")
    changed = False
    shim = WINE_SHIM
    if (shim.exists() or shim.is_symlink()) and not shim_is_ours(p):
        try: cur = shim.read_text()
        except OSError: cur = ""
        if SHIM_MARK not in cur:
            r.fail(f"{shim} exists and is not this app's — remove it, or point it at {p.build.wine}"); return False
    if not shim_is_ours(p) or shim.read_text() != shim_content(p):
        shim.parent.mkdir(parents=True, exist_ok=True)
        if shim.is_symlink(): shim.unlink()
        shim.write_text(shim_content(p)); shim.chmod(0o755); changed = True
    f = daw_environment_file()
    try:
        if f.is_file() and f.read_text() == legacy_daw_environment_content(p):
            f.unlink(); changed = True
    except OSError: pass
    (r.ok if changed else r.skip)(f"{shim} routes {p.path.name} -> {p.build.wine.name}; other prefixes keep their wine" if changed else "configured")
    return changed

def daw_environment_status(p: Prefix) -> tuple[str, str]:
    """('active' | 'missing', detail).
    active:  wine started by a DAW for this prefix is this wine (our shim in
             ~/.local/bin, WINELOADER in this session, or the wine on PATH *is* it)
    missing: not configured — a DAW would use the host's wine, whose prefix
             update rewrites the app's prefix with another Wine's DLLs"""
    want = str(p.build.wine)
    if shim_is_ours(p): return "active", f"{WINE_SHIM} routes this prefix to this wine; other prefixes keep the wine after it on PATH (assumes ~/.local/bin precedes /usr/bin, the desktop default)"
    if os.environ.get("WINELOADER") == want: return "active", "WINELOADER is set in this session (routes every prefix; the shim is preferred)"
    host = shutil.which("wine")
    try:
        if host and Path(host).resolve() == p.build.wine.resolve(): return "active", "the wine on PATH is this wine"
    except OSError: pass
    return "missing", "not configured: a DAW would run plugins with the host's wine and rewrite the prefix"

def broken_bundles() -> list[Path]:
    """yabridge VST3 bundles whose Windows plugin link no longer resolves (the
    plugin was uninstalled, or an update removed it and did not finish)."""
    out = []
    root = Path.home() / ".vst3/yabridge"
    if not root.is_dir(): return out
    for bundle in sorted(root.glob("*.vst3")):
        win = bundle / "Contents/x86_64-win"
        links = list(win.iterdir()) if win.is_dir() else []
        if links and any(l.is_symlink() and not l.exists() for l in links): out.append(bundle)
    return out

def bridged(p: Prefix) -> list[dict]:
    """Parsed `yabridgectl status`, limited to plugin dirs inside this prefix
    (yabridge's config is global and may list other prefixes), deduplicated by plugin name."""
    out, seen, cur_in_prefix = [], set(), False
    root = str(p.drive_c.resolve())
    for line in status(p).splitlines():
        if not line.startswith(" ") and line.rstrip().endswith("/"):
            cur_in_prefix = str(Path(line.strip()).resolve()).startswith(root); continue
        if cur_in_prefix and "::" in line:
            name, info = (x.strip() for x in line.split("::", 1))
            if name in seen: continue
            seen.add(name); out.append({"name": name, "info": info})
    return out
