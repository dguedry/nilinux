"""Native Access: download, install, and every Wine fix it needs.

Each public function is idempotent and reports through a Reporter. See the
project README for the diagnosis behind each fix.
"""
import hashlib, json, os, re, shutil, struct, subprocess, tempfile, time
from pathlib import Path
from . import paths
from .download import fetch, text
from .progress import null_reporter
from .wine import Prefix

NA_DOWNLOAD_PAGE = "https://www.native-instruments.com/pages/native-access"
UPDATE_FEED = "https://na-update.native-instruments.com"   # version lookup only; the installer is never downloaded by this app
# winetricks ucrtbase2019 source: last VC2019 redist that still ships ucrtbase.dll
UCRT_URL = "https://download.visualstudio.microsoft.com/download/pr/85d47aa9-69ae-4162-8300-e6b7e4bf3cf3/52B196BBE9016488C735E7B41805B651261FFA5D7AA86EB6A1D0095BE83687B2/VC_redist.x64.exe"
UCRT_SHA256 = "52b196bbe9016488c735e7b41805b651261ffa5d7aa86eb6a1d0095be83687b2"
VC2022_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
STACK_RESERVE = 0x4000000  # 64MB
# NA versions validated on this stack (all fixes apply, window renders, installs work)
KNOWN_GOOD = {"3.23.0", "3.25.2"}
UPDATER_DISABLED_URL = "http://127.0.0.1:1/native-access-self-update-disabled-by-nilinux"
DEP_MARK = "/*NA_DEP_PATCH*/"
NTK_PORTS = (7865, 5563, 5146)   # NTKDaemon listens on 127.0.0.1 — one daemon per machine

NA_REL = Path("Program Files/Native Instruments/Native Access")
NTK_REL = Path("Program Files/Common Files/Native Instruments/NTKDaemon")

def na_dir(p: Prefix) -> Path: return p.drive_c / NA_REL
def na_exe(p: Prefix) -> Path: return na_dir(p) / "Native Access.exe"
def na_roaming(p: Prefix) -> Path: return p.user_dir / "AppData/Roaming/Native Instruments/Native Access"
def na_log(p: Prefix) -> Path: return p.public_docs / "Native Instruments/Logs/Native Access/native-access.log"

def _need(tool):
    if not shutil.which(tool): raise RuntimeError(f"missing host tool: {tool}")

# --- feed / download ------------------------------------------------------------
def feed() -> dict:
    """Parse NA's electron-updater feed (latest.yml) without pyyaml."""
    y = text(f"{UPDATE_FEED}/latest.yml")
    ver = re.search(r"^version:\s*(\S+)", y, re.M)
    path = re.search(r"^path:\s*(\S+)", y, re.M)
    sha = re.search(r"^sha512:\s*(\S+)", y, re.M)
    size = re.search(r"size:\s*(\d+)", y)
    return {"version": ver.group(1), "file": path.group(1), "sha512": sha.group(1), "size": int(size.group(1)) if size else None}

def latest_version() -> str | None:
    """Current NA version from NI's feed, for display only."""
    try: return feed()["version"]
    except Exception: return None

# --- install -----------------------------------------------------------------------
def extract_app(installer: Path, dest_dir: Path):
    """The NSIS package does not run under Wine; pull app-64.7z out of it."""
    _need("7z")
    with tempfile.TemporaryDirectory() as t:
        subprocess.run(["7z", "e", "-y", f"-o{t}", str(installer), "$PLUGINSDIR/app-64.7z"], check=True, capture_output=True)
        app7z = Path(t) / "app-64.7z"
        if not app7z.exists(): raise RuntimeError("installer has no $PLUGINSDIR/app-64.7z")
        subprocess.run(["7z", "x", "-y", f"-o{dest_dir}", str(app7z)], check=True, capture_output=True)
    if not (dest_dir / "Native Access.exe").exists(): raise RuntimeError("extracted app has no Native Access.exe")

def install(p: Prefix, installer: Path, reporter=None, keep_previous=True):
    r = null_reporter(reporter)
    r.step("Installing Native Access application files")
    d = na_dir(p)
    if d.exists():
        prev = d.with_name("Native Access.prev")
        shutil.rmtree(prev, ignore_errors=True)
        d.rename(prev) if keep_previous else shutil.rmtree(d)
    tmp = d.with_name("Native Access.new"); shutil.rmtree(tmp, ignore_errors=True)
    extract_app(installer, tmp); tmp.rename(d)
    r.ok(exe_version(na_exe(p)) or "installed")

def exe_version(exe: Path) -> str | None:
    """FileVersion from the PE VS_FIXEDFILEINFO block (signature 0xFEEF04BD)."""
    try: d = Path(exe).read_bytes()
    except OSError: return None
    key = "VS_VERSION_INFO".encode("utf-16-le")
    i, k = -1, d.find(key)
    while k >= 0:  # signature follows the key within a few padding bytes
        j = d.find(b"\xbd\x04\xef\xfe", k, k + 80)
        if j >= 0: i = j; break
        k = d.find(key, k + 1)
    if i < 0 or i + 24 > len(d): return None
    ms, ls = struct.unpack_from("<II", d, i + 8)
    return f"{ms >> 16}.{ms & 0xffff}.{ls >> 16}" + (f".{ls & 0xffff}" if ls & 0xffff else "")

# --- fix 1: PE stack reserve ---------------------------------------------------------
def stack_patch(exe: Path) -> str:
    """Returns 'patched' | 'already' ."""
    with open(exe, "r+b") as f:
        hdr = f.read(0x400)
        opt = struct.unpack_from("<I", hdr, 0x3c)[0] + 24
        if struct.unpack_from("<H", hdr, opt)[0] != 0x20B: raise RuntimeError("not a PE32+ exe")
        off = opt + 72
        reserve = struct.unpack_from("<Q", hdr, off)[0]
        if reserve >= STACK_RESERVE: return "already"
        bak = exe.with_suffix(".exe.bak")
        if not bak.exists(): shutil.copy2(exe, bak)
        f.seek(off); f.write(struct.pack("<Q", STACK_RESERVE))
        return "patched"

def stack_ok(exe: Path) -> bool:
    try:
        hdr = exe.read_bytes()[:0x400]
        opt = struct.unpack_from("<I", hdr, 0x3c)[0] + 24
        return struct.unpack_from("<Q", hdr, opt + 72)[0] >= STACK_RESERVE
    except Exception: return False

# --- fix 2: fonts + FontSubstitutes -------------------------------------------------------
# Only the four base faces per family: extra same-family faces (Condensed,
# ExtraLight...) poison Wine's font matching and re-trigger the GDI storm.
BASE_STYLES = {"regular", "book", "bold", "italic", "oblique", "bold italic", "bold oblique"}
FAMILIES = {  # family -> required?
    "DejaVu Sans": True, "DejaVu Sans Mono": True, "DejaVu Serif": True,
    "Liberation Sans": True, "Liberation Serif": True, "Liberation Mono": True,
    "Noto Sans Symbols": False, "Noto Sans Symbols2": False, "Noto Color Emoji": False,
}
PURGE = ("DejaVuSansCondensed", "DejaVuSans-ExtraLight", "DejaVuSansMono-Oblique", "DejaVuSansMono-BoldOblique",
         "DejaVuSerifCondensed", "Ubuntu-M", "Ubuntu-MI", "Ubuntu-L", "Ubuntu-LI", "Ubuntu-C", "Ubuntu-Th")
FONT_FALLBACK = {
    "DejaVu": ("https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip", None),
    "Liberation": ("https://github.com/liberationfonts/liberation-fonts/files/7261482/liberation-fonts-ttf-2.1.5.tar.gz", None),
}

def host_fonts() -> dict[str, list[Path]]:
    """family -> base-style font files, via fontconfig."""
    found: dict[str, list[Path]] = {}
    try:
        out = subprocess.run(["fc-list", "--format", "%{file}|%{family}|%{style}\n"], capture_output=True, text=True, timeout=30).stdout
    except Exception: return found
    for line in out.splitlines():
        try: file, fams, styles = line.split("|", 2)
        except ValueError: continue
        fam_set = {f.strip() for f in fams.split(",")}
        style = styles.split(",")[0].strip().lower()
        for fam in FAMILIES:
            if fam in fam_set and file.lower().endswith((".ttf", ".otf")) and (style in BASE_STYLES or fam.startswith("Noto")):
                found.setdefault(fam, []).append(Path(file))
    return found

def _fallback_fonts(reporter, missing: set[str]) -> dict[str, list[Path]]:
    r = null_reporter(reporter); got = {}
    for vendor, (url, sha) in FONT_FALLBACK.items():
        fams = [f for f in missing if f.startswith(vendor)]
        if not fams: continue
        arc = fetch(url, paths.DOWNLOADS / Path(url).name, sha256=sha, reporter=r, label=vendor + " fonts")
        d = paths.DOWNLOADS / (vendor + "-fonts"); shutil.rmtree(d, ignore_errors=True); d.mkdir()
        if arc.suffix == ".zip":
            import zipfile; zipfile.ZipFile(arc).extractall(d)
        else:
            import tarfile; tarfile.open(arc).extractall(d, filter="tar")
        for ttf in d.rglob("*.ttf"):
            stem = ttf.stem
            if any(x in stem for x in PURGE) or "Condensed" in stem or "ExtraLight" in stem: continue
            fam = {"DejaVuSans": "DejaVu Sans", "DejaVuSansMono": "DejaVu Sans Mono", "DejaVuSerif": "DejaVu Serif",
                   "LiberationSans": "Liberation Sans", "LiberationSerif": "Liberation Serif", "LiberationMono": "Liberation Mono"}.get(stem.split("-")[0])
            if fam in fams: got.setdefault(fam, []).append(ttf)
    return got

def fonts(p: Prefix, reporter=None) -> dict:
    r = null_reporter(reporter)
    r.step("Installing fonts into the prefix")
    fdir = p.drive_c / "windows/Fonts"; fdir.mkdir(parents=True, exist_ok=True)
    have = host_fonts()
    missing = {f for f, req in FAMILIES.items() if f not in have}
    if missing & {f for f, req in FAMILIES.items() if req}:
        have.update(_fallback_fonts(r, missing))
    copied = 0
    for fam, files in have.items():
        for f in files:
            if any(x in f.stem for x in PURGE): continue
            dst = fdir / f.name
            if not dst.exists() or dst.stat().st_size != f.stat().st_size:
                shutil.copy2(f, dst); copied += 1
    for f in fdir.iterdir():
        if any(f.name.startswith(x) for x in PURGE): f.unlink()
    still = [f for f, req in FAMILIES.items() if req and f not in have]
    if still: r.fail(f"missing required fonts: {', '.join(still)}"); return {"missing": still}
    r.ok(f"{len(list(fdir.iterdir()))} fonts, {copied} new")
    return {"families": sorted(have), "missing": sorted(missing)}

def registry(p: Prefix, reporter=None, have_noto: bool | None = None):
    r = null_reporter(reporter)
    r.step("Font substitutes and DLL overrides")
    fdir = p.drive_c / "windows/Fonts"
    noto_sym = (fdir / "NotoSansSymbols-Regular.ttf").exists()
    noto_sym2 = (fdir / "NotoSansSymbols2-Regular.ttf").exists()
    noto_emoji = (fdir / "NotoColorEmoji.ttf").exists()
    subs = {
        "Segoe UI": "DejaVu Sans", "Segoe UI Light": "DejaVu Sans", "Segoe UI Semibold": "DejaVu Sans",
        "Segoe UI Semilight": "DejaVu Sans", "Segoe UI Black": "DejaVu Sans",
        "Segoe UI Symbol": "Noto Sans Symbols" if noto_sym else "DejaVu Sans",
        "Segoe UI Emoji": "Noto Color Emoji" if noto_emoji else "DejaVu Sans",
        "Segoe MDL2 Assets": "Noto Sans Symbols2" if noto_sym2 else "DejaVu Sans",
        "Segoe Fluent Icons": "Noto Sans Symbols2" if noto_sym2 else "DejaVu Sans",
        "Tahoma": "DejaVu Sans", "Verdana": "DejaVu Sans", "Microsoft Sans Serif": "DejaVu Sans",
        "Calibri": "Liberation Sans", "Cambria": "Liberation Serif", "Consolas": "DejaVu Sans Mono",
    }
    dlls = ["ucrtbase", "msvcp140", "msvcp140_1", "msvcp140_2", "msvcp140_atomic_wait", "msvcp140_codecvt_ids",
            "vcruntime140", "vcruntime140_1", "concrt140", "vcomp140"]
    reg = ["Windows Registry Editor Version 5.00", "",
           r"[HKEY_LOCAL_MACHINE\Software\Microsoft\Windows NT\CurrentVersion\FontSubstitutes]"]
    reg += [f'"{k}"="{v}"' for k, v in subs.items()]
    reg += ["", r"[HKEY_CURRENT_USER\Software\Wine\DllOverrides]"] + [f'"{d}"="native,builtin"' for d in dlls] + [""]
    rc = p.reg_import("\r\n".join(reg), "nilinux-setup.reg")
    if rc != 0: r.fail(f"regedit exit {rc}")
    else: r.ok()

# --- fix 3: real C runtime ---------------------------------------------------------------------
def vc_runtime(p: Prefix, reporter=None):
    r = null_reporter(reporter); _need("cabextract"); _need("7z")
    s32 = p.drive_c / "windows/system32"
    r.step("Installing real ucrtbase.dll (VC2019 redist)")
    if (s32 / "ucrtbase.dll.wine-builtin.bak").exists() and (s32 / "ucrtbase.dll").stat().st_size > 900_000:
        r.skip("present")
    else:
        vc19 = fetch(UCRT_URL, paths.DOWNLOADS / "VC_redist2019.x64.exe", sha256=UCRT_SHA256, reporter=r, label="VC2019")
        with tempfile.TemporaryDirectory() as t:
            subprocess.run(["cabextract", "-q", "-d", t, "-F", "a10", str(vc19)], check=True)
            subprocess.run(["cabextract", "-q", "-d", t, "-F", "ucrtbase.dll", f"{t}/a10"], check=True)
            src = Path(t) / "ucrtbase.dll"
            if not src.exists(): raise RuntimeError("ucrtbase.dll not found in VC2019 redist")
            bak = s32 / "ucrtbase.dll.wine-builtin.bak"
            if not bak.exists() and (s32 / "ucrtbase.dll").exists(): shutil.copy2(s32 / "ucrtbase.dll", bak)
            shutil.copy2(src, s32 / "ucrtbase.dll")
        r.ok()
    r.step("Installing matched VC++ 2022 x64 runtime")
    if (s32 / "vcruntime140.dll.bak").exists() and (s32 / "msvcp140.dll.bak").exists():
        r.skip("present")
    else:
        vc22 = fetch(VC2022_URL, paths.DOWNLOADS / "VC_redist2022.x64.exe", reporter=r, label="VC2022")
        with tempfile.TemporaryDirectory() as t:
            subprocess.run(["cabextract", "-q", "-d", t, str(vc22)], capture_output=True)
            cab = None
            for c in sorted(Path(t).glob("a*")):
                l = subprocess.run(["7z", "l", str(c)], capture_output=True, text=True).stdout
                if "vcruntime140.dll_amd64" in l: cab = c; break
            if cab is None: raise RuntimeError("x64 runtime cab not found in VC2022 redist")
            rt = Path(t) / "rt"; rt.mkdir()
            subprocess.run(["cabextract", "-q", "-d", str(rt), str(cab)], check=True)
            n = 0
            for f in rt.glob("*_amd64"):
                dll = f.name[: -len("_amd64")]
                if (s32 / dll).exists() and not (s32 / (dll + ".bak")).exists(): shutil.copy2(s32 / dll, s32 / (dll + ".bak"))
                shutil.copy2(f, s32 / dll); n += 1
        r.ok(f"{n} DLLs")

# --- fix 4: NTK Daemon helper service ----------------------------------------------------------------
def ntk_daemon(p: Prefix, reporter=None) -> str | None:
    r = null_reporter(reporter); _need("7z")
    r.step("Installing NTK Daemon helper service")
    setups = sorted((na_dir(p) / "resources/daemon").rglob("NTKDaemon*Setup*.exe"))
    if not setups: r.fail("NTKDaemon setup exe not found in NA resources"); return None
    setup = setups[-1]
    dest = p.drive_c / NTK_REL; dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        subprocess.run(["7z", "x", "-y", f"-o{t}", str(setup)], check=True, capture_output=True)
        exes = list(Path(t).rglob("NTKDaemon.exe"))
        if not exes: r.fail("NTKDaemon.exe not in extracted setup"); return None
        if p.is_running("NTKDaemon.exe"): p.sc("stop", "NTKDaemon"); time.sleep(2)
        shutil.copytree(exes[0].parent, dest, dirs_exist_ok=True)
    ver = exe_version(dest / "NTKDaemon.exe")
    binpath = "C:\\Program Files\\Common Files\\Native Instruments\\NTKDaemon\\NTKDaemon.exe"
    # sc wants "binPath=" and the value as separate argv tokens
    if not p.reg_query(r"HKLM\System\CurrentControlSet\Services\NTKDaemon").get("ImagePath"):
        p.sc("create", "NTKDaemon", "binPath=", f'"{binpath}"', "start=", "auto")
    # belt and braces: SERVICE_AUTO_START so it comes up with every prefix boot
    p.reg_add(r"HKLM\System\CurrentControlSet\Services\NTKDaemon", "Start", "2", "REG_DWORD")
    p.sc("start", "NTKDaemon"); time.sleep(2)
    running = p.is_running("NTKDaemon.exe") or "RUNNING" in p.sc("query", "NTKDaemon")
    r.ok(f"{ver or ''} {'running' if running else 'installed (starts with prefix)'}".strip())
    return ver

# --- fix 5: NA settings -----------------------------------------------------------------------------------
def config(p: Prefix, reporter=None):
    r = null_reporter(reporter)
    r.step("Enabling hardware acceleration in NA settings")
    d = na_roaming(p); n = 0
    if d.exists():
        for f in d.glob("*.json"):
            try: j = json.loads(f.read_text())
            except Exception: continue
            if isinstance(j, dict) and j.get("disableHardwareAcceleration") is True:
                j["disableHardwareAcceleration"] = False
                f.write_text(json.dumps(j, indent=2)); n += 1
    r.ok(f"{n} file(s) updated" if n else "nothing to change (first run)")

# --- fix 8: a writable download location ------------------------------------------------------------
NA_PREFS_KEY = r"HKCU\Software\Native Instruments\Native Access"

def default_download_dir(p: Prefix) -> Path:
    """C:\\users\\Public\\Downloads: inside the prefix, next to NA's content location
    (Public\\Documents). Wine turns the per-user Downloads folder into a symlink to
    the host's ~/Downloads, which the Flatpak sandbox mounts read-only; Public
    folders are never symlinked, so this one is always writable."""
    return p.drive_c / "users/Public/Downloads"

def writable_dir(host: Path) -> bool:
    """Can we create a file there? (os.access says yes on a read-only bind mount, so really try.)"""
    try:
        host.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=host, prefix=".nilinux-write-test-"): pass
        return True
    except OSError: return False

def download_location_status(p: Prefix) -> tuple[str, bool]:
    """(NA's configured download location as a Windows path, or '' when unset; writable from here)"""
    cur = p.reg_query(NA_PREFS_KEY).get("DownloadLocation", "")
    return cur, bool(cur) and writable_dir(p.to_host(cur))

def download_location(p: Prefix, reporter=None) -> bool:
    """A fresh prefix has no download location at all (the daemon then fails every
    download with "Download folder does not exist"), and a location on the host's
    ~/Downloads is read-only inside the sandbox ("could not create new file").
    Keep a user-chosen location that works; otherwise point NA at
    default_download_dir. Returns True if the preference was changed."""
    r = null_reporter(reporter)
    r.step("Download location")
    cur, ok = download_location_status(p)
    if ok: r.skip(cur); return False
    d = default_download_dir(p); d.mkdir(parents=True, exist_ok=True)
    win = p.to_win(d)
    # The NTK daemon serves these preferences to NA from memory, read at its start:
    # restart it around the write so NA sees the new value without a prefix reboot.
    # (sc talks to the prefix's services.exe, so this works across Flatpak instances.)
    running = "RUNNING" in p.sc("query", "NTKDaemon")
    if running: p.sc("stop", "NTKDaemon"); time.sleep(2)
    p.reg_add(NA_PREFS_KEY, "DownloadLocation", win)
    if running: p.sc("start", "NTKDaemon")
    r.ok(f"{win} (was {cur}: not writable)" if cur else f"{win} (was unset)")
    return True

# --- asar patching (fixes 6 and 7) -----------------------------------------------------------------------------
def _patch_asar(p: Prefix, edits: dict[str, "callable"], reporter=None) -> dict[str, str]:
    """Rewrite entries of NA's app.asar. edits: {path regex: fn(bytes) -> bytes | None}
    (None = no change). Only the edited entries change; offsets and per-file
    SHA-256 integrity are recomputed, unpacked entries untouched, and the asar
    header hash in the exe's Integrity resource is updated (NA ships with the
    EnableEmbeddedAsarIntegrityValidation fuse on). Returns {regex: patched|unchanged|missing}."""
    asar = na_dir(p) / "resources/app.asar"; exe = na_exe(p)
    data = asar.read_bytes()
    json_len = struct.unpack_from("<I", data, 12)[0]
    hjson = data[16:16 + json_len]; header = json.loads(hjson.rstrip(b"\0"))
    base = 16 + ((json_len + 3) & ~3)
    old_hash = hashlib.sha256(hjson).hexdigest()
    entries = []
    def walk(node, path):
        for k, v in node.get("files", {}).items():
            q = f"{path}/{k}" if path else k
            if "files" in v: walk(v, q)
            elif not v.get("unpacked"): entries.append((q, v))
    walk(header, "")
    BLOCK = 4 * 1024 * 1024
    def integrity(b):
        return {"algorithm": "SHA256", "hash": hashlib.sha256(b).hexdigest(), "blockSize": BLOCK,
                "blocks": [hashlib.sha256(b[i:i + BLOCK]).hexdigest() for i in range(0, len(b), BLOCK)]}
    result = {rx: "missing" for rx in edits}; replaced: dict[str, bytes] = {}
    for q, node in entries:
        for rx, fn in edits.items():
            if re.fullmatch(rx, q):
                o = int(node["offset"]); blob = data[base + o: base + o + node["size"]]
                nb = fn(blob)
                result[rx] = "patched" if nb is not None and nb != blob else "unchanged"
                if nb is not None and nb != blob: replaced[q] = nb
    if not replaced: return result
    entries.sort(key=lambda e: int(e[1]["offset"]))
    chunks, cur = [], 0
    for q, node in entries:
        if q in replaced: blob = replaced[q]; node["integrity"] = integrity(blob)
        else: o = int(node["offset"]); blob = data[base + o: base + o + node["size"]]
        node["offset"] = str(cur); node["size"] = len(blob); chunks.append(blob); cur += len(blob)
    hj = json.dumps(header, separators=(",", ":")).encode("utf-8")
    new_hash = hashlib.sha256(hj).hexdigest()
    pad = ((len(hj) + 3) & ~3) - len(hj)
    head = struct.pack("<IIII", 4, len(hj) + pad + 8, len(hj) + pad + 4, len(hj))
    ed = exe.read_bytes()
    if ed.count(old_hash.encode()) < 1: raise RuntimeError("asar header hash not found in exe — asar/exe mismatch")
    orig = asar.with_suffix(".asar.orig")
    if not orig.exists(): shutil.copy2(asar, orig)
    exb = exe.with_suffix(".exe.pre-asarpatch")
    if not exb.exists(): shutil.copy2(exe, exb)
    tmp = asar.with_suffix(".asar.tmp")
    with open(tmp, "wb") as f:
        f.write(head); f.write(hj); f.write(b"\0" * pad)
        for c in chunks: f.write(c)
    tmp.replace(asar)
    exe.write_bytes(ed.replace(old_hash.encode(), new_hash.encode()))
    return result

RENDERER_RX = r"out/renderer/assets/index-[\w-]+\.js"
MAIN_RX = r"out/main/index\.js"

# --- fix 6: dependency check patch --------------------------------------------------------------------------------
_DEP_PAT = re.compile(rb"(_0x[0-9a-f]+)=(_0x[0-9a-f]+)\[0x0\]\?\?(_0x[0-9a-f]+)\[0x0\],(_0x[0-9a-f]+)=\2\[_0x[0-9a-f]+\(0x[0-9a-f]+\)\]\((_0x[0-9a-f]+)=>\5\[")

def _dep_edit(js: bytes):
    if DEP_MARK.encode() in js: return None
    ms = list(_DEP_PAT.finditer(js))
    if len(ms) != 1: raise LookupError(f"patch site not found ({len(ms)} matches)")
    m = ms[0]; cand, owned, players = m.group(1), m.group(2), m.group(3)
    old = b"%s=%s[0x0]??%s[0x0]" % (cand, owned, players)
    new = b"%s=%s%s.find(p=>p.isInstalled)??%s.find(p=>p.isInstalled)??%s[0x0]??%s[0x0]" % (cand, DEP_MARK.encode(), owned, players, owned, players)
    return js.replace(old, new, 1)

def dependency_patch(p: Prefix, reporter=None) -> str:
    """Make an *installed* viable Kontakt satisfy library dependency checks."""
    r = null_reporter(reporter)
    r.step("Patching NA dependency check (installed Player wins)")
    if dependency_patched(p): r.skip("already"); return "already"
    try: res = _patch_asar(p, {RENDERER_RX: _dep_edit}, r)
    except LookupError as e: r.fail(f"{e} — NA version not yet supported"); return "unsupported"
    st = res[RENDERER_RX]
    (r.ok if st == "patched" else r.fail)(st); return st if st == "patched" else "unsupported"

def dependency_patched(p: Prefix) -> bool:
    asar = na_dir(p) / "resources/app.asar"
    return asar.exists() and DEP_MARK.encode() in asar.read_bytes()

# --- fix 7: NA self-updater -------------------------------------------------------------------------------------
_AU_ON, _AU_OFF = b"autoUpdateEnabled:!0", b"autoUpdateEnabled:!1"

def _au_edit(js: bytes):
    if _AU_OFF in js: return None
    if js.count(_AU_ON) != 1: raise LookupError(f"autoUpdateEnabled default not found once ({js.count(_AU_ON)})")
    return js.replace(_AU_ON, _AU_OFF, 1)   # same length: build default flips to "auto updates disabled"

def disable_self_update(p: Prefix, reporter=None) -> str:
    """Flip NA's build-config default so the updater is never initialised (NA
    logs "auto updates disabled, skipping init"): no download, no prompt, no
    error toast. Updates happen only through an installer the user downloads
    from NI (install_native_access). The env var can only enable, never
    disable, and the dev.json override schema only picks daemon/log keys."""
    r = null_reporter(reporter)
    r.step("Disabling Native Access self-updates")
    # undo earlier attempts (feed-URL hack; dev.json key)
    yml = na_dir(p) / "resources/app-update.yml"; orig = yml.with_suffix(".yml.orig")
    if orig.exists() and UPDATER_DISABLED_URL in yml.read_text(): yml.write_text(orig.read_text())
    dj = na_roaming(p) / "dev.json"
    if dj.exists():
        try:
            cfg = json.loads(dj.read_text()); cfg.pop("autoUpdateEnabled", None)
            dj.write_text(json.dumps(cfg, indent=2)) if cfg else dj.unlink()
        except Exception: pass
    if self_update_disabled(p): r.skip("already"); return "already"
    try: res = _patch_asar(p, {MAIN_RX: _au_edit}, r)
    except LookupError as e: r.fail(f"{e} — NA version not yet supported"); return "unsupported"
    st = res[MAIN_RX]
    (r.ok if st == "patched" else r.fail)("updates only via a downloaded installer" if st == "patched" else st)
    return st if st == "patched" else "unsupported"

def self_update_disabled(p: Prefix) -> bool:
    asar = na_dir(p) / "resources/app.asar"
    return asar.exists() and _AU_OFF in asar.read_bytes()

def version_notice(p: Prefix) -> dict:
    """installed vs latest (from NI's feed, version string only) and whether latest is validated."""
    inst = exe_version(na_exe(p)) if na_exe(p).exists() else None
    inst_short = ".".join(inst.split(".")[:3]) if inst else None
    latest = latest_version()
    newer = bool(inst_short and latest and _vtuple(latest) > _vtuple(inst_short))
    return {"installed": inst_short, "latest": latest, "newer": newer, "latest_known_good": latest in KNOWN_GOOD if latest else False}

def _vtuple(v: str): return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

# --- launch / update -------------------------------------------------------------------------------------------
def clear_stale_mutexes(p: Prefix):
    """boost named mutexes are not crash-safe; clear when no NI app runs."""
    if any(p.is_running(x) for x in ("Native Access.exe", "Kontakt", "NTKDaemon.exe")): return
    d = p.drive_c / "ProgramData/boost_interprocess"
    if d.exists():
        for f in d.rglob("*"):
            if f.is_file(): f.unlink(missing_ok=True)

def launch(p: Prefix, reporter=None, extra_args=()) -> subprocess.Popen:
    r = null_reporter(reporter)
    exe = na_exe(p)
    if not exe.exists(): raise RuntimeError(f"Native Access is not installed yet — download it from {NA_DOWNLOAD_PAGE} and use install-na")
    if p.is_running("Native Access.exe"): p.kill_exe("Native Access.exe")
    clear_stale_mutexes(p)
    if stack_patch(exe) == "patched": r.log("re-applied stack patch (NA updated itself)")
    download_location(p, r)
    paths.ensure_dirs()
    log = paths.LOGS / "native-access-launch.log"
    # --disable-gpu: NA >= 3.25's GPU process crash-loops under Wine (blank window)
    return p.spawn([str(exe), "--disable-gpu", *extra_args], log=log)

def pending_update(p: Prefix) -> Path | None:
    f = p.user_dir / "AppData/Local/nativeaccess2-updater/pending/Native-Access-latest.exe"
    return f if f.exists() else None

def apply_pending_update(p: Prefix, reporter=None) -> bool:
    r = null_reporter(reporter)
    f = pending_update(p)
    if not f: return False
    if p.is_running("Native Access.exe"): p.kill_exe("Native Access.exe")
    install_native_access(p, f, r)
    return True

# --- setup ---------------------------------------------------------------------------------------------------------
def prepare(p: Prefix, reporter=None):
    """Everything that does not need Native Access: prefix, fonts, C runtime, registry."""
    r = null_reporter(reporter)
    p.create(r); p.refresh_builtins(r); fonts(p, r); vc_runtime(p, r); registry(p, r); download_location(p, r); p.wait_idle()
    from . import yabridge
    try: yabridge.install(r)      # so DAW bridging works from the first product install
    except Exception as e: r.step("Installing yabridge"); r.fail(str(e)[:80])
    yabridge.configure_daw_environment(p, r)   # DAWs must run plugins with *this* wine (see yabridge.daw_environment_status)

def install_native_access(p: Prefix, installer: Path, reporter=None):
    """Install (or update) NA from an installer the user downloaded from NI, then apply the NA-side fixes."""
    r = null_reporter(reporter)
    installer = Path(installer)
    if not installer.is_file(): raise FileNotFoundError(f"installer not found: {installer}")
    if p.is_running("Native Access.exe"): p.kill_exe("Native Access.exe")
    if not (p.drive_c / "windows/Fonts/DejaVuSans.ttf").exists(): prepare(p, r)
    install(p, installer, r)
    r.step("Patching Native Access.exe stack reserve to 64MB"); r.ok(stack_patch(na_exe(p)))
    disable_self_update(p, r); ntk_daemon(p, r); config(p, r); download_location(p, r); dependency_patch(p, r); p.wait_idle()

def install_native_access_fixes(p: Prefix, reporter=None):
    """Re-apply the NA-side fixes to an already installed Native Access (repair)."""
    r = null_reporter(reporter)
    r.step("Patching Native Access.exe stack reserve to 64MB"); r.ok(stack_patch(na_exe(p)))
    disable_self_update(p, r); ntk_daemon(p, r); config(p, r); download_location(p, r); dependency_patch(p, r); p.wait_idle()

def setup(p: Prefix, reporter=None, installer: Path | None = None):
    """prepare(); then install NA if an installer path is given. Native Access
    itself is never downloaded by this app — the user gets it from NA_DOWNLOAD_PAGE."""
    prepare(p, reporter)
    if installer: install_native_access(p, installer, reporter)
    elif na_exe(p).exists(): install_native_access_fixes(p, reporter)

def status(p: Prefix) -> dict:
    exe = na_exe(p); s32 = p.drive_c / "windows/system32"
    return {
        "installed": exe.exists(),
        "version": exe_version(exe) if exe.exists() else None,
        "stack_patch": stack_ok(exe) if exe.exists() else False,
        "fonts": (p.drive_c / "windows/Fonts/DejaVuSans.ttf").exists(),
        "ucrtbase": (s32 / "ucrtbase.dll.wine-builtin.bak").exists(),
        "vc_runtime": (s32 / "vcruntime140.dll.bak").exists(),
        "prepared": (p.drive_c / "windows/Fonts/DejaVuSans.ttf").exists() and (p.drive_c / "windows/system32/vcruntime140.dll.bak").exists(),
        "ntk_daemon": (p.drive_c / NTK_REL / "NTKDaemon.exe").exists(),
        "ntk_version": exe_version(p.drive_c / NTK_REL / "NTKDaemon.exe") if (p.drive_c / NTK_REL / "NTKDaemon.exe").exists() else None,
        "dependency_patch": dependency_patched(p) if exe.exists() else False,
        "self_update_disabled": self_update_disabled(p) if exe.exists() else False,
        "pending_update": pending_update(p) is not None,
        "running": p.is_running("Native Access.exe"),
    }
