"""Other nilinux prefixes on this machine, and who owns the NTK daemon ports.

A machine can end up with several nilinux prefixes: the source install
(~/.local/share/nilinux), the Flatpak (~/.var/app/<id>/data/nilinux), or an
older Flatpak app id. Each has its own wineserver, registry and NTK daemon,
but the daemon binds fixed localhost ports, so only one can own them. A plugin
bridged from prefix A while prefix B's daemon holds the ports talks to the
wrong daemon and hangs in every DAW. The rules that keep this consistent:

- the nilinux that runs owns the bridges: sync() removes other nilinux
  prefixes' directories from yabridgectl (third-party prefixes are left alone)
- Health names the prefix whose daemon holds the ports, by reading the
  listener's WINEPREFIX, instead of guessing from "our wineserver is up"
"""
import os, re, socket
from pathlib import Path
from .wine import Prefix

PREFIX_RE = re.compile(r"^(.*/nilinux/prefix)(?:/drive_c(?:/.*)?)?/?$")

def nilinux_prefixes() -> list[Path]:
    """Every existing nilinux prefix: source install, every Flatpak app id, and
    whatever XDG_DATA_HOME points at."""
    from . import paths
    home = Path.home()
    cands = [paths.PREFIX, home / ".local/share/nilinux/prefix"]
    var = home / ".var/app"
    if var.is_dir():
        cands += sorted(var.glob("*/data/nilinux/prefix"))
    out: list[Path] = []
    for c in cands:
        if (c / "drive_c" / "windows").exists() and not any(_same(c, o) for o in out):
            out.append(c)
    return out

def _same(a: Path, b: Path) -> bool:
    try: return a.resolve() == b.resolve()
    except OSError: return a == b

def other_prefixes(p: Prefix) -> list[Path]:
    return [x for x in nilinux_prefixes() if not _same(x, p.path)]

def prefix_of_dir(d: str | Path) -> Path | None:
    """The nilinux prefix a directory lives in, or None (third-party prefix, host dir)."""
    try: s = str(Path(d).resolve())
    except OSError: s = str(d)
    m = PREFIX_RE.match(s.rstrip("/"))
    return Path(m.group(1)) if m else None

def yabridgectl_dirs(status_text: str) -> list[str]:
    """Plugin directories from `yabridgectl status` output (the unindented lines)."""
    return [l.strip() for l in status_text.splitlines() if l.startswith("/") and l.rstrip().endswith("/")]

def foreign_yabridge_dirs(p: Prefix, status_text: str) -> list[str]:
    """Registered directories that belong to a *different* nilinux prefix."""
    out = []
    for d in yabridgectl_dirs(status_text):
        owner = prefix_of_dir(d)
        if owner is not None and not _same(owner, p.path): out.append(d)
    return out

def short(path: str | Path) -> str:
    s = str(path)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s

# --- processes ---------------------------------------------------------------------------------
def _environ(pid: int) -> dict[str, str]:
    try: raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    except OSError: return {}
    out = {}
    for item in raw.split(b"\0"):
        k, _, v = item.partition(b"=")
        if k: out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out

def _comm(pid: int) -> str:
    try: return (Path("/proc") / str(pid) / "comm").read_text().strip()
    except OSError: return ""

def _cmdline(pid: int) -> str:
    try: return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError: return ""

def _listeners(ports: tuple[int, ...]) -> dict[int, int]:
    """port -> socket inode for LISTEN sockets on 127.0.0.1 (from /proc/net/tcp)."""
    out: dict[int, int] = {}
    try: lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError: return out
    for l in lines:
        f = l.split()
        if len(f) < 10 or f[3] != "0A": continue        # 0A = LISTEN
        addr, port_hex = f[1].split(":")
        if addr != "0100007F": continue                 # 127.0.0.1, little-endian hex
        port = int(port_hex, 16)
        if port in ports: out[port] = int(f[9])
    return out

def _pid_for_inode(inode: int) -> int | None:
    """A process holding this socket. Wine sockets are shared by wineserver and the
    Windows process that owns them; prefer the latter for a meaningful name."""
    want = f"socket:[{inode}]"
    holders = []
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit(): continue
        try:
            for fd in (pid_dir / "fd").iterdir():
                if os.readlink(fd) == want: holders.append(int(pid_dir.name)); break
        except OSError: continue
    if not holders: return None
    named = [h for h in holders if "wineserver" not in _cmdline(h)]
    return (named or holders)[0]

def ntk_port_owner(ports: tuple[int, ...]) -> dict:
    """Who listens on the NTK daemon ports.
    {'busy': [ports], 'pid': int|None, 'prefix': str|None, 'exe': str}. pid/prefix are
    None when a port is busy but its holder is invisible (another pid namespace)."""
    inodes = _listeners(ports)
    busy = sorted(inodes)
    if not busy:
        # /proc/net/tcp is per network namespace; double-check with a bind attempt
        for port in ports:
            with socket.socket() as sk:
                try: sk.bind(("127.0.0.1", port))
                except OSError: busy.append(port)
        return {"busy": busy, "pid": None, "prefix": None, "exe": ""}
    pid = None
    for port in busy:
        pid = _pid_for_inode(inodes[port])
        if pid is not None: break
    env = _environ(pid) if pid else {}
    return {"busy": busy, "pid": pid, "prefix": env.get("WINEPREFIX") or None, "exe": _comm(pid) if pid else ""}

def wineservers() -> list[dict]:
    """Running wineservers: [{'pid', 'prefix'}]."""
    out = []
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit(): continue
        try: comm = (pid_dir / "comm").read_text().strip()
        except OSError: continue
        if comm != "wineserver": continue
        pid = int(pid_dir.name)
        out.append({"pid": pid, "prefix": _environ(pid).get("WINEPREFIX", "")})
    return out

def report(p: Prefix, ports: tuple[int, ...], status_text: str = "") -> str:
    """Human-readable overview for `nilinux prefixes`."""
    lines = [f"this prefix:   {short(p.path)}"]
    others = other_prefixes(p)
    lines.append("other prefixes: " + (", ".join(short(o) for o in others) if others else "none"))
    ws = wineservers()
    lines.append("wineservers:    " + (", ".join(f"{short(w['prefix']) or '?'} (pid {w['pid']})" for w in ws) if ws else "none running"))
    o = ntk_port_owner(ports)
    if not o["busy"]: lines.append("NTK daemon:     not running (starts with Native Access / the first plugin)")
    elif o["prefix"]: lines.append(f"NTK daemon:     {o['exe']} pid {o['pid']} in {short(o['prefix'])}" + ("" if _same(Path(o["prefix"]), p.path) else "   <-- ANOTHER PREFIX"))
    else: lines.append(f"NTK daemon:     ports {o['busy']} held by a process this sandbox cannot see")
    if status_text:
        foreign = foreign_yabridge_dirs(p, status_text)
        lines.append("yabridgectl:    " + (f"{len(foreign)} directories of other nilinux prefixes (nilinux sync removes them)" if foreign else "only this prefix's directories"))
    return "\n".join(lines)
