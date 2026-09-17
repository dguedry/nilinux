"""Command-line front end. Every subcommand is a thin call into the package."""
import argparse, sys
from pathlib import Path
from . import __version__, paths, wine, native_access as na, products, yabridge, doctor, prefixes, programs
from .progress import ConsoleReporter

def _prefix(create=False) -> wine.Prefix:
    b = wine.installed_build()
    if b is None:
        if not create: sys.exit("wine not provisioned yet — run: nilinux setup")
        b = wine.provision(ConsoleReporter())
    return wine.Prefix(paths.PREFIX, b)

def _na_hint():
    print(f"\nNative Access is not bundled. Download it from\n  {na.NA_DOWNLOAD_PAGE}\nthen run:  nilinux install-na ~/Downloads/Native-Access-latest.exe"
          + (f"\n(current version: {v})" if (v := na.latest_version()) else ""))

def cmd_setup(a):
    r = ConsoleReporter(); b = wine.provision(r); p = wine.Prefix(paths.PREFIX, b)
    na.prepare(p, r)
    if a.installer: na.install_native_access(p, Path(a.installer), r)
    elif na.na_exe(p).exists(): na.install_native_access_fixes(p, r)   # repair / re-apply on an existing install
    if r.failed: sys.exit(f"setup finished with failures: {[s.name for s in r.failed]}")
    if na.na_exe(p).exists():
        v = na.version_notice(p)
        if v["installed_known_bad"]: print(f"\nNative Access {v['installed']} does not work on this stack: {v['installed_known_bad']}. Install a validated version: {', '.join(sorted(na.KNOWN_GOOD))}.")
        if v["newer"]: print(f"\nNative Access {v['latest']} is available ({'validated' if v['latest_known_good'] else ('DOES NOT WORK' if v['latest_known_bad'] else 'not yet validated')} on this stack); installed {v['installed']}. Update via: nilinux install-na <downloaded file>")
        print("\nDone. Launch with: nilinux launch")
    else: _na_hint()

def cmd_install_na(a):
    p = _prefix(create=True); r = ConsoleReporter()
    na.install_native_access(p, Path(a.installer), r)
    if r.failed: sys.exit("failed")
    print("\nNative Access installed. Launch with: nilinux launch")

def cmd_launch(a):
    p = _prefix(); r = ConsoleReporter()
    proc = na.launch(p, r, extra_args=a.args)
    print(f"Native Access started (pid {proc.pid}); log: {paths.LOGS/'native-access-launch.log'}")
    if a.wait:
        proc.wait(); print("Native Access exited; finishing installs, registering libraries and syncing yabridge")
        products.finish_staged_installs(p, r); products.register_all_libraries(p, r); yabridge.sync(p, r)

def cmd_update(a):
    p = _prefix(); r = ConsoleReporter()
    if not na.apply_pending_update(p, r): print("no pending update (trigger it inside Native Access first)")

def cmd_doctor(a):
    p = _prefix() if wine.installed_build() else None
    bad = 0
    for c in doctor.run(p):
        mark = "✓" if c.ok else "✗"; bad += not c.ok
        line = f"  {mark} {c.name}" + (f"  ({c.detail})" if c.detail else "")
        if not c.ok and c.fix: line += f"\n      fix: {c.fix}"
        print(line)
    sys.exit(1 if bad else 0)

def cmd_products(a):
    p = _prefix()
    rows = products.installed(p)
    if not rows: print("no installed products recorded (install something in Native Access first)"); return
    w = max(len(x.name) for x in rows)
    for x in rows:
        flags = [x.type or "?", "registered" if x.registered else "NOT REGISTERED", "licensed" if x.licensed else "no license"]
        print(f"  {x.name.ljust(w)}  {x.version:8}  {', '.join(flags)}")

def cmd_register(a):
    p = _prefix(); r = ConsoleReporter()
    done = [n for n in a.names if products.register_library(p, n, r)] if a.names else products.register_all_libraries(p, r)
    print(f"registered: {done or 'nothing new'}")

def cmd_programs(a):
    p = _prefix(); rows = programs.installed(p)
    if not rows: print("no programs found in the prefix"); return
    w = max(len(x.name) for x in rows)
    for x in rows:
        print(f"  {x.name.ljust(w)}  {x.version:10}  {x.exe or '(uninstall only)'}")

def cmd_run(a):
    p = _prefix(); r = ConsoleReporter()
    prog = programs.find(p, a.name); proc = programs.run(p, prog, r)
    if a.wait:
        proc.wait(); yabridge.sync(p, r)           # the program may have installed plugins

def cmd_uninstall(a):
    p = _prefix(); r = ConsoleReporter()
    programs.uninstall(p, programs.find(p, a.name), r)
    if not a.no_sync: yabridge.sync(p, r)

def cmd_install_program(a):
    p = _prefix(); r = ConsoleReporter()
    programs.install(p, Path(a.installer), r)
    if not a.no_sync: yabridge.sync(p, r)

def cmd_install(a):
    p = _prefix(); r = ConsoleReporter()
    src = Path(a.installer)
    if a.third_party:
        programs.install(p, src, r)
    else:
        res = products.install_app(p, src, r, keep_trace=a.keep_trace)
        print(f"  {res['name']}: installed via {res['method']}")
    if not a.no_sync: yabridge.sync(p, r)

def cmd_sync(a):
    p = _prefix(); r = ConsoleReporter()
    res = yabridge.sync(p, r, extras=a.dirs)
    if a.daw_env: yabridge.configure_daw_environment(p, r)
    print(res["summary"])

def cmd_status(a):
    p = _prefix(); print(yabridge.status(p))

def cmd_dxvk(a):
    from . import dxvk
    p = _prefix()
    if a.action == "status":
        st = dxvk.status(p)
        print(f"vulkan: {'yes' if st['vulkan_ok'] else 'no'} — {st['vulkan']}")
        print(f"dxvk:   {st['version'] + ' installed' if st['installed'] else 'not installed'} (wanted {st['wanted']})")
        return
    r = ConsoleReporter()
    if a.action == "remove": dxvk.uninstall(p, r)
    else: dxvk.install(p, r, force=a.force)

def cmd_prefixes(a):
    p = _prefix(); print(prefixes.report(p, na.NTK_PORTS, yabridge.status(p)))

def cmd_report(a):
    """Write a diagnostic bundle to attach to a bug report."""
    from . import report
    b = wine.installed_build()
    p = wine.Prefix(paths.PREFIX, b) if b else None
    if a.summary_only:
        print(report.summary(p)); return
    dest = report.write_bundle(Path(a.output) if a.output else None, p)
    print(f"wrote {dest}")
    print("It contains a summary and this app's logs, with serials, licence tokens and your")
    print("home directory removed. Please attach it to the issue.")

def cmd_rescue(a):
    """Kill an installer that has stopped responding and finish it from what it staged."""
    p = _prefix(); r = ConsoleReporter()
    res = products.rescue_stalled_na_install(p, r)
    if not res: print("nothing to finish (no staged install found)")
    for x in res: print(f"  {x.get('name')}: {x.get('method')}")

def cmd_finish_installs(a):
    p = _prefix(); r = ConsoleReporter()
    staged = products.staged_installs(p)
    if not staged: print("no interrupted Native Access installs found"); return
    res = products.finish_staged_installs(p, r)
    for x in res: print(f"  {x['name']}: finished via {x['method']}")
    if not a.no_sync: yabridge.sync(p, r)
    if r.failed: sys.exit("finished with failures")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="nilinux", description="Native Instruments on Linux: Native Access, products and VST bridging without touching Wine yourself.")
    ap.add_argument("--version", action="version", version=__version__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("setup", help="create the prefix, install Native Access and apply all fixes (idempotent)")
    s.add_argument("--installer", help="Native-Access-*.exe you downloaded from native-instruments.com"); s.set_defaults(f=cmd_setup)
    s = sp.add_parser("install-na", help="install/update Native Access from an installer you downloaded from NI")
    s.add_argument("installer"); s.set_defaults(f=cmd_install_na)
    s = sp.add_parser("launch", help="start Native Access")
    s.add_argument("--wait", action="store_true", help="wait for it to exit, then register libraries and sync yabridge")
    s.add_argument("args", nargs="*"); s.set_defaults(f=cmd_launch)
    sp.add_parser("update", help="apply an update NA downloaded before its self-updater was disabled (legacy)").set_defaults(f=cmd_update)
    sp.add_parser("doctor", help="check every fix and prerequisite").set_defaults(f=cmd_doctor)
    sp.add_parser("products", help="list installed NI products").set_defaults(f=cmd_products)
    s = sp.add_parser("register", help="write the HKLM keys Kontakt needs for installed libraries")
    s.add_argument("names", nargs="*"); s.set_defaults(f=cmd_register)
    s = sp.add_parser("install", help="install an NI app from its installer (zip/exe), or a third-party plugin installer")
    s.add_argument("installer"); s.add_argument("--third-party", action="store_true", help="run interactively (non-NI installer)")
    s.add_argument("--keep-trace", action="store_true"); s.add_argument("--no-sync", action="store_true"); s.set_defaults(f=cmd_install)
    s = sp.add_parser("sync", help="bridge the prefix's plugins to Linux DAWs with yabridge")
    s.add_argument("dirs", nargs="*", help="extra plugin directories"); s.add_argument("--daw-env", action="store_true", help="make DAWs use the app's wine for this prefix (~/.local/bin/wine shim)")
    s.set_defaults(f=cmd_sync)
    sp.add_parser("programs", help="list Windows programs installed in the prefix").set_defaults(f=cmd_programs)
    s = sp.add_parser("run", help="start an installed program (name or part of it)")
    s.add_argument("name"); s.add_argument("--wait", action="store_true"); s.set_defaults(f=cmd_run)
    s = sp.add_parser("uninstall", help="run an installed program's own uninstaller")
    s.add_argument("name"); s.add_argument("--no-sync", action="store_true"); s.set_defaults(f=cmd_uninstall)
    s = sp.add_parser("install-program", help="run any Windows installer (.exe or .msi) in the prefix")
    s.add_argument("installer"); s.add_argument("--no-sync", action="store_true"); s.set_defaults(f=cmd_install_program)
    sp.add_parser("status", help="yabridge status").set_defaults(f=cmd_status)
    s = sp.add_parser("dxvk", help="Direct3D on Vulkan for plugin GUIs that Wine draws wrong")
    s.add_argument("action", nargs="?", default="install", choices=["install", "remove", "status"])
    s.add_argument("--force", action="store_true", help="reinstall even if the pinned version is already there")
    s.set_defaults(f=cmd_dxvk)
    s = sp.add_parser("report", help="write a diagnostic bundle to send with a bug report")
    s.add_argument("-o", "--output", help="where to write it (default: ~/nilinux-report-<date>.tar.gz)")
    s.add_argument("--summary-only", action="store_true", help="print the summary instead of writing a file")
    s.set_defaults(f=cmd_report)
    sp.add_parser("rescue-install", help="an NI installer has stopped responding: stop it and finish the install from its downloaded payload").set_defaults(f=cmd_rescue)
    sp.add_parser("prefixes", help="which nilinux prefixes exist, which one runs the NI daemon, what yabridge points at").set_defaults(f=cmd_prefixes)
    s = sp.add_parser("finish-installs", help="complete installs Native Access started but did not finish")
    s.add_argument("--no-sync", action="store_true"); s.set_defaults(f=cmd_finish_installs)
    a = ap.parse_args(argv); a.f(a)

if __name__ == "__main__": main()
