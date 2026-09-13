"""Per-program fixes for Windows programs that need a nudge under Wine.

Each quirk names a program (as the Programs page lists it), says how to tell
whether it applies, and applies a small change. Quirks run after "Install a
Windows program" and before every Run, and are idempotent. They are the
generic sibling of the Native Access fixes in native_access.py: an app that
works everywhere else and fails here for one identifiable reason.
"""
import re
from pathlib import Path
from . import asar
from .progress import null_reporter
from .wine import Prefix

MARK = b"/*NILINUX_QUIRK*/"

# IK Product Manager (Electron 11): its os-info module runs `ver` and expects
# "Microsoft Windows [Version 10.0.19045]"; Wine's cmd prints
# "Microsoft Windows 10.0.19045". The match is null, getVersion throws, and the
# same throw later breaks the app's API requests. Accept a bare version too.
_IK_OLD = rb"version = winVersion.match(/\[([^\]]+)\]/)"
_IK_NEW = rb"version = winVersion.match(/\[([^\]]+)\]/) || winVersion.match(/(\d+\.\d+[\d.]*)/) /*NILINUX_QUIRK*/"

def _ik_os_info(js: bytes):
    if MARK in js: return None
    if js.count(_IK_OLD) != 1: raise LookupError("os-info getVersion not found once")
    return js.replace(_IK_OLD, _IK_NEW, 1)

QUIRKS = {
    "IK Product Manager": [("resources/app.asar", r"local_modules/os-info/index\.js", _ik_os_info,
                            "os-info: accept Wine's `ver` output (no [Version …] brackets)")],
}

def _match(name: str):
    for key, quirks in QUIRKS.items():
        if key.lower() in name.lower(): return quirks
    return []

def apply(p: Prefix, name: str, install_dir: str, reporter=None) -> list[str]:
    """Apply the quirks registered for `name` to the program under install_dir
    (Windows path). Returns what was done."""
    r = null_reporter(reporter); done = []
    for asar_rel, path_rx, fn, what in _match(name):
        a = p.to_host(install_dir) / asar_rel
        if not a.exists(): continue
        r.step(f"Quirk for {name}: {what}")
        try: res = asar.patch(a, {path_rx: fn})
        except LookupError as e: r.fail(f"{e} — this version is not covered"); continue
        st = res[path_rx]
        (r.ok if st == "patched" else r.skip)("patched" if st == "patched" else ("already" if st == "unchanged" else "file not in bundle"))
        if st == "patched": done.append(what)
    return done
