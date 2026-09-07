"""Health checks: which fixes are applied, what is missing, what to run."""
import shutil, subprocess
from dataclasses import dataclass
from pathlib import Path
from . import paths, wine, native_access as na, yabridge, products

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""        # CLI command that repairs it

def run(p: wine.Prefix | None = None) -> list[Check]:
    c: list[Check] = []
    for tool in ("7z", "cabextract"):
        c.append(Check(f"host tool: {tool}", bool(shutil.which(tool)), fix=f"install {tool} with your package manager"))
    try: import olefile; c.append(Check("python: olefile", True))
    except ImportError: c.append(Check("python: olefile", False, fix="pip install olefile"))
    b = wine.installed_build()
    c.append(Check("wine build", b is not None, b.version() if b else "not provisioned", fix="nilinux setup"))
    if p is None:
        if b is None: return c
        p = wine.Prefix(paths.PREFIX, b)
    c.append(Check("prefix", p.exists, str(p.path), fix="nilinux setup"))
    if not p.exists: return c
    s = na.status(p)
    c.append(Check("prefix prepared (fonts, C runtime)", s["prepared"], fix="nilinux setup"))
    c.append(Check("Native Access installed", s["installed"], s["version"] or "",
                   fix=f"download from {na.NA_DOWNLOAD_PAGE}, then: nilinux install-na <file>"))
    if s["installed"]:
        c.append(Check("stack patch (64MB)", s["stack_patch"], fix="nilinux launch (re-applies)"))
        c.append(Check("fonts + substitutes", s["fonts"], fix="nilinux setup"))
        c.append(Check("real ucrtbase.dll", s["ucrtbase"], fix="nilinux setup"))
        c.append(Check("VC++ 2022 runtime", s["vc_runtime"], fix="nilinux setup"))
        c.append(Check("NTK Daemon installed", s["ntk_daemon"], s["ntk_version"] or "", fix="nilinux setup"))
        c.append(Check("dependency-check patch", s["dependency_patch"], fix="nilinux setup"))
        c.append(Check("NA self-updater disabled", s["self_update_disabled"], "updates only via a downloaded installer", fix="nilinux setup"))
        v = na.version_notice(p)
        if v["newer"]:
            c.append(Check("Native Access version", True, f"{v['installed']} installed; {v['latest']} available "
                           + ("(validated)" if v["latest_known_good"] else "(not yet validated on this stack)")))
        if s["pending_update"]:
            c.append(Check("no pending legacy self-update", False, "apply with: nilinux update, or ignore", fix="nilinux update"))
    # the NTK daemon binds fixed localhost ports; a daemon from another prefix
    # (any other Wine prefix running Native Access) blocks ours with "Address in use"
    import socket
    busy = []
    for port in na.NTK_PORTS:
        with socket.socket() as sk:
            try: sk.bind(("127.0.0.1", port))
            except OSError: busy.append(port)
    # a daemon we cannot see as a process (another Flatpak instance of this app)
    # still counts as ours when this prefix's wineserver is up
    ours = p.is_running("NTKDaemon.exe") or (bool(busy) and p.wineserver_running())
    c.append(Check("NTK Daemon ports free or ours", ours or not busy,
                   "daemon running" if ours else (f"ports {busy} held by another process (another prefix's daemon?)" if busy else "daemon not running (starts with Native Access)"),
                   fix="stop the other Native Access / NTKDaemon, then nilinux launch"))
    # Wine's audio driver speaks the PulseAudio protocol; PipeWire serves that
    # socket too. Without it, standalone NI apps are silent (DAW use is unaffected).
    import os
    pulse = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "pulse" / "native"
    c.append(Check("audio server (PulseAudio/PipeWire socket)", pulse.exists(),
                   str(pulse) if pulse.exists() else "no Pulse/PipeWire socket: standalone NI apps will have no sound (plugins in a DAW are unaffected)",
                   fix="install and start pipewire-pulse (or pulseaudio)"))
    unreg = [x.name for x in products.installed(p) if x.type == "Content" and not x.registered]
    c.append(Check("libraries registered for Kontakt", not unreg, ", ".join(unreg), fix="nilinux register"))
    yv = yabridge.installed()
    c.append(Check("yabridge", yv is not None, yv or "", fix="nilinux sync"))
    host = shutil.which("wine")
    if host and b:
        hv = subprocess.run([host, "--version"], capture_output=True, text=True).stdout.strip()
        c.append(Check("DAW wine == app wine", Path(host).resolve() == b.wine.resolve() or yabridge.daw_environment_file().exists(),
                       f"PATH wine is {hv}; DAWs will use it unless WINELOADER is set", fix="nilinux sync --daw-env"))
    return c
