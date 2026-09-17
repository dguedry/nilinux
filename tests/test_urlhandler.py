"""Native Access signs in through the browser, which calls back to
native-access://. Without a handler registered with the desktop the login never
returns; these cover installing and removing that handler."""
import tempfile, unittest
from pathlib import Path
from unittest import mock

from nilinux import urlhandler
from nilinux.wine import Prefix, WineBuild

class UrlHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        (root / "wine/bin").mkdir(parents=True)                  # WineBuild wants a real binary
        (root / "wine/bin/wine").write_text("#!/bin/sh\n"); (root / "wine/bin/wine").chmod(0o755)
        self.p = Prefix(root / "prefix", WineBuild(root / "wine"))
        (self.p.drive_c / "Program Files/Native Instruments/Native Access").mkdir(parents=True)
        self.script = root / ".local/bin/nilinux-url-handler"
        self.apps = root / ".local/share/applications"
        self.desktop = self.apps / "io.github.dguedry.nilinux.url-handler.desktop"
        self.patches = [mock.patch.object(urlhandler, "SCRIPT", self.script),
                        mock.patch.object(urlhandler, "APPS", self.apps),
                        mock.patch.object(urlhandler, "DESKTOP", self.desktop),
                        mock.patch.object(urlhandler.shutil, "which", return_value=None),   # no xdg tools in tests
                        mock.patch.object(urlhandler.subprocess, "run", return_value=mock.Mock(stdout="", returncode=0))]
        for x in self.patches: x.start()

    def tearDown(self):
        for x in self.patches: x.stop()
        self.tmp.cleanup()

    def test_register_writes_an_executable_handler_and_a_desktop_file(self):
        self.assertTrue(urlhandler.register(self.p))
        self.assertTrue(self.script.exists() and self.desktop.exists())
        self.assertTrue(self.script.stat().st_mode & 0o111, "handler must be executable")
        body = self.script.read_text()
        self.assertIn(str(self.p.path), body)                       # this prefix
        self.assertIn("Native Access.exe", body)
        self.assertIn('"$1"', body)                                 # the URL is passed through
        self.assertIn("x-scheme-handler/native-access", self.desktop.read_text())

    def test_register_is_idempotent(self):
        urlhandler.register(self.p); first = self.script.read_text()
        urlhandler.register(self.p)
        self.assertEqual(self.script.read_text(), first)

    def test_status_reports_a_foreign_handler(self):
        urlhandler.register(self.p)
        with mock.patch.object(urlhandler.subprocess, "run",
                               return_value=mock.Mock(stdout="someone-elses.desktop\n", returncode=0)):
            st = urlhandler.status(self.p)
            self.assertTrue(st["installed"]); self.assertTrue(st["foreign"]); self.assertFalse(st["ok"])

    def test_unregister_removes_only_our_files(self):
        urlhandler.register(self.p)
        self.assertTrue(urlhandler.unregister())
        self.assertFalse(self.script.exists()); self.assertFalse(self.desktop.exists())
        self.assertFalse(urlhandler.unregister())          # nothing left to do

    def test_unregister_leaves_a_handler_we_did_not_write(self):
        self.script.parent.mkdir(parents=True, exist_ok=True)
        self.script.write_text("#!/bin/sh\n# someone else's script\n")
        urlhandler.unregister()
        self.assertTrue(self.script.exists(), "must not delete a script that is not ours")

if __name__ == "__main__": unittest.main()
