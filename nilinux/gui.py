"""GTK4 / libadwaita front end. Thin: every action calls the package."""
import subprocess, sys, threading
from pathlib import Path
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Gio
from . import __version__, paths, wine, native_access as na, products, yabridge, doctor
from .progress import Reporter, OK, FAIL, SKIP, RUN

APP_ID = "io.github.dguedry.nilinux"
TITLE = "NI on Linux"

def ui(fn, *a):
    """Run fn(*a) on the main loop."""
    GLib.idle_add(lambda: (fn(*a), False)[1])

class GuiReporter(Reporter):
    """Feeds a step list + log view on the main loop."""
    ICON = {OK: "emblem-ok-symbolic", FAIL: "dialog-error-symbolic", SKIP: "radio-symbolic", RUN: "content-loading-symbolic"}
    def __init__(self, group: Adw.PreferencesGroup, log: Gtk.TextBuffer, progress: Gtk.ProgressBar | None = None):
        super().__init__(); self.group, self.logbuf, self.bar = group, log, progress; self.rows = {}
    def on_step(self, s):
        def go():
            row = self.rows.get(id(s))
            if row is None:
                row = Adw.ActionRow(title=s.name); img = Gtk.Image(); row.add_suffix(img); row.img = img
                self.group.add(row); self.rows[id(s)] = row
            row.img.set_from_icon_name(self.ICON[s.status]); row.set_subtitle(s.detail or "")
            if s.status == FAIL: row.add_css_class("error")
        ui(go)
    def on_log(self, line): ui(lambda: self.logbuf.insert(self.logbuf.get_end_iter(), line + "\n"))
    def on_progress(self, done, total, label):
        if self.bar and total: ui(lambda: (self.bar.set_fraction(done / total), self.bar.set_text(f"{label} {done/1e6:.0f}/{total/1e6:.0f} MB")))

class TaskPage(Gtk.Box):
    """Step list + progress bar + collapsible log; used by setup and installs."""
    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18, margin_start=24, margin_end=24)
        self.group = Adw.PreferencesGroup(title=title); self.append(self.group)
        self.bar = Gtk.ProgressBar(show_text=True); self.append(self.bar)
        self.logbuf = Gtk.TextBuffer(); tv = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True)
        sw = Gtk.ScrolledWindow(min_content_height=140, child=tv); exp = Gtk.Expander(label="Details", child=sw); self.append(exp)
        self.reporter = GuiReporter(self.group, self.logbuf, self.bar)
    def reset(self, title=None):
        for r in list(self.reporter.rows.values()): self.group.remove(r)
        self.reporter.rows.clear(); self.reporter.steps.clear(); self.bar.set_fraction(0); self.bar.set_text("")
        if title: self.group.set_title(title)

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=TITLE, default_width=860, default_height=640)
        self.toasts = Adw.ToastOverlay(); self.set_content(self.toasts)
        self.busy = False
        tv = Adw.ToolbarView(); self.toasts.set_child(tv)
        self.stack = Adw.ViewStack()
        header = Adw.HeaderBar(); switcher = Adw.ViewSwitcherTitle(stack=self.stack, title=TITLE); header.set_title_widget(switcher)
        menu = Gio.Menu(); menu.append("Re-run setup / repair", "app.setup"); menu.append("Make DAWs use this wine", "app.dawenv"); menu.append("About", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        tv.add_top_bar(header); tv.set_content(self.stack)
        self.stack.add_titled_with_icon(self.build_plugins(), "plugins", "Plugins", "audio-x-generic-symbolic")
        self.stack.add_titled_with_icon(self.build_install(), "install", "Install", "list-add-symbolic")
        self.stack.add_titled_with_icon(self.build_health(), "health", "Health", "emblem-ok-symbolic")
        self.task = TaskPage("Working"); self.stack.add_titled_with_icon(self.task, "task", "Progress", "content-loading-symbolic")
        self.stack.add_named(self.build_get_na(), "getna")   # not in the switcher; shown until NA is installed
        b = wine.installed_build(); self.prefix = wine.Prefix(paths.PREFIX, b) if b else None
        if not self.is_ready(): self.run_setup(first=True)
        else: self.refresh_all()
        import os
        if os.environ.get("NILINUX_PAGE"): self.stack.set_visible_child_name(os.environ["NILINUX_PAGE"])
        act = os.environ.get("NILINUX_ACTION")   # test hook: run an action at startup
        if act: GLib.timeout_add(500, lambda: ({"sync": self.sync, "setup": self.run_setup, "na": self.open_na, "getna": lambda: self.stack.set_visible_child_name("getna")}[act](), False)[1])

    # ---- state ------------------------------------------------------------------
    def is_ready(self): return self.prefix is not None and self.prefix.exists and na.na_exe(self.prefix).exists()
    def toast(self, text): ui(lambda: self.toasts.add_toast(Adw.Toast(title=text, timeout=4)))
    def run_bg(self, title, fn, done=None):
        """Run fn(reporter) in a thread on the Progress page."""
        if self.busy: self.toast("Another task is still running"); return
        self.busy = True; self.task.reset(title); self.stack.set_visible_child_name("task")
        def worker():
            err = None
            try: result = fn(self.task.reporter)
            except Exception as e: err = e; result = None
            def finish():
                self.busy = False
                if err: self.task.reporter.step(f"Error: {err}"); self.task.reporter.fail()
                if done: done(result, err)
                self.refresh_all()
            ui(finish)
        threading.Thread(target=worker, daemon=True).start()

    # ---- setup ---------------------------------------------------------------------
    def run_setup(self, first=False):
        """Prepare the environment (wine, prefix, fonts, C runtime). Native Access
        itself is never downloaded by this app: the user gets it from NI."""
        def fn(r):
            b = wine.provision(r); self.prefix = wine.Prefix(paths.PREFIX, b); na.prepare(self.prefix, r)
            if na.na_exe(self.prefix).exists(): na.install_native_access_fixes(self.prefix, r)
            return True
        def done(res, err):
            if err or self.task.reporter.failed: return
            if self.is_ready(): self.toast("Environment ready"); self.stack.set_visible_child_name("install")
            else: self.stack.set_visible_child_name("getna")
        self.run_bg("Preparing the environment" if first else "Repairing setup", fn, done)

    # ---- get Native Access page ------------------------------------------------------
    def build_get_na(self):
        page = Adw.StatusPage(icon_name="folder-download-symbolic", title="Get Native Access",
            description="Native Access is Native Instruments' installer for your products. It is not bundled with this app: "
                        "download it from Native Instruments, then choose the downloaded file here. Everything else is automatic.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        link = Gtk.Button(label="Open the Native Access download page", css_classes=["pill", "suggested-action"])
        link.connect("clicked", lambda *_: self.open_url(na.NA_DOWNLOAD_PAGE)); box.append(link)
        pick = Gtk.Button(label="Choose the downloaded installer…", css_classes=["pill"])
        pick.connect("clicked", lambda *_: self.pick_file("Choose Native-Access-latest.exe", self.install_na, downloads=True)); box.append(pick)
        note = Gtk.Label(label="Windows version (Native-Access-latest.exe). It lands in your Downloads folder.", css_classes=["dim-label", "caption"]); box.append(note)
        v = na.latest_version()
        if v: box.append(Gtk.Label(label=f"Current version: {v}", css_classes=["dim-label", "caption"]))
        page.set_child(box); return page
    def open_url(self, url):
        Gtk.UriLauncher(uri=url).launch(self, None, lambda l, res: (l.launch_finish(res) if True else None))
    def install_na(self, path):
        def fn(r):
            b = wine.provision(r); self.prefix = self.prefix or wine.Prefix(paths.PREFIX, b)
            na.install_native_access(self.prefix, path, r); return True
        def done(res, err):
            if not err and not self.task.reporter.failed: self.toast("Native Access installed — open it to sign in"); self.stack.set_visible_child_name("install")
        self.run_bg(f"Installing Native Access from {path.name}", fn, done)

    # ---- plugins page ------------------------------------------------------------------
    def build_plugins(self):
        page = Adw.PreferencesPage()
        self.products_group = Adw.PreferencesGroup(title="Installed products", description="From Native Access. Libraries are registered for Kontakt automatically.")
        page.add(self.products_group)
        self.bridged_group = Adw.PreferencesGroup(title="Bridged plugins", description="Available to Linux DAWs through yabridge (~/.vst, ~/.vst3, ~/.clap).")
        page.add(self.bridged_group)
        self._rows = {self.products_group: [], self.bridged_group: []}
        return page
    def _fill(self, group, rows):
        for r in self._rows[group]: group.remove(r)
        self._rows[group] = []
        for r in rows: group.add(r); self._rows[group].append(r)
    def refresh_plugins(self):
        if not self.is_ready(): return
        def work():
            prods = products.installed(self.prefix); bridged = yabridge.bridged(self.prefix)
            def show():
                rows = []
                for p in prods:
                    row = Adw.ActionRow(title=GLib.markup_escape_text(p.name), subtitle=GLib.markup_escape_text(f"{p.type or '?'} {p.version}".strip()))
                    for txt, ok in (("registered" if p.registered else "not registered", p.registered), ("licensed" if p.licensed else "no license", p.licensed)):
                        lbl = Gtk.Label(label=txt, css_classes=["caption", "dim-label" if ok else "warning"]); row.add_suffix(lbl)
                    rows.append(row)
                if not rows: rows.append(Adw.ActionRow(title="Nothing installed yet", subtitle="Open Native Access from the Install tab"))
                self._fill(self.products_group, rows)
                brows = [Adw.ActionRow(title=GLib.markup_escape_text(b["name"]), subtitle=GLib.markup_escape_text(b["info"])) for b in bridged]
                if not brows: brows.append(Adw.ActionRow(title="No bridged plugins yet", subtitle="Install a product, then Sync"))
                self._fill(self.bridged_group, brows)
            ui(show)
        threading.Thread(target=work, daemon=True).start()

    # ---- install page -------------------------------------------------------------------
    def build_install(self):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Native Instruments", description="Products from your NI account.")
        r = Adw.ActionRow(title="Open Native Access", subtitle="Sign in, install or update products. When you close it, libraries are registered and plugins bridged automatically.", activatable=True)
        r.add_suffix(Gtk.Image(icon_name="go-next-symbolic")); r.connect("activated", lambda *_: self.open_na() if self.is_ready() else self.stack.set_visible_child_name("getna")); g.add(r)
        r = Adw.ActionRow(title="Get Native Access from Native Instruments", subtitle=na.NA_DOWNLOAD_PAGE, activatable=True)
        r.add_suffix(Gtk.Image(icon_name="web-browser-symbolic")); r.connect("activated", lambda *_: self.open_url(na.NA_DOWNLOAD_PAGE)); g.add(r)
        r = Adw.ActionRow(title="Install or update Native Access from a downloaded installer", subtitle="Pick Native-Access-latest.exe from your Downloads folder.", activatable=True)
        self.na_update_row = r
        r.add_suffix(Gtk.Image(icon_name="document-open-symbolic")); r.connect("activated", lambda *_: self.pick_file("Choose Native-Access-latest.exe", self.install_na, downloads=True)); g.add(r)
        r = Adw.ActionRow(title="Install an NI application from its installer", subtitle="For Kontakt, Reaktor, Massive… when Native Access's own install fails. Pick the .zip or 'Setup PC.exe'.", activatable=True)
        r.add_suffix(Gtk.Image(icon_name="document-open-symbolic")); r.connect("activated", lambda *_: self.pick_file("Choose NI installer", self.install_ni)); g.add(r)
        page.add(g)
        g = Adw.PreferencesGroup(title="Other plugins", description="Any Windows VST2 / VST3 / CLAP installer.")
        r = Adw.ActionRow(title="Run a plugin installer", subtitle="The installer's own window opens; plugins are bridged when it finishes.", activatable=True)
        r.add_suffix(Gtk.Image(icon_name="document-open-symbolic")); r.connect("activated", lambda *_: self.pick_file("Choose installer", self.install_third_party)); g.add(r)
        r = Adw.ActionRow(title="Add a plugin folder", subtitle="If an installer put VST2 .dlls somewhere unusual inside the prefix.", activatable=True)
        r.add_suffix(Gtk.Image(icon_name="folder-open-symbolic")); r.connect("activated", lambda *_: self.pick_folder()); g.add(r)
        r = Adw.ActionRow(title="Bridge plugins now", subtitle="Re-scan the prefix and update the DAW-visible plugins.", activatable=True)
        r.add_suffix(Gtk.Image(icon_name="view-refresh-symbolic")); r.connect("activated", lambda *_: self.sync()); g.add(r)
        page.add(g)
        return page
    def open_na(self):
        if not self.is_ready(): self.toast("Run setup first"); return
        try: proc = na.launch(self.prefix)
        except Exception as e: self.toast(str(e)); return
        self.toast("Native Access is starting…")
        import time; t0 = time.time()
        def watch():
            proc.wait()
            if time.time() - t0 < 15:
                # exited at once: NA was already running (its single-instance logic focused it)
                self.toast("Native Access is already open"); return
            # NA's parent wine process exits when the app closes; then do the invisible
            # bookkeeping — on the main loop, never touch GTK from this thread
            ui(lambda: self.run_bg("Finishing up after Native Access",
                                   lambda r: (products.register_all_libraries(self.prefix, r), yabridge.sync(self.prefix, r))))
        threading.Thread(target=watch, daemon=True).start()
    def pick_file(self, title, cb, downloads=False):
        d = Gtk.FileDialog(title=title)
        dl = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if downloads and dl: d.set_initial_folder(Gio.File.new_for_path(dl))
        def on(dlg, res):
            try: f = dlg.open_finish(res)
            except GLib.Error: return
            cb(Path(f.get_path()))
        d.open(self, None, on)
    def pick_folder(self):
        d = Gtk.FileDialog(title="Choose plugin folder", initial_folder=Gio.File.new_for_path(str(self.prefix.drive_c / "Program Files")))
        def on(dlg, res):
            try: f = dlg.select_folder_finish(res)
            except GLib.Error: return
            self.run_bg("Bridging plugins", lambda r: yabridge.sync(self.prefix, r, extras=[f.get_path()]))
        d.select_folder(self, None, on)
    def install_ni(self, path):
        def fn(r):
            res = products.install_app(self.prefix, path, r); yabridge.sync(self.prefix, r); return res
        self.run_bg(f"Installing {path.name}", fn, lambda res, err: self.toast(f"{res['name']} installed ({res['method']})") if res else None)
    def install_third_party(self, path):
        def fn(r):
            products.run_installer(self.prefix, path, r); return yabridge.sync(self.prefix, r)
        self.run_bg(f"Installing {path.name}", fn)
    def sync(self): self.run_bg("Bridging plugins", lambda r: yabridge.sync(self.prefix, r))
    # ---- health page --------------------------------------------------------------------
    def build_health(self):
        page = Adw.PreferencesPage()
        self.health_group = Adw.PreferencesGroup(title="Checks"); page.add(self.health_group); self._rows[self.health_group] = []
        return page
    def refresh_health(self):
        def work():
            checks = doctor.run(self.prefix)
            def show():
                rows = []
                for c in checks:
                    row = Adw.ActionRow(title=c.name, subtitle=GLib.markup_escape_text(c.detail + (f"  ·  fix: {c.fix}" if not c.ok and c.fix else "")))
                    row.add_suffix(Gtk.Image(icon_name="emblem-ok-symbolic" if c.ok else "dialog-warning-symbolic")); rows.append(row)
                self._fill(self.health_group, rows)
            ui(show)
        threading.Thread(target=work, daemon=True).start()
    def refresh_version_notice(self):
        if not self.is_ready(): return
        def work():
            v = na.version_notice(self.prefix)
            def show():
                if v["newer"]:
                    self.na_update_row.set_subtitle(f"Installed {v['installed']} · {v['latest']} available "
                        + ("(validated on this stack)" if v["latest_known_good"] else "(not yet validated on this stack)") + ". Download it from NI, then pick the file here.")
                elif v["installed"]: self.na_update_row.set_subtitle(f"Installed {v['installed']} (current). Pick Native-Access-latest.exe from your Downloads folder to reinstall.")
            ui(show)
        threading.Thread(target=work, daemon=True).start()
    def refresh_all(self): self.refresh_plugins(); self.refresh_health(); self.refresh_version_notice()

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        for name, cb in (("setup", lambda *_: self.win.run_setup()), ("dawenv", lambda *_: self.win.run_bg("DAW environment", lambda r: yabridge.configure_daw_environment(self.win.prefix, r))), ("about", self.about)):
            act = Gio.SimpleAction(name=name); act.connect("activate", cb); self.add_action(act)
        # Ctrl+1..4 switch tabs (keyboard access to the view switcher)
        for i, page in enumerate(("plugins", "install", "health", "task"), start=1):
            act = Gio.SimpleAction(name=f"tab{i}"); act.connect("activate", lambda *_, p=page: self.win.stack.set_visible_child_name(p))
            self.add_action(act); self.set_accels_for_action(f"app.tab{i}", [f"<Control>{i}"])
    def do_activate(self):
        self.win = Window(self); self.win.present()
    def about(self, *_):
        Adw.AboutWindow(transient_for=self.win, application_name=TITLE, version=__version__,
                        comments="Native Instruments' Native Access, products and Windows VSTs on Linux — without touching Wine yourself.").present()

def main():
    paths.ensure_dirs(); sys.exit(App().run(sys.argv))

if __name__ == "__main__": main()
