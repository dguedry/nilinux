"""Letting Native Access finish a browser login.

Native Access signs in through your browser, which redirects back to a
`native-access://...` URL. Inside the prefix Wine registers that scheme for
Native Access.exe, but nothing registers it with the *desktop*, so the browser
has nowhere to send the callback: the login never completes and Native Access
sits waiting. (Reported by a user, who worked around it with a hand-written
handler script.)

This installs a tiny handler on the host -- a .desktop file claiming the scheme
and a script that passes the URL to Native Access inside this prefix. Both live
under ~/.local, need no root, and are removed by `unregister()`.
"""
from __future__ import annotations

import os, shutil, subprocess
from pathlib import Path

from .progress import null_reporter
from .wine import Prefix

SCHEME = "x-scheme-handler/native-access"
APPS = Path.home() / ".local/share/applications"
DESKTOP = APPS / "io.github.dguedry.nilinux.url-handler.desktop"
SCRIPT = Path.home() / ".local/bin/nilinux-url-handler"
MARK = "# nilinux url handler"

def script_content(p: Prefix) -> str:
    """The handler. It runs on the host (the .desktop file is a host file), so it
    calls this prefix's wine directly rather than going back through the app."""
    return f'''#!/bin/sh
{MARK}
# Native Access signs in via the browser, which calls back to native-access://...
# Hand that URL to Native Access inside the prefix this app manages.
[ -n "$1" ] || exit 0
WINEPREFIX="{p.path}"
export WINEPREFIX
exec "{p.build.wine}" "{p.drive_c}/Program Files/Native Instruments/Native Access/Native Access.exe" "$1"
'''

DESKTOP_CONTENT = f"""[Desktop Entry]
Type=Application
Name=Native Access login callback
Comment=Passes native-access:// sign-in links to Native Access (NI on Linux)
Exec={SCRIPT} %u
NoDisplay=true
Terminal=false
MimeType=x-scheme-handler/native-access;
"""

def _ours(path: Path) -> bool:
    try: return MARK in path.read_text(errors="replace")
    except OSError: return False

def status(p: Prefix | None = None) -> dict:
    """Is the handler installed, and is the desktop actually using it?"""
    default = ""
    try:
        default = subprocess.run(["xdg-mime", "query", "default", SCHEME],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError): pass
    installed = SCRIPT.exists() and DESKTOP.exists() and _ours(SCRIPT)
    current = (not default) or default == DESKTOP.name
    return {"installed": installed, "default": default, "is_default": default == DESKTOP.name,
            "ok": installed and default == DESKTOP.name,
            "foreign": bool(default) and default != DESKTOP.name}

def register(p: Prefix, reporter=None) -> bool:
    """Install the handler and make it the default for native-access:// links."""
    r = null_reporter(reporter)
    r.step("Browser sign-in returns to Native Access (native-access:// handler)")
    st = status(p)
    if st["foreign"]:
        # Someone else claims the scheme -- very likely the user's own workaround.
        # Replacing it is the point, but say so rather than doing it silently.
        r.log(f"replacing the existing handler for native-access:// ({st['default']})")
    try:
        SCRIPT.parent.mkdir(parents=True, exist_ok=True)
        APPS.mkdir(parents=True, exist_ok=True)
        want = script_content(p)
        if not SCRIPT.exists() or SCRIPT.read_text(errors="replace") != want:
            SCRIPT.write_text(want)
        SCRIPT.chmod(0o755)
        if not DESKTOP.exists() or DESKTOP.read_text(errors="replace") != DESKTOP_CONTENT:
            DESKTOP.write_text(DESKTOP_CONTENT)
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(APPS)], capture_output=True, timeout=30)
        if shutil.which("xdg-mime"):
            subprocess.run(["xdg-mime", "default", DESKTOP.name, SCHEME], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        r.fail(str(e)[:100]); return False
    after = status(p)
    if after["ok"]: r.ok("registered")
    elif after["installed"]: r.ok("installed (the desktop still lists another handler)")
    else: r.fail("could not register")
    return after["installed"]

def unregister(reporter=None) -> bool:
    r = null_reporter(reporter)
    r.step("Removing the native-access:// handler")
    removed = False
    for f in (SCRIPT, DESKTOP):
        if f.exists() and (f is DESKTOP or _ours(f)):
            try: f.unlink(); removed = True
            except OSError as e: r.fail(str(e)[:80]); return False
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(APPS)], capture_output=True, timeout=30)
    r.ok("removed" if removed else "nothing to remove")
    return removed
