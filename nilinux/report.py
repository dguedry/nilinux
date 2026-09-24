"""A diagnostic bundle a user can send when something goes wrong.

Support by conversation is slow: the questions that matter (which wine, which
Native Access, which prefix, what the installer did before it stopped) take
several round trips to ask. This collects them once into a single file.

What it deliberately does NOT collect: serial numbers, licence tokens (the
ras3 *.jwt files), the user's own file names, or anything from outside the
prefix and this app's own logs. Paths under $HOME are shortened to ~ so a
username does not travel with the report either.
"""
from __future__ import annotations

import io, json, os, platform, re, shutil, subprocess, tarfile, time
from pathlib import Path

from . import paths, wine, host

REDACT = [
    (re.compile(r"\b[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}\b"), "<serial>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "<token>"),
]

def scrub(text: str) -> str:
    """Remove the identifying things NI's software writes into its logs."""
    home = str(Path.home())
    text = text.replace(home, "~")
    for rx, sub in REDACT: text = rx.sub(sub, text)
    return text

def _run(cmd: list[str], timeout=20) -> str:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (cp.stdout + cp.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"({cmd[0]}: {e})"

def summary(p: wine.Prefix | None = None) -> str:
    """The human-readable part: what this machine is and what state the app is in."""
    from . import native_access as na, yabridge, products, prefixes, doctor, dxvk, __version__
    out = io.StringIO()
    w = out.write
    w(f"nilinux {__version__} report — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    w("=" * 60 + "\n\n")

    w("## Machine\n")
    w(f"python        {platform.python_version()}\n")
    w(f"kernel        {platform.release()}\n")
    try:
        osr = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text(errors="replace").splitlines() if "=" in l)
        w(f"distribution  {osr.get('PRETTY_NAME', '?').strip('\"')}\n")
    except OSError: pass
    w(f"flatpak       {'yes' if host.in_flatpak() else 'no'}\n")
    ok, detail = host.available()
    w(f"host access   {'yes' if ok else 'no'} ({detail})\n")
    w(f"session       {os.environ.get('XDG_SESSION_TYPE', '?')} / {os.environ.get('XDG_CURRENT_DESKTOP', '?')}\n")
    w(f"vulkan        {dxvk.vulkan_ok()[1]}\n\n")

    b = wine.installed_build()
    if p is None and b is not None: p = wine.Prefix(paths.PREFIX, b)
    w("## Wine and prefix\n")
    # report the wine the prefix actually uses, which is not paths.WINE_DIR when the
    # report is run against a Flatpak prefix from outside the sandbox
    build = p.build if p is not None and getattr(p, "build", None) is not None else b
    ver = None
    if build is not None:
        try: ver = build.version()
        except Exception: ver = None
    w(f"wine build    {ver or (str(build.root).replace(str(Path.home()), '~') if build else 'not provisioned')}\n")
    if p is not None:
        w(f"prefix        {str(p.path).replace(str(Path.home()), '~')} (exists: {p.exists})\n")
        others = prefixes.other_prefixes(p) if p.exists else []
        if others: w(f"other prefixes {', '.join(prefixes.short(o) for o in others)}\n")
    w("\n")

    if p is not None and p.exists:
        w("## Native Access\n")
        try:
            s = na.status(p)
            for k in ("installed", "version", "stack_patch", "fonts", "ucrtbase", "vc_runtime",
                      "ntk_daemon", "ntk_version", "dependency_patch", "self_update_disabled"):
                w(f"{k:<22} {s.get(k)}\n")
        except Exception as e: w(f"(failed: {e})\n")
        w("\n## yabridge\n")
        try:
            w(f"installed     {yabridge.installed()}\n")
            w(f"compatible    {yabridge.compatibility()}\n")
            broken = yabridge.broken_bundles()
            w(f"broken links  {[b.name for b in broken] if broken else 'none'}\n")
        except Exception as e: w(f"(failed: {e})\n")
        w("\n## DXVK\n")
        try: w(json.dumps(dxvk.status(p), indent=2) + "\n")
        except Exception as e: w(f"(failed: {e})\n")
        w("\n## Installed products\n")
        try:
            for x in products.installed(p)[:60]:
                w(f"  {x.name}  [{x.type}]  {'registered' if x.registered else 'NOT registered'}\n")
        except Exception as e: w(f"(failed: {e})\n")
        w("\n## Interrupted installs (staged payloads)\n")
        try:
            st = products.staged_installs(p)
            if not st: w("  none\n")
            for x in st:
                size = sum(f.stat().st_size for f in x.dir.rglob("*") if f.is_file()) // (1024 * 1024)
                w(f"  {x.name}: {size} MB staged in {x.dir.name}, download {'kept' if x.download else 'gone'}\n")
        except Exception as e: w(f"(failed: {e})\n")
        w("\n## Running in this prefix\n")
        try:
            procs = p.processes()
            if not procs: w("  nothing\n")
            for pid, cmd in procs[:30]: w(f"  {pid}  {cmd[:110]}\n")
        except Exception as e: w(f"(failed: {e})\n")

    w("\n## Health checks\n")
    try:
        for c in doctor.run(p):
            w(f"  [{'ok ' if c.ok else 'FAIL'}] {c.name}{(': ' + c.detail) if c.detail else ''}\n")
            if not c.ok and c.fix: w(f"         fix: {c.fix}\n")
    except Exception as e: w(f"(failed: {e})\n")
    return scrub(out.getvalue())

def write_bundle(dest: Path | None = None, p: wine.Prefix | None = None) -> Path:
    """Write summary + logs to a .tar.gz the user can attach to a bug report."""
    paths.ensure_dirs()
    dest = Path(dest) if dest else Path.home() / f"nilinux-report-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    text = summary(p)
    with tarfile.open(dest, "w:gz") as t:
        data = text.encode()
        info = tarfile.TarInfo("report/summary.txt"); info.size = len(data); info.mtime = int(time.time())
        t.addfile(info, io.BytesIO(data))
        # this app's own logs, scrubbed; tail only, they can be large
        for log in sorted(paths.LOGS.glob("*")) if paths.LOGS.is_dir() else []:
            if not log.is_file(): continue
            try: body = scrub(log.read_text(errors="replace"))[-400_000:]
            except OSError: continue
            b = body.encode()
            i = tarfile.TarInfo(f"report/logs/{log.name}"); i.size = len(b); i.mtime = int(time.time())
            t.addfile(i, io.BytesIO(b))
        # Native Access's own log, which says what it was installing
        if p is not None and p.exists:
            na_log = p.drive_c / "users/Public/Documents/Native Instruments/Logs/Native Access/native-access.log"
            if na_log.is_file():
                try:
                    b = scrub(na_log.read_text(errors="replace"))[-400_000:].encode()
                    i = tarfile.TarInfo("report/logs/native-access.log"); i.size = len(b); i.mtime = int(time.time())
                    t.addfile(i, io.BytesIO(b))
                except OSError: pass
    return dest
