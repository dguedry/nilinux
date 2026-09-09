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
    try: return subprocess.run([str(YCTL), "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception: return "unknown"

def install(reporter=None) -> str:
    r = null_reporter(reporter)
    if YCTL.exists(): return installed() or "present"
    r.step("Installing yabridge (latest release)")
    rel = json.loads(text("https://api.github.com/repos/robbert-vdh/yabridge/releases/latest"))
    url = next(a["browser_download_url"] for a in rel["assets"]
               if re.fullmatch(r"yabridge-[0-9.]+\.tar\.gz", a["name"]))
    tgz = fetch(url, paths.DOWNLOADS / Path(url).name, reporter=r, label="yabridge")
    YAB_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz) as t: t.extractall(YAB_DIR.parent, filter="tar")
    (Path.home() / ".local/bin").mkdir(parents=True, exist_ok=True)
    link = Path.home() / ".local/bin/yabridgectl"
    if not link.exists(): link.symlink_to(YCTL)
    r.ok(rel["tag_name"]); return rel["tag_name"]

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
    r.step("Syncing yabridge")
    cp = subprocess.run([str(YCTL), "sync", "--prune"], capture_output=True, text=True, env=env, timeout=1800)
    summary = next((l for l in cp.stdout.splitlines() if l.startswith("Finished")), "")
    if not summary: summary = (cp.stderr or cp.stdout).strip().splitlines()[-1:] or [""]; summary = summary[0][:160]
    (r.ok if cp.returncode == 0 else r.fail)(summary)
    for l in cp.stdout.splitlines():
        if l.startswith("WARNING"): r.log(l)
    return {"dirs": [str(d) for d in dirs], "returncode": cp.returncode, "output": cp.stdout, "summary": summary}

def status(p: Prefix) -> str:
    if not YCTL.exists(): return "yabridge not installed"
    return subprocess.run([str(YCTL), "status"], capture_output=True, text=True, env=_yctl_env(p), timeout=120).stdout

def daw_environment_file() -> Path:
    return Path.home() / ".config/environment.d/50-nilinux.conf"

WINE_SHIM = Path.home() / ".local/bin/wine"
SHIM_MARK = "# nilinux: DAWs run yabridge plugins with the app's wine (same build as the prefix)"

def daw_environment_content(p: Prefix) -> str:
    return f"WINELOADER={p.build.wine}\nWINEFSYNC=1\n"

def shim_content(p: Prefix) -> str:
    return f'#!/bin/sh\n{SHIM_MARK}\nexport WINEFSYNC=1\nexec "{p.build.wine}" "$@"\n'

def shim_is_ours(p: Prefix) -> bool:
    f = WINE_SHIM
    try:
        if f.is_symlink(): return f.resolve() == p.build.wine.resolve()
        return f.is_file() and SHIM_MARK in f.read_text() and str(p.build.wine) in f.read_text()
    except OSError: return False

def configure_daw_environment(p: Prefix, reporter=None) -> bool:
    """Make DAWs run yabridge plugins with the app's wine, two ways:
    1. ~/.local/bin/wine shim — yabridge resolves `wine` through PATH, and
       ~/.local/bin precedes /usr/bin on Debian/Ubuntu/Mint/Fedora desktops.
       Effective for every wine started from now on, no re-login, any desktop.
    2. WINELOADER in systemd user environment.d — for desktops that import it
       (GNOME); a belt-and-braces for PATHs without ~/.local/bin.
    Returns True if anything was written."""
    r = null_reporter(reporter)
    r.step("DAWs use this wine (~/.local/bin/wine shim, WINELOADER)")
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
    f = daw_environment_file(); want = daw_environment_content(p)
    if not (f.exists() and f.read_text() == want):
        f.parent.mkdir(parents=True, exist_ok=True); f.write_text(want); changed = True
    (r.ok if changed else r.skip)(f"{shim} -> {p.build.wine.name}" if changed else "configured")
    return changed

def daw_environment_status(p: Prefix) -> tuple[str, str]:
    """('active' | 'pending' | 'missing', detail).
    active:  wine started by a DAW is this wine (shim in ~/.local/bin, WINELOADER
             in this session, or the wine on PATH *is* this wine)
    pending: only environment.d is written and the session predates it
    missing: not configured — a DAW would use the host's wine, whose prefix
             update rewrites the app's prefix with another Wine's DLLs"""
    want = str(p.build.wine)
    if shim_is_ours(p): return "active", f"{WINE_SHIM} runs this wine (assumes ~/.local/bin precedes /usr/bin on PATH, the desktop default)"
    if os.environ.get("WINELOADER") == want: return "active", "WINELOADER is set in this session"
    host = shutil.which("wine")
    try:
        if host and Path(host).resolve() == p.build.wine.resolve(): return "active", "the wine on PATH is this wine"
    except OSError: pass
    f = daw_environment_file()
    if f.exists() and f.read_text() == daw_environment_content(p):
        return "pending", "WINELOADER configured for the next login (no ~/.local/bin shim); log out and back in, then start your DAW"
    return "missing", "not configured: a DAW would run plugins with the host's wine and rewrite the prefix"

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
