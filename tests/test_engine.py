"""Unit tests for the Burrow engine's pure logic.  Run: /usr/bin/python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("alfred_workflow_cache", tempfile.mkdtemp())

import burrow  # noqa: E402
import engine  # noqa: E402

# Use the compiled trash helper from the last build when testing from src/.
_built_helper = os.path.join(os.path.dirname(__file__), "..", "build", "bin", "BurrowTrash")
if not os.access(engine.TRASH_HELPER, os.X_OK) and os.access(_built_helper, os.X_OK):
    engine.TRASH_HELPER = os.path.abspath(_built_helper)


class ArtifactKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def touch(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return path

    def test_node_modules_always(self):
        self.assertEqual(engine.artifact_kind(self.tmp, "node_modules", os.path.join(self.tmp, "node_modules")), "node_modules")

    def test_dist_needs_package_json(self):
        dist = os.path.join(self.tmp, "dist")
        self.assertIsNone(engine.artifact_kind(self.tmp, "dist", dist))
        self.touch("package.json")
        self.assertEqual(engine.artifact_kind(self.tmp, "dist", dist), "dist")

    def test_target_needs_cargo_or_maven(self):
        target = os.path.join(self.tmp, "target")
        self.assertIsNone(engine.artifact_kind(self.tmp, "target", target))
        self.touch("Cargo.toml")
        self.assertEqual(engine.artifact_kind(self.tmp, "target", target), "target")

    def test_venv_needs_pyvenv_cfg(self):
        venv = os.path.join(self.tmp, "venv")
        os.makedirs(venv)
        self.assertIsNone(engine.artifact_kind(self.tmp, "venv", venv))
        self.touch("venv", "pyvenv.cfg")
        self.assertEqual(engine.artifact_kind(self.tmp, "venv", venv), "venv")

    def test_plain_build_folder_is_left_alone(self):
        self.assertIsNone(engine.artifact_kind(self.tmp, "build", os.path.join(self.tmp, "build")))


class PurgeScanTest(unittest.TestCase):
    def test_finds_old_node_modules_and_skips_young(self):
        root = tempfile.mkdtemp()
        old = os.path.join(root, "old-app", "node_modules")
        young = os.path.join(root, "new-app", "node_modules")
        for d in (old, young):
            os.makedirs(d)
            with open(os.path.join(d, "blob"), "wb") as f:
                f.write(os.urandom(2 * 1024 * 1024))
        past = 1_000_000_000
        for d in (old, young):
            open(os.path.join(os.path.dirname(d), "package.json"), "w").close()
        os.utime(os.path.join(os.path.dirname(old), "package.json"), (past, past))
        os.utime(old, (past, past))
        os.utime(os.path.dirname(old), (past, past))
        os.environ["project_dirs"] = root
        try:
            found = list(engine.purge_scan(min_days=7))
        finally:
            del os.environ["project_dirs"]
        self.assertEqual([f["path"] for f in found], [os.path.realpath(old)])
        self.assertEqual(found[0]["project"], "old-app")


class ProjectAgeTest(unittest.TestCase):
    def test_recent_source_edit_counts_even_if_root_is_old(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "src"))
        os.makedirs(os.path.join(root, "node_modules"))
        past = 1_000_000_000
        os.utime(root, (past, past))
        os.utime(os.path.join(root, "node_modules"), (past, past))
        # src/ was just created, so the project counts as touched today
        self.assertGreater(engine.project_mtime(root), past + 86400)


class OwningAppTest(unittest.TestCase):
    apps = {
        "com.brave.Browser": {"name": "Brave Browser", "path": "/Applications/Brave Browser.app"},
        "com.spotify.client": {"name": "Spotify", "path": "/Applications/Spotify.app"},
    }

    def test_exact_bundle_id(self):
        self.assertEqual(engine.owning_app("com.spotify.client", self.apps)["name"], "Spotify")

    def test_bundle_id_prefix(self):
        self.assertEqual(engine.owning_app("com.brave.Browser.origin", self.apps)["name"], "Brave Browser")

    def test_app_name(self):
        self.assertEqual(engine.owning_app("spotify", self.apps)["name"], "Spotify")

    def test_unknown(self):
        self.assertIsNone(engine.owning_app("com.spotifyx", self.apps))

    def test_browser_prefix(self):
        self.assertEqual(engine.browser_for("com.brave.Browser.origin")[0], "Brave Origin cache")
        self.assertEqual(engine.browser_for("com.brave.Browser")[0], "Brave cache")
        self.assertIsNone(engine.browser_for("com.spotify.client"))


class ResidualMatchTest(unittest.TestCase):
    def terms(self, name, bid, executable=None):
        return engine._search_terms(name, {"bundle_id": bid, "bundle_name": name, "display_name": None, "executable": executable or name})

    def test_matches_own_files(self):
        t = self.terms("CoolApp", "com.example.CoolApp")
        for entry in ("com.example.CoolApp.plist", "com.example.CoolApp", "com.example.CoolApp.helper",
                      "ABCDE12345.com.example.CoolApp", "com.example.CoolApp.savedState", "CoolApp"):
            self.assertTrue(engine._matches_app(entry, t), entry)

    def test_other_apps_extensions_dont_match(self):
        # Regression: uninstalling WireGuard used to claim other VPNs' WireGuard extensions.
        t = self.terms("WireGuard", "com.wireguard.macos")
        for entry in ("com.fastestvpn.vpn.macos.WireGuardExtension", "com.reckonmac.iProVPN.WireGuardExtension",
                      "com.anchorfree.hss-mac.wireguard"):
            self.assertFalse(engine._matches_app(entry, t), entry)

    def test_bundle_id_prefix_needs_dot_boundary(self):
        t = self.terms("Visual Studio Code", "com.microsoft.VSCode", executable="Electron")
        self.assertFalse(engine._matches_app("com.microsoft.VSCodeInsiders", t))
        self.assertFalse(engine._matches_app("electron", t))  # generic executable name ignored
        self.assertTrue(engine._matches_app("com.microsoft.VSCode.ShipIt", t))

    def test_last_two_bundle_parts_not_used(self):
        t = self.terms("Linguix for Safari", "com.linguix.SafariExtension")
        self.assertFalse(engine._matches_app("com.eltima.Folx3.FolxSafariExtension", t))


class DuplicatesTest(unittest.TestCase):
    def test_finds_identical_files_only(self):
        root = tempfile.mkdtemp()
        blob = os.urandom(2 * 1024 * 1024)
        for name in ("a.bin", "sub/b.bin"):
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(blob)
        with open(os.path.join(root, "c.bin"), "wb") as f:  # same size, different content
            f.write(blob[:-1] + b"x")
        os.link(os.path.join(root, "a.bin"), os.path.join(root, "hardlink.bin"))  # same file, not a copy
        groups = [g for g in engine.duplicates([root]) if g["type"] == "group"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(os.path.basename(p) for p in groups[0]["paths"]), ["a.bin", "b.bin"])


class UndoTest(unittest.TestCase):
    def test_trash_and_put_back(self):
        root = tempfile.mkdtemp(dir=os.path.expanduser("~/Library/Caches"))
        try:
            path = os.path.join(root, "nested", "burrow-undo-test.txt")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("hello")
            self.assertEqual(engine.trash_paths([path], label="unit test"), [])
            self.assertFalse(os.path.exists(path))
            batch = engine.last_trash_batch()
            if batch is None:
                self.skipTest("compiled trash helper not available (run build.py first)")
            restored, skipped = engine.undo_trash_batch(batch)
            self.assertEqual((len(restored), skipped), (1, []))
            with open(path) as f:
                self.assertEqual(f.read(), "hello")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class ReviewRegressionTest(unittest.TestCase):
    def test_changed_copy_is_not_trashed(self):
        root = tempfile.mkdtemp()
        keep, same, changed = (os.path.join(root, n) for n in ("keep", "same", "changed"))
        blob = os.urandom(300000)
        for path, data in ((keep, blob), (same, blob), (changed, blob[:-1] + b"!")):
            with open(path, "wb") as f:
                f.write(data)
        self.assertEqual(engine.verify_duplicates(keep, [same, changed, keep]), [same])
        self.assertEqual(engine.verify_duplicates(os.path.join(root, "gone"), [same]), [])

    def test_packages_are_never_opened(self):
        root = tempfile.mkdtemp()
        blob = os.urandom(2 * 1024 * 1024)
        for name in ("disk.sparsebundle/bands/1", "disk.sparsebundle/bands/2", "Thing.weird/Contents/a", "Thing.weird/Contents/b"):
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(blob)
        open(os.path.join(root, "Thing.weird", "Info.plist"), "w").close()
        self.assertEqual([g for g in engine.duplicates([root]) if g["type"] == "group"], [])

    def test_batches_group_by_id_and_undo_drops_them(self):
        cache = tempfile.mkdtemp()
        old = engine.CACHE_DIR
        engine.CACHE_DIR = cache
        try:
            engine.record_trash_batch("Earlier thing", {"/x/a": "/t/a"})
            bid = engine.new_batch_id()
            engine.record_trash_batch("Uninstall X", {"/x/app": "/t/app"}, bid)
            engine.record_trash_batch("Uninstall X", {"/x/pref": "/t/pref"}, bid)
            batches = engine.load_state(engine.TRASH_HISTORY)["batches"]
            self.assertEqual([b["label"] for b in batches], ["Earlier thing", "Uninstall X"])
            self.assertEqual(len(batches[-1]["items"]), 2)
            engine.undo_trash_batch(batches[-1])  # nothing exists, so nothing restores...
            left = engine.load_state(engine.TRASH_HISTORY)["batches"]
            self.assertEqual([b["label"] for b in left], ["Earlier thing"])  # ...but the batch is gone
        finally:
            engine.CACHE_DIR = old

    def test_unreachable_programs_are_not_missing(self):
        self.assertFalse(engine.program_is_gone("/Volumes/Unplugged Drive/Tool.app/Contents/MacOS/tool"))
        self.assertTrue(engine.program_is_gone(os.path.join(tempfile.mkdtemp(), "deleted-helper")))
        self.assertFalse(engine.program_is_gone("/bin/sh"))

    def test_symlink_is_trashed_as_a_link(self):
        root = tempfile.mkdtemp(dir=os.path.expanduser("~/Library/Caches"))
        try:
            target = os.path.join(root, "target.txt")
            link = os.path.join(root, "link")
            with open(target, "w") as f:
                f.write("keep me")
            os.symlink(target, link)
            self.assertEqual(engine.trash_paths([link], label="unit test link"), [])
            self.assertFalse(os.path.lexists(link))
            self.assertTrue(os.path.exists(target))
            batch = engine.last_trash_batch()  # put it back so the test leaves the Trash alone
            if batch and batch["label"] == "unit test link":
                engine.undo_trash_batch(batch)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class Round3RegressionTest(unittest.TestCase):
    def test_generic_app_names_never_claim_library_folders(self):
        for name in ("Developer", "Fonts", "Logs", "Preferences", "Audio", "Keychains"):
            ids = {"bundle_id": "com.example." + name, "bundle_name": name, "display_name": None, "executable": name}
            found = [r["path"] for r in engine.find_residual_files(name, ids)]
            for path in found:
                self.assertNotIn(os.path.dirname(path), engine.LIBRARY_ROOTS, (name, path))

    def test_running_detection_with_accented_path(self):
        import shutil
        import subprocess
        root = tempfile.mkdtemp(suffix="-café")
        app = os.path.join(root, "Übersicht.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        exe = os.path.join(app, "Contents", "MacOS", "spin")
        src = os.path.join(root, "spin.c")
        with open(src, "w") as f:
            f.write("#include <unistd.h>\nint main(){sleep(20);return 0;}\n")
        if subprocess.run(["clang", src, "-o", exe], stderr=subprocess.DEVNULL).returncode != 0:
            self.skipTest("no C compiler")
        proc = subprocess.Popen([exe])
        try:
            import time
            time.sleep(0.5)
            self.assertTrue(engine.app_is_running(app))
        finally:
            proc.kill()
            proc.wait()
            shutil.rmtree(root, ignore_errors=True)

    def test_dir_size_skips_cloud_folders(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "CloudStorage", "Drive"))
        with open(os.path.join(root, "CloudStorage", "Drive", "big"), "wb") as f:
            f.write(b"x" * 500000)
        self.assertLess(engine.dir_size(root), 100000)

    def test_app_package_heuristics(self):
        root = tempfile.mkdtemp()
        for name in ("Captures.libCapto", "My.photolibrary", "Stuff.mybundle"):
            self.assertTrue(engine.is_package_dir(os.path.join(root, name), name), name)
        self.assertFalse(engine.is_package_dir(os.path.join(root, "project.v2"), "project.v2"))


class UninstallerTest(unittest.TestCase):
    def test_system_extension_parsing(self):
        sample = (
            "4 extension(s)\n"
            "--- com.apple.system_extension.network_extension\n"
            "enabled\tactive\tteamID\tbundleID (version)\tname\t[state]\n"
            "*\t*\tMLZF7K7B5R\tat.obdev.littlesnitch.networkextension (6.5/7303)\tLittle Snitch Network Extension\t[activated enabled]\n"
            "*\t*\tTC3Q7MAJXF\tcom.adguard.mac.adguard.network-extension (2.19.0/2258)\tAdGuard Network Extension\t[activated enabled]\n"
        )
        real = engine.sh
        engine.sh = lambda args, timeout=10: sample if "systemextensionsctl" in args[0] else real(args, timeout)
        try:
            found = engine.system_extensions("at.obdev.littlesnitch", "MLZF7K7B5R")
            self.assertEqual([e["name"] for e in found], ["Little Snitch Network Extension"])
            self.assertEqual(engine.system_extensions("com.example.other", "ZZZZZZZZZZ"), [])
        finally:
            engine.sh = real

    def test_vendor_uninstaller_detection(self):
        root = tempfile.mkdtemp()
        app = os.path.join(root, "Cool Tool.app")
        os.makedirs(os.path.join(app, "Contents", "Resources", "Uninstall Cool Tool.app"))
        os.makedirs(os.path.join(root, "Some Other Uninstaller.app"))
        found = [os.path.basename(p) for p in engine.vendor_uninstallers(app, "Cool Tool")]
        self.assertIn("Uninstall Cool Tool.app", found)

    def test_reset_keeps_non_data(self):
        rows = [
            {"path": "/x/a", "location": "Preferences"}, {"path": "/x/b", "location": "Containers"},
            {"path": "/x/c", "location": "System Library/LaunchDaemons"}, {"path": "/x/d", "location": "Homebrew"},
            {"path": "/x/e", "location": "Installed by package"}, {"path": "/x/f", "location": "Group Containers"},
        ]
        self.assertEqual([r["path"] for r in rows if burrow.is_data(r)], ["/x/a", "/x/b", "/x/f"])

    def test_needs_root(self):
        self.assertTrue(engine.needs_root("/Library/LaunchDaemons/x.plist"))
        self.assertFalse(engine.needs_root(os.path.join(tempfile.mkdtemp(), "x")))


class UninstallSafetyTest(unittest.TestCase):
    def test_channel_and_sibling_apps_are_not_claimed(self):
        t = {"names": ["bravebrowser"], "bundle_ids": ["com.brave.Browser"], "others": ["com.brave.browser.origin"]}
        self.assertFalse(engine._matches_app("com.brave.Browser.origin", t))
        self.assertFalse(engine._matches_app("com.brave.Browser.origin.plist", t))
        self.assertTrue(engine._matches_app("com.brave.Browser.helper", t))
        t = {"names": [], "bundle_ids": ["com.google.Chrome"], "others": []}
        self.assertFalse(engine._matches_app("com.google.Chrome.canary", t))  # even when Canary isn't installed
        self.assertTrue(engine._matches_app("com.google.Chrome", t))

    def test_package_files_never_touch_shared_or_other_apps(self):
        app = "/Applications/Cool.app"
        fake = {
            "com.cool.pkg": ("/", [
                "Applications/Cool.app/Contents/Info.plist",
                "Applications/Other.app/Contents/PlugIns/x.plugin/Contents/MacOS/x",
                "Library/Frameworks/Python.framework/Versions/3/lib/foo.py",
                "usr/local/bin/cool",
                "Library/Audio/Plug-Ins/Components/Cool.component/Contents/MacOS/Cool",
                "Library/Audio/Plug-Ins/Components/Shared.component/Contents/MacOS/Shared",
            ]),
            "com.other.pkg": ("/", ["Library/Audio/Plug-Ins/Components/Shared.component"]),
        }
        real = (engine.all_package_files, engine.os.path.lexists, engine.list_apps)
        engine.all_package_files = lambda: fake
        engine.os.path.lexists = lambda p: True
        engine.list_apps = lambda: [{"path": app}]
        engine._PKG_OWNERS.clear()
        try:
            self.assertEqual(engine.app_packages(app), ["com.cool.pkg"])
            self.assertEqual(engine.package_files(app), ["/Library/Audio/Plug-Ins/Components/Cool.component"])
        finally:
            engine.all_package_files, engine.os.path.lexists, engine.list_apps = real
            engine._PKG_OWNERS.clear()

    def test_package_match_requires_the_same_copy(self):
        fake = {"com.cool.pkg": ("/", ["Applications/Cool.app/Contents/Info.plist"])}
        real = (engine.all_package_files, engine.list_apps)
        engine.all_package_files = lambda: fake
        engine.list_apps = lambda: [{"path": "/Applications/Cool.app"}, {"path": "/Users/x/Applications/Cool.app"}]
        engine._PKG_OWNERS.clear()
        try:
            self.assertEqual(engine.app_packages("/Users/x/Applications/Cool.app"), [])
        finally:
            engine.all_package_files, engine.list_apps = real
            engine._PKG_OWNERS.clear()


class StateTest(unittest.TestCase):
    def setUp(self):
        self.old = engine.CACHE_DIR
        engine.CACHE_DIR = tempfile.mkdtemp()

    def tearDown(self):
        engine.CACHE_DIR = self.old

    def test_concurrent_updates_are_not_lost(self):
        import subprocess
        script = ("import sys; sys.path.insert(0, {src!r}); import engine; engine.CACHE_DIR = {cache!r}\n"
                  "for _ in range(25):\n"
                  "    engine.update_state('count.json', lambda d: dict(d, n=d.get('n', 0) + 1))\n").format(
                      src=os.path.join(os.path.dirname(__file__), "..", "src"), cache=engine.CACHE_DIR)
        procs = [subprocess.Popen(["/usr/bin/python3", "-c", script]) for _ in range(4)]
        for p in procs:
            p.wait()
        self.assertEqual(engine.load_state("count.json")["n"], 100)

    def test_cached_serves_last_value_when_offline(self):
        engine.cached("net", 0, lambda: {"v": 1})
        def offline():
            raise OSError("no network")
        self.assertEqual(engine.cached("net", 0, offline), {"v": 1})

    def test_update_rollbacks_outlive_unrelated_history(self):
        engine.record_trash_batch("Update Foo to 2", {"/Applications/Foo.app": "/t/Foo.app"}, "u1")
        engine.update_state(engine.TRASH_HISTORY, lambda d: {"batches": [dict(b, items=[dict(i, replace=True) for i in b["items"]]) for b in d["batches"]]})
        for i in range(30):
            engine.record_trash_batch("clean {}".format(i), {"/x/{}".format(i): "/t/{}".format(i)})
        labels = [b["label"] for b in engine.load_state(engine.TRASH_HISTORY)["batches"]]
        self.assertIn("Update Foo to 2", labels)
        self.assertEqual(len(labels), engine.TRASH_HISTORY_KEEP + 1)

    def test_undo_keeps_what_could_not_go_back(self):
        root = tempfile.mkdtemp()
        src = os.path.join(root, "trashed")
        open(src, "w").close()
        engine.record_trash_batch("x", {os.path.join(root, "blocked", "f"): src, os.path.join(root, "gone"): os.path.join(root, "nothing")}, "b1")
        os.makedirs(os.path.join(root, "blocked"))
        os.chmod(os.path.join(root, "blocked"), 0o500)  # can't move back into it
        try:
            real = engine._move_back
            engine._move_back = lambda s, d: False
            engine.undo_trash_batch(engine.find_trash_batch("b1"))
        finally:
            engine._move_back = real
            os.chmod(os.path.join(root, "blocked"), 0o700)
        left = engine.find_trash_batch("b1")
        self.assertEqual([i["to"] for i in left["items"]], [src])  # kept to retry; the vanished one is dropped

    def test_housekeeping_removes_stale_files_only(self):
        import time
        jobs = os.path.join(engine.CACHE_DIR, "jobs")
        os.makedirs(jobs)
        old = time.time() - 10 * 86400
        for name in ("analyze-aaa.out", "analyze-aaa.done", "analyze-aaa.pid", "memo-x.json"):
            path = os.path.join(jobs if name.startswith("analyze") else engine.CACHE_DIR, name)
            open(path, "w").close()
            os.utime(path, (old, old))
        fresh = os.path.join(jobs, "clean.out")
        open(fresh, "w").close()
        engine.housekeeping()
        self.assertEqual(sorted(os.listdir(jobs)), ["clean.out"])
        self.assertFalse(os.path.exists(os.path.join(engine.CACHE_DIR, "memo-x.json")))


class SmallHelpersTest(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(engine.format_bytes(0), "0 B")
        self.assertEqual(engine.format_bytes(1000), "1000 B")
        self.assertEqual(engine.format_bytes(1536), "1.5 KB")
        self.assertEqual(engine.format_bytes(5 * 1024 ** 3), "5.0 GB")

    def test_cpu_usage(self):
        prev = [[100, 100, 800, 0], [0, 0, 1000, 0]]
        cur = [[150, 150, 900, 0], [0, 0, 1200, 0]]
        total, per_core = engine.cpu_usage(prev, cur)
        self.assertAlmostEqual(per_core[0], 50.0)
        self.assertAlmostEqual(per_core[1], 0.0)
        self.assertAlmostEqual(total, 25.0)

    def test_dir_size_counts_nested_files(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "a", "b"))
        with open(os.path.join(root, "a", "b", "f"), "wb") as f:
            f.write(b"x" * 100000)
        self.assertGreaterEqual(engine.dir_size(root), 100000)

    def test_large_query_filters(self):
        self.assertEqual(burrow.parse_large_query("2gb videos old holiday"), (2 * 1024 ** 3, "video", True, "holiday"))
        self.assertEqual(burrow.parse_large_query("report"), (None, None, False, "report"))

    def test_split_analyze_query(self):
        home = os.path.expanduser("~")
        self.assertEqual(burrow.split_analyze_query("~/"), (home, ""))
        self.assertEqual(burrow.split_analyze_query("~/nonexistent-zz"), (home, "nonexistent-zz"))
        self.assertIsNone(burrow.split_analyze_query("downloads"))

    def test_trash_skips_missing_paths(self):
        self.assertEqual(engine.trash_paths(["/nonexistent/burrow-test"]), [])


if __name__ == "__main__":
    unittest.main()
