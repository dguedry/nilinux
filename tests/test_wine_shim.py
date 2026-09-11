"""The ~/.local/bin/wine shim routes by prefix: only the app's prefix runs the
app's wine; everything else falls through to the next wine on PATH."""
import os, stat, subprocess, tempfile, unittest
from pathlib import Path
from nilinux import yabridge
from nilinux.wine import Prefix, WineBuild

def _script(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

class WineShimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.home = root / "home"; (self.home / ".wine").mkdir(parents=True)
        build = root / "wine-build"
        _script(build / "bin" / "wine", 'echo "app wine prefix=$WINEPREFIX fsync=$WINEFSYNC args=$*"\n')
        self.prefix = Prefix(root / "prefix", WineBuild(build)); (self.prefix.path / "drive_c").mkdir(parents=True)
        self.hostbin = root / "usr-bin"
        _script(self.hostbin / "wine", 'echo "host wine prefix=$WINEPREFIX fsync=${WINEFSYNC-unset} args=$*"\n')
        self.shim = self.home / ".local/bin/wine"
        self._old = yabridge.WINE_SHIM; yabridge.WINE_SHIM = self.shim
        self.assertTrue(yabridge.configure_daw_environment(self.prefix))
        self.assertTrue(yabridge.shim_is_ours(self.prefix))

    def tearDown(self):
        yabridge.WINE_SHIM = self._old; self.tmp.cleanup()

    def run_wine(self, wineprefix=None, path_extra=True):
        env = {"HOME": str(self.home),
               "PATH": f"{self.shim.parent}:{self.hostbin}" if path_extra else str(self.shim.parent)}
        if wineprefix is not None: env["WINEPREFIX"] = str(wineprefix)
        cp = subprocess.run(["wine", "--version"], env=env, capture_output=True, text=True, timeout=30)
        return cp.returncode, cp.stdout.strip(), cp.stderr.strip()

    def test_app_prefix_goes_to_app_wine(self):
        rc, out, _ = self.run_wine(self.prefix.path)
        self.assertEqual(rc, 0); self.assertTrue(out.startswith("app wine"), out)
        self.assertIn("fsync=1", out); self.assertIn("args=--version", out)
        # a path *inside* the prefix counts too (drive_c, dosdevices, …)
        rc, out, _ = self.run_wine(self.prefix.path / "drive_c")
        self.assertTrue(out.startswith("app wine"), out)

    def test_foreign_prefix_goes_to_host_wine(self):
        other = self.home / "games-prefix"; other.mkdir()
        rc, out, _ = self.run_wine(other)
        self.assertEqual(rc, 0); self.assertTrue(out.startswith("host wine"), out)
        self.assertIn("fsync=unset", out, "the shim must not leak WINEFSYNC into other prefixes")

    def test_unset_prefix_is_the_users_own(self):
        rc, out, _ = self.run_wine(None)
        self.assertTrue(out.startswith("host wine"), out)

    def test_nonexistent_prefix_is_foreign(self):
        rc, out, _ = self.run_wine(self.home / "does-not-exist")
        self.assertTrue(out.startswith("host wine"), out)

    def test_app_wine_missing_falls_through(self):
        os.remove(self.prefix.build.root / "bin" / "wine")
        rc, out, _ = self.run_wine(self.prefix.path)
        self.assertEqual(rc, 0); self.assertTrue(out.startswith("host wine"), out)

    def test_no_other_wine_on_path(self):
        rc, out, err = self.run_wine(self.home / ".wine", path_extra=False)
        self.assertEqual(rc, 127); self.assertIn("no other wine on PATH", err)

    def test_foreign_shim_is_left_alone(self):
        _script(self.shim, "echo someone else's\n")
        self.assertFalse(yabridge.shim_is_ours(self.prefix))
        self.assertFalse(yabridge.configure_daw_environment(self.prefix))
        self.assertIn("someone else", self.shim.read_text())

    def test_legacy_environment_d_removed(self):
        legacy = self.home / ".config/environment.d/50-nilinux.conf"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(yabridge.legacy_daw_environment_content(self.prefix))
        old = yabridge.daw_environment_file
        yabridge.daw_environment_file = lambda: legacy
        try:
            self.assertTrue(yabridge.configure_daw_environment(self.prefix))
            self.assertFalse(legacy.exists())
            legacy.write_text("WINELOADER=/somewhere/else/wine\n")     # not ours: kept
            self.assertFalse(yabridge.configure_daw_environment(self.prefix))
            self.assertTrue(legacy.exists())
        finally:
            yabridge.daw_environment_file = old

    def test_status_active_with_our_shim(self):
        st, detail = yabridge.daw_environment_status(self.prefix)
        self.assertEqual(st, "active"); self.assertIn("other prefixes keep", detail)

if __name__ == "__main__":
    unittest.main()
