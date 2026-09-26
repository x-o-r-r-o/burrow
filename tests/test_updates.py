"""Tests for app update checking and installing. Run: /usr/bin/python3 -m unittest discover tests"""

import os
import plistlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("alfred_workflow_cache", tempfile.mkdtemp())

import engine  # noqa: E402
import updates  # noqa: E402

APPCAST = b"""<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" version="2.0"><channel>
<item><title>2.0</title><sparkle:version>200</sparkle:version><sparkle:shortVersionString>2.0</sparkle:shortVersionString>
  <enclosure url="https://example.com/App-2.0.zip" length="1" type="application/octet-stream"/></item>
<item><title>3.0 beta</title><sparkle:version>300</sparkle:version><sparkle:shortVersionString>3.0b1</sparkle:shortVersionString>
  <sparkle:channel>beta</sparkle:channel><enclosure url="https://example.com/App-3.0b1.zip"/></item>
<item><title>2.5</title><sparkle:version>250</sparkle:version><sparkle:shortVersionString>2.5</sparkle:shortVersionString>
  <sparkle:minimumSystemVersion>99.0</sparkle:minimumSystemVersion><enclosure url="https://example.com/App-2.5.zip"/></item>
</channel></rss>"""


class VersionTest(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(updates.newer("2.10.1", "2.9"))
        self.assertTrue(updates.newer("1.2.1", "1.2"))
        self.assertFalse(updates.newer("1.2.0", "1.2"))
        self.assertFalse(updates.newer("1.2", "1.2.0"))
        self.assertTrue(updates.newer("1.0", "1.0b3"))
        self.assertFalse(updates.newer("1.0b3", "1.0"))
        self.assertTrue(updates.newer("v3.6.6-8b85519e", "3.6.4"))
        self.assertTrue(updates.newer("5.3.2,1234", "5.3.1"))
        self.assertFalse(updates.newer("", "1.0"))
        self.assertTrue(updates.newer("2.0.0-rc.1", "2.0.0-beta.2"))
        self.assertTrue(updates.newer("6.1.0-2", "6.1.0-1"))
        self.assertTrue(updates.newer("2024.10", "2024.9"))


class SourcesTest(unittest.TestCase):
    def test_sparkle_skips_betas_and_unsupported_os(self):
        real = updates.fetch
        updates.fetch = lambda url, **k: APPCAST
        try:
            r = updates.check_sparkle({"name": "App", "version": "1.0", "build": "100", "feed": "https://example.com/appcast.xml"})
            self.assertEqual((r["version"], r["build"], r["url"]), ("2.0", "200", "https://example.com/App-2.0.zip"))
            r = updates.check_sparkle({"name": "App", "version": "2.0", "build": "200", "feed": "https://example.com/appcast.xml"})
            self.assertEqual(r, {"current": True})
        finally:
            updates.fetch = real

    def test_catalog_only_trusts_similar_version_shapes(self):
        index = {"App.app": {"token": "app", "version": "2.1.0,4567", "url": "https://example.com/a.dmg", "sha256": "ab", "bids": ["com.example.app"]}}
        app = {"path": "/Applications/App.app", "version": "2.0.1", "build": "400", "bundle_id": "com.example.app"}
        self.assertEqual(updates.check_catalog(app, index)["version"], "2.1.0")
        index["App.app"]["version"] = "4567"  # build-number-only cask versions are ignored
        self.assertIsNone(updates.check_catalog(app, index))

    def test_catalog_needs_matching_bundle_id(self):
        # "Helium.app" in the catalog is a different app than the Helium browser
        index = {"Helium.app": {"token": "helium", "version": "1.0.0", "url": "https://x/h.dmg", "bids": ["com.koushikdutta.helium"]}}
        app = {"path": "/Applications/Helium.app", "version": "0.18.1", "build": "1", "bundle_id": "net.imput.helium"}
        self.assertIsNone(updates.check_catalog(app, index))
        index["Helium.app"]["bids"] = ["net.imput.helium"]
        self.assertEqual(updates.check_catalog(app, index)["version"], "1.0.0")

    def test_self_update_never_waits_for_network_offline(self):
        real = updates.fetch_json
        updates.fetch_json = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used"))
        try:
            self.assertIsNone(updates.check_self(network=False))
        finally:
            updates.fetch_json = real

    def test_pick_asset_prefers_native_arch(self):
        assets = [("App-1.0-x64.zip", "u-x64"), ("App-1.0-arm64.zip", "u-arm"), ("App-1.0-universal.dmg", "u-uni"), ("App-1.0.zip.blockmap", "b")]
        chosen = updates.pick_asset(assets)
        self.assertEqual(chosen, "u-arm" if updates.ARCH == "arm64" else "u-x64")


class InstallSafetyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.real = (updates.download, updates.extract_app, updates.signing, updates.gatekeeper_ok)
        self.new_app = self.make_app("new", "com.example.app", "2.0")
        updates.download = lambda url, dest, sha256=None: dest
        updates.extract_app = lambda archive, work, bid: (self.new_app, lambda: None)
        updates.gatekeeper_ok = lambda p: True

    def tearDown(self):
        updates.download, updates.extract_app, updates.signing, updates.gatekeeper_ok = self.real
        shutil.rmtree(self.root, ignore_errors=True)

    def make_app(self, folder, bid, version):
        app = os.path.join(self.root, folder, "App.app")
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bid, "CFBundleShortVersionString": version, "CFBundleVersion": version}, f)
        return app

    def update(self):
        old = self.make_app("old", "com.example.app", "1.0")
        return {"path": old, "url": "https://example.com/App.zip", "installable": True, "version": "2.0", "source": "Sparkle"}

    def test_rejects_other_developer(self):
        updates.signing = lambda p: (True, "TEAMAAAAAA" if "/old/" in p else "TEAMBBBBBB", "x")
        with self.assertRaisesRegex(updates.UpdateError, "different developer"):
            updates.prepare(self.update())

    def test_rejects_invalid_signature(self):
        updates.signing = lambda p: (("/old/" in p), "TEAMAAAAAA", "x")
        with self.assertRaisesRegex(updates.UpdateError, "signature"):
            updates.prepare(self.update())

    def test_rejects_not_newer(self):
        updates.signing = lambda p: (True, "TEAMAAAAAA", "x")
        u = self.update()
        self.new_app = self.make_app("same", "com.example.app", "1.0")
        with self.assertRaisesRegex(updates.UpdateError, "isn't newer"):
            updates.prepare(u)

    def test_rejects_gatekeeper_regression(self):
        updates.signing = lambda p: (True, "TEAMAAAAAA", "x")
        updates.gatekeeper_ok = lambda p: "/old/" in p
        with self.assertRaisesRegex(updates.UpdateError, "Gatekeeper"):
            updates.prepare(self.update())

    def test_rejects_plain_http(self):
        u = self.update()
        u["url"] = "http://example.com/App.zip"
        with self.assertRaisesRegex(updates.UpdateError, "HTTPS"):
            updates.prepare(u)

    def test_rejects_update_for_newer_macos(self):
        updates.signing = lambda p: (True, "TEAMAAAAAA", "x")
        with open(os.path.join(self.new_app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        info["LSMinimumSystemVersion"] = "99.0"
        with open(os.path.join(self.new_app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump(info, f)
        with self.assertRaisesRegex(updates.UpdateError, "needs macOS 99.0"):
            updates.prepare(self.update())

    def test_one_install_per_app_at_a_time(self):
        engine.CACHE_DIR = tempfile.mkdtemp()
        held = updates._AppLock("/Applications/Same.app")
        try:
            with self.assertRaisesRegex(updates.UpdateError, "already being updated"):
                updates._AppLock("/Applications/Same.app")
        finally:
            held.release()

    def test_accepts_matching_update(self):
        updates.signing = lambda p: (True, "TEAMAAAAAA", "x")
        staged, new, cleanup = updates.prepare(self.update())
        self.assertEqual(new["version"], "2.0")


@unittest.skipUnless(os.access(os.path.join(os.path.dirname(__file__), "..", "build", "bin", "BurrowTrash"), os.X_OK), "build first")
class RollbackTest(unittest.TestCase):
    def test_undo_puts_the_old_version_back(self):
        engine.TRASH_HELPER = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "build", "bin", "BurrowTrash"))
        engine.CACHE_DIR = tempfile.mkdtemp()
        root = tempfile.mkdtemp(dir=os.path.expanduser("~/Library/Caches"))
        try:
            app = os.path.join(root, "Thing.app")
            os.makedirs(os.path.join(app, "Contents"))
            with open(os.path.join(app, "Contents", "v"), "w") as f:
                f.write("old")
            batch = engine.new_batch_id()
            self.assertEqual(engine.trash_paths([app], label="Update Thing", batch_id=batch), [])
            os.makedirs(os.path.join(app, "Contents"))  # the "new version" installed in its place
            with open(os.path.join(app, "Contents", "v"), "w") as f:
                f.write("new")
            updates.mark_replacement(batch)
            restored, skipped = engine.undo_trash_batch(engine.last_trash_batch())
            with open(os.path.join(app, "Contents", "v")) as f:
                self.assertEqual(f.read(), "old")
            # the new version went to the Trash; take it back out and delete it
            batch = engine.last_trash_batch()
            if batch:
                engine.undo_trash_batch(batch)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
