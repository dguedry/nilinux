"""Import an existing Bottles/Wine setup (Native Access + NI products) into the
app's prefix, so users who started with the manual playbook keep their
installs, licenses, settings and Native Access login."""
import os, re, shutil, subprocess, time
from pathlib import Path
from .progress import null_reporter
from .wine import Prefix
from . import yabridge

BOTTLE_ROOTS = [
    Path.home() / ".var/app/com.usebottles.bottles/data/bottles/bottles",   # Bottles flatpak
    Path.home() / ".local/share/bottles/bottles",                            # Bottles native
    Path.home() / ".local/share/nilinux",                                    # this app run outside Flatpak ("prefix")
    Path.home() / ".var/app/io.github.dguedry.nilinux/data/nilinux",       # this app as a Flatpak
    Path.home() / ".var/app/org.nilinux.NILinux/data/nilinux",             # pre-rename Flatpak id
]
# Program Files entries that are Wine's own or ours — everything else (vendor
# folders such as "ACE Studio") is copied too, so third-party plugins survive.
PF_SKIP = {"Common Files", "Internet Explorer", "Windows Media Player", "Windows NT", "Windows Photo Viewer",
           "Windows Defender", "Native Instruments", "Steinberg", "VstPlugins"}

def find_bottles() -> list[Path]:
    """Bottles (or plain prefixes) that contain a Native Access install."""
    out = []
    for root in BOTTLE_ROOTS:
        if root.is_dir():
            for b in sorted(root.iterdir()):
                if (b / "drive_c/Program Files/Native Instruments/Native Access/Native Access.exe").exists(): out.append(b)
    return out

def _src_user(src_drive_c: Path) -> Path | None:
    users = src_drive_c / "users"
    for cand in ("steamuser", os.environ.get("USER", "")):
        if cand and (users / cand).is_dir(): return users / cand
    for u in users.iterdir():
        if u.is_dir() and u.name != "Public": return u
    return None

# (relative to drive_c) -> exclude names; copied dirs_exist_ok
COPY_DIRS = [
    ("Program Files/Native Instruments", {"Native Access", "Native Access.old-3.23", "Native Access.prev", "Native Access.new"}),
    ("Program Files/Common Files/Native Instruments", {"NTKDaemon"}),
    ("Program Files/Common Files/VST3", set()), ("Program Files/Common Files/VST2", set()),
    ("Program Files/Common Files/CLAP", set()), ("Program Files/Common Files/Steinberg", set()),
    ("Program Files/VstPlugins", set()), ("Program Files/Steinberg", set()),
    ("users/Public/Documents", {"Logs"}),
]
USER_DIRS = [  # relative to the user profile
    ("AppData/Roaming/Native Instruments", {"Cache", "GPUCache", "Code Cache", "DawnGraphiteCache", "DawnWebGPUCache", "blob_storage", "Crashpad", "lockfile"}),
    ("AppData/Local/Native Instruments", set()),
    ("Documents/Native Instruments", set()),
]

def _copy_tree(src: Path, dst: Path, exclude: set[str], reporter, label: str, counter: list[int]):
    def ignore(d, names):
        return [n for n in names if n in exclude and Path(d) == src]
    def copy(s, d):
        if os.path.exists(d) and os.path.getsize(d) == os.path.getsize(s) and int(os.path.getmtime(d)) >= int(os.path.getmtime(s)): return d
        counter[0] += 1
        if counter[0] % 200 == 0: reporter.progress(counter[0], None, label); reporter.log(f"{counter[0]} files… {Path(s).name}")
        return shutil.copy2(s, d)
    shutil.copytree(src, dst, ignore=ignore, copy_function=copy, dirs_exist_ok=True)

def hive_keys(reg_file: Path, prefixes=("Software\\\\Native Instruments\\\\", "Software\\\\VST")) -> dict[str, dict[str, tuple[str, str]]]:
    """Parse a Wine .reg hive: key -> {value: (type, data)} for matching key prefixes."""
    out: dict[str, dict[str, tuple[str, str]]] = {}; cur = None
    for line in reg_file.read_text(errors="ignore").splitlines():
        m = re.match(r"^\[(.+)\] \d+$", line)
        if m:
            k = m.group(1); cur = k if any(k.startswith(pfx) for pfx in prefixes) else None
            if cur: out.setdefault(cur.replace("\\\\", "\\"), {})
            continue
        if not cur or not line.startswith('"'): continue
        mv = re.match(r'^"((?:[^"\\]|\\.)+)"=(.*)$', line)
        if not mv: continue
        name, raw = mv.group(1).replace('\\"', '"'), mv.group(2)
        if raw.startswith('"'): out[cur.replace("\\\\", "\\")][name] = ("REG_SZ", raw[1:-1].replace("\\\\", "\\").replace('\\"', '"'))
        elif raw.startswith("dword:"): out[cur.replace("\\\\", "\\")][name] = ("REG_DWORD", str(int(raw[6:], 16)))
        elif raw.startswith("str(2):"): out[cur.replace("\\\\", "\\")][name] = ("REG_EXPAND_SZ", raw[8:-1].replace("\\\\", "\\"))
        # binary/multi-string values are skipped (NI keys don't use them)
    return out

def import_bottle(p: Prefix, bottle: Path, reporter=None, retire_yabridge_dirs=True) -> dict:
    r = null_reporter(reporter)
    src = Path(bottle) / "drive_c"
    if not src.is_dir(): raise FileNotFoundError(f"no drive_c under {bottle}")
    if src.resolve() == p.drive_c.resolve(): raise ValueError("source is the app prefix itself")
    su = _src_user(src); du = p.user_dir
    r.step("Stopping Native Access in the app prefix")
    p.kill_exe("Native Access.exe"); r.ok()
    counter = [0]
    dirs = list(COPY_DIRS)
    pf = src / "Program Files"
    if pf.is_dir():
        dirs += [(f"Program Files/{d.name}", set()) for d in sorted(pf.iterdir()) if d.is_dir() and d.name not in PF_SKIP]
    for rel, excl in dirs:
        s = src / rel
        if not s.is_dir() or s.resolve() == (p.drive_c / rel).resolve(): continue
        r.step(f"Copying {rel}")
        _copy_tree(s, p.drive_c / rel, excl, r, rel, counter); r.ok()
    if su:
        for rel, excl in USER_DIRS:
            s = su / rel
            if not s.is_dir(): continue
            r.step(f"Copying user data: {rel}")
            _copy_tree(s, du / rel, excl, r, rel, counter); r.ok()
    r.step("Importing registry keys")
    old_user = f"C:\\users\\{su.name}" if su else None; new_user = p.to_win(du)
    n = 0; batch: dict[str, dict[str, tuple[str, str]]] = {}
    for hive, root in (("system.reg", "HKLM"), ("user.reg", "HKCU")):
        f = Path(bottle) / hive
        if not f.exists(): continue
        for key, vals in hive_keys(f).items():
            if key.split("\\")[-1] in ("Native Access", "NTKDaemon"): continue   # ours are already right
            out = {}
            for name, (kind, data) in vals.items():
                if old_user and isinstance(data, str): data = data.replace(old_user, new_user)
                out[name] = (kind, data); n += 1
            if out: batch[f"{root}\\{key}"] = out
    rc = p.reg_import_values(batch, "nilinux-import.reg")   # one regedit call instead of one wine process per value
    (r.ok if rc == 0 else r.fail)(f"{n} values")
    retired = []
    if retire_yabridge_dirs and yabridge.YCTL.exists():
        r.step("Retiring the bottle's plugin directories from yabridge")
        for d in yabridge.foreign_dirs(p):
            if str(Path(d).resolve()).startswith(str(src.resolve())):
                subprocess.run([str(yabridge.YCTL), "rm", d], capture_output=True); retired.append(d)
        r.ok(f"{len(retired)} removed")
    return {"files": counter[0], "registry_values": n, "yabridge_retired": retired}
