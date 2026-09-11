"""Health checks: which fixes are applied, what is missing, what to run."""
import shutil, subprocess
from dataclasses import dataclass
from pathlib import Path
from . import paths, wine, native_access as na, yabridge, products, prefixes

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
    foreign = p.foreign_dlls()
    c.append(Check("prefix files from this wine", not foreign,
                   f"{', '.join(foreign)} were written by another Wine (a DAW using the host wine?)" if foreign else "",
                   fix="nilinux setup (refreshes them); then 'Make DAWs use this wine'"))
    # NI's setup exes are 32-bit; inside a Flatpak without --allow=multiarch every
    # 32-bit program fails at once ("Application could not be started", daemon error 731)
    cp = p.run([r"C:\windows\syswow64\cmd.exe", "/c", "echo ok"], timeout=120)
    c.append(Check("32-bit programs run (WoW64)", cp.returncode == 0 and "ok" in cp.stdout,
                   "" if cp.returncode == 0 else "cannot start a 32-bit program: NI installers will fail (Flatpak: needs --allow=multiarch)",
                   fix="reinstall the current Flatpak build"))
    loc, ok = na.download_location_status(p)
    c.append(Check("NA download location writable", ok, loc if ok else (f"{loc}: not writable from here" if loc else "unset: every download fails"),
                   fix="nilinux launch (re-applies) or nilinux setup"))
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
    # The NTK daemon binds fixed localhost ports, one daemon per machine. A daemon
    # from another prefix holding them means plugins bridged from *this* prefix talk
    # to the wrong daemon and hang in every DAW; a wineserver of ours being up is no
    # proof the daemon is ours, so look at the listener's WINEPREFIX.
    owner = prefixes.ntk_port_owner(na.NTK_PORTS)
    if not owner["busy"]:
        c.append(Check("NTK Daemon ports free or ours", True, "daemon not running (starts with Native Access or the first plugin)"))
    elif owner["prefix"] and Path(owner["prefix"]).resolve() == p.path.resolve():
        c.append(Check("NTK Daemon ports free or ours", True, f"{owner['exe'] or 'daemon'} running in this prefix"))
    elif owner["prefix"]:
        c.append(Check("NTK Daemon ports free or ours", False,
                       f"ports {owner['busy']} held by {owner['exe']} (pid {owner['pid']}) of another prefix: {prefixes.short(owner['prefix'])} -- plugins bridged from this prefix will hang",
                       fix="quit Native Access / the DAW using that prefix, or run that prefix's nilinux instead"))
    else:
        ours = p.is_running("NTKDaemon.exe") or p.wineserver_running()
        c.append(Check("NTK Daemon ports free or ours", ours,
                       "daemon running (holder not visible from this sandbox)" if ours else f"ports {owner['busy']} held by a process this sandbox cannot see (another prefix's daemon?)",
                       fix="stop the other Native Access / NTKDaemon, then nilinux launch"))
    others = prefixes.other_prefixes(p)
    c.append(Check("single nilinux prefix", not others,
                   "" if not others else "also: " + ", ".join(prefixes.short(o) for o in others) + " -- only one can own the NI daemon; sync keeps the bridges on this one",
                   fix="delete the other prefix folders once you are sure this one is the install to keep"))
    ystat = yabridge.status(p)
    foreign = prefixes.foreign_yabridge_dirs(p, ystat)
    c.append(Check("yabridge lists only this prefix", not foreign,
                   "" if not foreign else f"{len(foreign)} plugin directories of other nilinux prefixes are registered", fix="nilinux sync"))
    broken = yabridge.broken_bundles()
    c.append(Check("bridged plugins point at existing files", not broken,
                   "" if not broken else "missing target for: " + ", ".join(b.name for b in broken) + " (uninstalled, or an update that did not finish)",
                   fix="nilinux finish-installs, then nilinux sync"))
    staged = products.staged_installs(p)
    c.append(Check("no interrupted Native Access installs", not staged,
                   "" if not staged else "left half-done: " + ", ".join(f"{x.name}" + (" (download kept)" if x.download else "") for x in staged),
                   fix="nilinux finish-installs"))
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
    if yv is not None:
        yok, ydetail = yabridge.compatibility()
        c.append(Check("yabridge matches this wine", yok, ydetail, fix="nilinux sync (installs the nilinux-built yabridge for this wine)"))
    st, detail = yabridge.daw_environment_status(p)
    c.append(Check("DAWs run plugins with this wine", st == "active", detail, fix="nilinux setup"))
    return c
