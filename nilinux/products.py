"""NI products: what is installed, registering libraries, installing apps.

- Content libraries install through Native Access. The daemon sometimes skips
  the HKLM key Kontakt scans at startup; register_library() rebuilds it.
- NI *application* installers (InstallAware) may fail under Wine. install_app()
  runs the setup silently under an MSI trace: if it succeeds, done; if not, the
  trace gives every payload->destination root and the registry keys, and the
  payload is deployed by hand from the extracted FileBag.
"""
import json, os, re, shutil, subprocess, tempfile, time, zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from . import paths
from .progress import null_reporter
from .wine import Prefix

NI_KEY = r"HKLM\Software\Native Instruments"

def hints_path(p: Prefix) -> Path:
    return p.drive_c / "Program Files/Common Files/Native Instruments/Service Center/NativeAccess.xml"
def installed_products_dir(p: Prefix) -> Path:
    return p.public_docs / "Native Instruments/installed_products"
def ras3_dir(p: Prefix) -> Path:
    return p.public_docs / "Native Instruments/Native Access/ras3"

@dataclass
class Product:
    name: str
    upid: str = ""
    type: str = ""          # App | Content | Bundle | Utility
    regkey: str = ""
    hu: str = ""
    jdx: str = ""
    content_dir: str = ""   # from installed_products json
    install_dir: str = ""
    version: str = ""
    registered: bool = False    # HKLM key present
    licensed: bool = False      # ras3 jwt present and non-empty
    deps: list = field(default_factory=list)

def hints(p: Prefix) -> dict[str, Product]:
    """Product catalogue NA writes (name -> Product)."""
    f = hints_path(p)
    if not f.exists(): return {}
    out = {}
    for pr in ET.parse(f).getroot().findall("Product"):
        n = pr.findtext("Name") or ""
        out[n] = Product(name=n, upid=pr.findtext("UPID") or "", type=pr.findtext("Type") or "",
                         regkey=pr.findtext("RegKey") or n, hu=pr.findtext("ProductSpecific/HU") or "",
                         jdx=pr.findtext("ProductSpecific/JDX") or "",
                         deps=[(d.text, d.get("minVersion")) for d in pr.findall("Dependencies/AppDependency")])
    return out

def hive_keys(p: Prefix) -> dict[str, dict[str, str]]:
    """HKLM\\Software\\Native Instruments\\* from the on-disk hive (fast, may lag a
    running wineserver by up to a minute). Values are strings."""
    reg = p.path / "system.reg"; out = {}
    if not reg.exists(): return out
    cur = None
    for line in reg.read_text(errors="ignore").splitlines():
        m = re.match(r"^\[Software\\\\Native Instruments\\\\([^\]]+)\]", line)
        if m: cur = m.group(1).replace("\\\\", "\\"); out[cur] = {}; continue
        if line.startswith("["): cur = None; continue
        if cur and line.startswith('"'):
            mv = re.match(r'^"([^"]+)"=(?:"(.*)"|dword:([0-9a-f]+)|str\(\d+\):"(.*)")', line)
            if mv:
                v = mv.group(2) if mv.group(2) is not None else (mv.group(4) if mv.group(4) is not None else str(int(mv.group(3), 16)))
                out[cur][mv.group(1)] = v.replace("\\\\", "\\") if isinstance(v, str) else v
    return out

def installed(p: Prefix) -> list[Product]:
    """Installed products = daemon's installed_products records, enriched with
    catalogue, registry and license state."""
    cat = hints(p); keys = hive_keys(p); lic = {f.stem: f.stat().st_size > 0 for f in ras3_dir(p).glob("*.jwt")} if ras3_dir(p).exists() else {}
    out = []
    d = installed_products_dir(p)
    if d.exists():
        for f in sorted(d.glob("*.json")):
            try: j = json.loads(f.read_text())
            except Exception: j = {}
            pr = cat.get(f.stem) or Product(name=f.stem, regkey=f.stem)
            pr.content_dir = j.get("ContentDir", ""); pr.install_dir = j.get("InstallDir", "")
            pr.version = j.get("ContentVersion", "")
            k = keys.get(pr.regkey, {})
            pr.registered = bool(k.get("ContentDir") or k.get("InstallDir"))
            pr.licensed = lic.get(pr.upid, False)
            out.append(pr)
    return out

# --- libraries ------------------------------------------------------------------------
def register_library(p: Prefix, name: str, reporter=None) -> bool:
    """Write HKLM\\Software\\Native Instruments\\<RegKey> for an installed content
    library (ContentDir/ContentVersion from installed_products, HU/JDX from the
    catalogue). Returns True if it wrote anything."""
    r = null_reporter(reporter)
    r.step(f"Registering library: {name}")
    cat = hints(p); pr = cat.get(name)
    rec = installed_products_dir(p) / f"{name}.json"
    if not rec.exists(): r.fail("not installed (no installed_products record)"); return False
    j = json.loads(rec.read_text())
    key = f"{NI_KEY}\\{pr.regkey if pr else name}"
    if p.reg_query(key).get("ContentDir"): r.skip("already registered"); return False
    vals = {"ContentDir": j["ContentDir"], "ContentVersion": j.get("ContentVersion", "1.0.0")}
    if pr:
        if pr.hu: vals["HU"] = pr.hu
        if pr.jdx: vals["JDX"] = pr.jdx
    for k, v in vals.items(): p.reg_add(key, k, v)
    p.reg_add(key, "Visibility", "3", "REG_DWORD")
    r.ok("restart Kontakt to see it"); return True

def register_all_libraries(p: Prefix, reporter=None) -> list[str]:
    done = []
    for pr in installed(p):
        if pr.type == "Content" and not pr.registered:
            if register_library(p, pr.name, reporter): done.append(pr.name)
    return done

# --- NI application installers ---------------------------------------------------------
TRACE_RE = re.compile(r"VALUES \( '(P[0-9A-F]+)_(\d+)' , '([^']*)'")
SKIP_ROOTS = ("Installer Log", "Start Menu", "\\Desktop", "Avid\\Audio", "ProgramData\\Native Instruments")

def parse_trace(trace: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(roots: 'P<hash>_1' -> windows dir, regkeys: 'SOFTWARE\\...\\Value' -> data)"""
    props: dict[str, dict[int, str]] = {}
    with open(trace, errors="ignore") as f:
        for line in f:
            for m in TRACE_RE.finditer(line):
                props.setdefault(m.group(1), {})[int(m.group(2))] = m.group(3).replace("\\\\", "\\")
    roots, regkeys = {}, {}
    last_key = ""
    for bag, v in props.items():
        if v.get(2) == "REGISTRY KEYS" and 3 in v and 4 in v and not v[3].startswith("HKCU"):
            name = v[3]
            # a bare value name (e.g. 'ContentVersion') belongs to the product key that
            # the previous REGISTRY KEYS entry addressed (SOFTWARE\Native Instruments\<Product>)
            if "\\" not in name:
                if not last_key: continue
                name = last_key + "\\" + name
            regkeys[name] = v[4]; last_key = name.rpartition("\\")[0]
        dest = v.get(1, "")
        if dest[:3].upper() == "C:\\" and not any(x in dest for x in SKIP_ROOTS):
            roots[bag + "_1"] = dest.rstrip("\\")
    return roots, regkeys

def _setup_exe_from(path: Path, workdir: Path) -> Path:
    """Accept a 'X Setup PC.exe' or the .zip NA downloads that wraps it."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".exe")]
            if not names: raise RuntimeError("zip contains no setup exe")
            z.extract(names[0], workdir); return workdir / names[0]
    return path

def install_app(p: Prefix, setup: Path, reporter=None, keep_trace=False) -> dict:
    """Install an NI application from its installer. Returns a summary dict."""
    r = null_reporter(reporter); paths.ensure_dirs()
    if not shutil.which("7z"): raise RuntimeError("missing host tool: 7z")
    work = Path(tempfile.mkdtemp(prefix="nilinux-app-", dir=paths.CACHE))
    try:
        exe = _setup_exe_from(setup, work)
        name = re.sub(r"\s+Setup PC\.exe$", "", exe.name, flags=re.I)
        trace = paths.LOGS / f"{name}-install.trace"
        r.step(f"Running installer silently: {exe.name}")
        t0 = time.time()
        cp = p.run([str(exe), "/s"], debug="+msi", timeout=3600, capture=False, stderr_to=trace)
        roots, regkeys = parse_trace(trace)
        installed_ok = cp.returncode == 0 and any(p.reg_query(_split_key(k)[0]).get(_split_key(k)[1]) for k in regkeys) if regkeys else cp.returncode == 0
        if installed_ok:
            r.ok(f"installer succeeded ({time.time()-t0:.0f}s)")
            if not keep_trace: trace.unlink(missing_ok=True)
            return {"name": name, "method": "installer", "regkeys": regkeys}
        r.fail(f"installer exit {cp.returncode}; falling back to manual deploy")
        if not roots: raise RuntimeError("trace has no destination roots; cannot deploy")
        r.step("Extracting installer payload"); bag = work / "bag"; bag.mkdir()
        subprocess.run(["7z", "x", "-y", f"-o{bag}", str(exe)], check=True, capture_output=True)
        msis = list(bag.glob("*.msi"))
        if not msis: raise RuntimeError("no inner MSI in installer")
        r.ok(msis[0].name)
        n = deploy_payload(p, msis[0], bag / "data", roots, r)
        r.step("Writing registry keys")
        vals = {}
        for k, v in regkeys.items():
            key, val = _split_key(k)
            if not key: r.log(f"skipping registry entry without a key: {k}"); continue
            vals.setdefault("HKLM\\" + key, {})[val] = ("REG_SZ", v)
        if vals: p.reg_import_values(vals, "nilinux-app-install.reg")
        r.ok(f"{sum(len(x) for x in vals.values())} value(s)")
        if not keep_trace: trace.unlink(missing_ok=True)
        return {"name": name, "method": "deploy", "files": n, "regkeys": regkeys}
    finally:
        shutil.rmtree(work, ignore_errors=True)

def _split_key(k: str) -> tuple[str, str]:
    key, _, val = k.rpartition("\\"); return key, val

def deploy_payload(p: Prefix, msi_path: Path, bag: Path, roots: dict[str, str], reporter=None) -> int:
    """Copy FileBag payload into the prefix using the MSI's File->Component->Directory tables."""
    from .msi import Msi
    r = null_reporter(reporter)
    r.step("Deploying payload files")
    m = Msi(msi_path)
    dirs = {d["Directory"]: (d["Directory_Parent"], d["DefaultDir"]) for d in m.rows("Directory")}
    comp_dir = {c["Component"]: c["Directory_"] for c in m.rows("Component")}
    long = lambda part: part.split("|", 1)[-1]
    cache: dict[str, tuple[str, str] | None] = {}
    def resolve(key):
        if key in cache: return cache[key]
        res = None
        if key in roots: res = (str(p.to_host(roots[key])), "")
        elif key in dirs:
            parent, dd = dirs[key]
            if parent and key != "TARGETDIR":
                base = resolve(parent)
                if base is not None:
                    tpart, spart = dd.split(":", 1) if ":" in dd else (dd, dd)
                    t, s = long(tpart), long(spart)
                    res = (base[0] if t == "." else os.path.join(base[0], t), base[1] if s == "." else os.path.join(base[1], s))
        cache[key] = res; return res
    copied = missing = 0
    for f in m.rows("File"):
        d = comp_dir.get(f["Component_"]); res = resolve(d) if d else None
        if res is None: continue
        tdir, sdir = res; name = long(f["FileName"])
        src = Path(bag) / sdir / name; dst = Path(tdir) / name
        if not src.exists(): missing += 1; continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.stat().st_size != src.stat().st_size: shutil.copy2(src, dst)
        copied += 1
    r.ok(f"{copied} files" + (f", {missing} missing" if missing else ""))
    return copied

# --- third-party installers ------------------------------------------------------------------
def run_installer(p: Prefix, exe: Path, reporter=None, args=()) -> int:
    """Run any Windows installer interactively in the prefix and wait for it."""
    r = null_reporter(reporter)
    r.step(f"Running installer: {Path(exe).name}")
    cp = p.run([str(exe), *args], timeout=7200, capture=False)
    (r.ok if cp.returncode == 0 else r.fail)(f"exit {cp.returncode}")
    return cp.returncode
