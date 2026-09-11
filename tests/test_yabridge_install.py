import json, os, tempfile, unittest
from pathlib import Path
from nilinux import yabridge

class YabridgeInstallTest(unittest.TestCase):
    def test_pinned_wine_version_parses_build_name(self):
        v = yabridge.pinned_wine_version()
        self.assertRegex(v, r"^\d+\.\d+")

    def test_needs_master(self):
        self.assertFalse(yabridge.needs_master("9.21"))
        self.assertTrue(yabridge.needs_master("9.22"))
        self.assertTrue(yabridge.needs_master("11.17"))
        self.assertFalse(yabridge.needs_master(""))

    def test_pick_asset_exact_wine_version_only(self):
        assets = [{"name": "nilinux.flatpak", "browser_download_url": "u0"},
                  {"name": "yabridge-b580a9f-wine-11.16.tar.gz", "browser_download_url": "u1"},
                  {"name": "yabridge-b580a9f-wine-11.17.tar.gz", "browser_download_url": "u2"}]
        self.assertEqual(yabridge.pick_asset(assets, "11.17")["browser_download_url"], "u2")
        self.assertIsNone(yabridge.pick_asset(assets, "11.18"))
        self.assertIsNone(yabridge.pick_asset([], "11.17"))

    def test_compatibility_with_marker(self):
        with tempfile.TemporaryDirectory() as d:
            old_dir, old_yctl, old_marker = yabridge.YAB_DIR, yabridge.YCTL, yabridge.MARKER
            yabridge.YAB_DIR = Path(d); yabridge.YCTL = Path(d) / "yabridgectl"; yabridge.MARKER = Path(d) / "nilinux-build.json"
            try:
                self.assertEqual(yabridge.compatibility()[0], False)          # not installed
                yabridge.YCTL.write_text("")
                ok, detail = yabridge.compatibility()                          # upstream release, new wine
                self.assertFalse(ok); self.assertIn("mouse clicks", detail)
                yabridge.MARKER.write_text(json.dumps({"yabridge_commit": "abc1234", "wine_version": yabridge.pinned_wine_version()}))
                ok, detail = yabridge.compatibility()
                self.assertTrue(ok); self.assertIn("abc1234", detail)
                self.assertIn("abc1234", yabridge.installed())
                yabridge.MARKER.write_text(json.dumps({"yabridge_commit": "abc1234", "wine_version": "9.21"}))
                ok, detail = yabridge.compatibility()
                self.assertFalse(ok); self.assertIn("built for wine 9.21", detail)
            finally:
                yabridge.YAB_DIR, yabridge.YCTL, yabridge.MARKER = old_dir, old_yctl, old_marker

if __name__ == "__main__": unittest.main()
