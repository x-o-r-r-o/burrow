"""Browser cleaning, end to end on throwaway Chromium and Firefox profiles.
Real files go through the Trash and are put back / deleted afterwards.
Run: /usr/bin/python3 -m unittest discover tests   (build first: python3 build.py)"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("alfred_workflow_cache", tempfile.mkdtemp())

import browsers  # noqa: E402
import engine  # noqa: E402

NOW = time.time()
OLD = NOW - 30 * 86400
CHROME_T = lambda t: int((t + 11644473600) * 1e6)  # noqa: E731
FF_T = lambda t: int(t * 1e6)  # noqa: E731


def make_chromium(profile):
    os.makedirs(profile)
    con = sqlite3.connect(os.path.join(profile, "History"))
    con.executescript("""
        CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);
        CREATE TABLE keyword_search_terms(url_id INTEGER, term TEXT);
        CREATE TABLE downloads(id INTEGER PRIMARY KEY, start_time INTEGER, target_path TEXT);
        CREATE TABLE downloads_url_chains(id INTEGER, url TEXT);
    """)
    con.execute("INSERT INTO urls VALUES (1, 'https://old.example'), (2, 'https://new.example')")
    con.execute("INSERT INTO visits VALUES (1, 1, ?), (2, 2, ?)", (CHROME_T(OLD), CHROME_T(NOW - 60)))
    con.execute("INSERT INTO keyword_search_terms VALUES (2, 'recent search')")
    con.execute("INSERT INTO downloads VALUES (1, ?, 'old.zip'), (2, ?, 'new.zip')", (CHROME_T(OLD), CHROME_T(NOW - 60)))
    con.commit()
    con.close()
    os.makedirs(os.path.join(profile, "Network"))
    con = sqlite3.connect(os.path.join(profile, "Network", "Cookies"))
    con.execute("CREATE TABLE cookies(host_key TEXT, creation_utc INTEGER)")
    con.execute("INSERT INTO cookies VALUES ('old', ?), ('new', ?)", (CHROME_T(OLD), CHROME_T(NOW - 60)))
    con.commit()
    con.close()
    for d in ("Local Storage", "Code Cache", "Sessions"):
        os.makedirs(os.path.join(profile, d))
        open(os.path.join(profile, d, "x"), "w").close()
    for f in ("Login Data", "Preferences", "Bookmarks", "Visited Links"):
        open(os.path.join(profile, f), "w").close()


def make_firefox(profile):
    os.makedirs(profile)
    con = sqlite3.connect(os.path.join(profile, "places.sqlite"))
    con.executescript("""
        CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, foreign_count INTEGER DEFAULT 0, origin_id INTEGER);
        CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER, visit_type INTEGER);
        CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, fk INTEGER);
        CREATE TABLE moz_inputhistory(place_id INTEGER);
        CREATE TABLE moz_annos(place_id INTEGER);
        CREATE TABLE moz_origins(id INTEGER PRIMARY KEY);
    """)
    # 1: old visit, 2: new visit, 3: new visit but bookmarked, 4: new download
    con.execute("INSERT INTO moz_places VALUES (1,'old',0,1),(2,'new',0,1),(3,'bookmarked',1,1),(4,'dl',0,1)")
    con.execute("INSERT INTO moz_historyvisits VALUES (1,1,?,1),(2,2,?,1),(3,3,?,1),(4,4,?,7)",
                (FF_T(OLD), FF_T(NOW - 60), FF_T(NOW - 60), FF_T(NOW - 60)))
    con.execute("INSERT INTO moz_bookmarks VALUES (1, 3)")
    con.execute("INSERT INTO moz_origins VALUES (1)")
    con.commit()
    con.close()
    open(os.path.join(profile, "logins.json"), "w").close()
    open(os.path.join(profile, "prefs.js"), "w").close()


def rows(db, sql):
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute(sql)]
    finally:
        con.close()


class BrowserCleanTest(unittest.TestCase):
    def setUp(self):
        engine.CACHE_DIR = tempfile.mkdtemp()
        self.base = tempfile.mkdtemp(prefix="burrow-browsers-", dir=os.path.expanduser("~/Library/Caches"))
        self.old = (browsers.SUPPORT, browsers.CACHES)
        browsers.SUPPORT = os.path.join(self.base, "Support")
        browsers.CACHES = os.path.join(self.base, "Caches")
        self.chrome = {"id": "chromium:Test/Chrome", "kind": "chromium", "name": "Test Chrome", "rel": "Test/Chrome",
                       "root": os.path.join(browsers.SUPPORT, "Test", "Chrome"), "app": None, "installed": True,
                       "profiles": [{"id": "Default", "name": "Me"}]}
        self.firefox = {"id": "firefox:TestFox", "kind": "firefox", "name": "Test Fox", "rel": "TestFox",
                        "root": os.path.join(browsers.SUPPORT, "TestFox"), "app": None, "installed": True,
                        "profiles": [{"id": os.path.join(browsers.SUPPORT, "TestFox", "Profiles", "p1"), "name": "default"}]}
        make_chromium(os.path.join(self.chrome["root"], "Default"))
        os.makedirs(os.path.join(browsers.CACHES, "Test", "Chrome", "Default", "Cache"))
        make_firefox(self.firefox["profiles"][0]["id"])

    def tearDown(self):
        # Everything these tests moved to the Trash is throwaway test data: delete it.
        for batch in engine.load_state(engine.TRASH_HISTORY).get("batches", []):
            for it in batch["items"]:
                if os.path.lexists(it["to"]) and os.path.dirname(it["to"]) == os.path.expanduser("~/.Trash"):
                    shutil.rmtree(it["to"], ignore_errors=True) if os.path.isdir(it["to"]) else os.remove(it["to"])
        browsers.SUPPORT, browsers.CACHES = self.old
        shutil.rmtree(self.base, ignore_errors=True)

    def test_chromium_last_hour_keeps_older_history(self):
        pdir = os.path.join(self.chrome["root"], "Default")
        browsers.clean(self.chrome, [], ["history", "downloads", "cookies"], "hour")
        hist = os.path.join(pdir, "History")
        self.assertEqual(rows(hist, "SELECT url FROM urls"), ["https://old.example"])
        self.assertEqual(rows(hist, "SELECT term FROM keyword_search_terms"), [])
        self.assertEqual(rows(hist, "SELECT target_path FROM downloads"), ["old.zip"])
        self.assertEqual(rows(os.path.join(pdir, "Network", "Cookies"), "SELECT host_key FROM cookies"), ["old"])
        self.assertTrue(os.path.exists(os.path.join(pdir, "Bookmarks")))
        self.assertTrue(os.path.exists(os.path.join(pdir, "Login Data")))

    def test_chromium_undo_restores_database(self):
        hist = os.path.join(self.chrome["root"], "Default", "History")
        browsers.clean(self.chrome, [], ["history"], "all")
        self.assertEqual(rows(hist, "SELECT count(*) FROM urls"), [0])
        moved_aside = {}
        real = engine.trash_paths
        engine.trash_paths = lambda paths, **k: real(paths, **dict(k, moved_out=moved_aside))
        try:
            restored, skipped = engine.undo_trash_batch(engine.last_trash_batch())
        finally:
            engine.trash_paths = real
        self.assertEqual(rows(hist, "SELECT count(*) FROM urls"), [2])
        for path in moved_aside.values():  # Undo put the cleaned copy in the Trash: remove it
            if os.path.isfile(path):
                os.remove(path)

    def test_chromium_cache_passwords_and_reset(self):
        pdir = os.path.join(self.chrome["root"], "Default")
        browsers.clean(self.chrome, [], ["cache", "passwords"], "all")
        self.assertFalse(os.path.exists(os.path.join(pdir, "Code Cache")))
        self.assertFalse(os.path.exists(os.path.join(browsers.CACHES, "Test", "Chrome", "Default", "Cache")))
        self.assertFalse(os.path.exists(os.path.join(pdir, "Login Data")))
        browsers.reset(self.chrome, [], full=False)
        self.assertFalse(os.path.exists(os.path.join(pdir, "Preferences")))
        self.assertTrue(os.path.exists(os.path.join(pdir, "Bookmarks")))
        browsers.reset(self.chrome, [], full=True)
        self.assertFalse(os.path.exists(pdir))

    def test_firefox_history_keeps_bookmarks_and_downloads(self):
        places = os.path.join(self.firefox["profiles"][0]["id"], "places.sqlite")
        browsers.clean(self.firefox, [], ["history"], "hour")
        self.assertEqual(sorted(rows(places, "SELECT url FROM moz_places")), ["bookmarked", "dl", "old"])
        browsers.clean(self.firefox, [], ["downloads"], "all")
        self.assertEqual(sorted(rows(places, "SELECT url FROM moz_places")), ["bookmarked", "old"])
        self.assertEqual(rows(places, "SELECT fk FROM moz_bookmarks"), [3])

    def test_firefox_passwords_and_reset(self):
        pdir = self.firefox["profiles"][0]["id"]
        browsers.clean(self.firefox, [], ["passwords"], "all")
        self.assertFalse(os.path.exists(os.path.join(pdir, "logins.json")))
        browsers.reset(self.firefox, [], full=False)
        self.assertFalse(os.path.exists(os.path.join(pdir, "prefs.js")))
        self.assertTrue(os.path.exists(os.path.join(pdir, "places.sqlite")))


class BrowserSafetyTest(BrowserCleanTest):
    def test_extension_storage_survives_cookie_cleaning(self):
        pdir = os.path.join(self.chrome["root"], "Default")
        for name in ("https_example.com_0.indexeddb.leveldb", "chrome-extension_abcdef_0.indexeddb.leveldb"):
            os.makedirs(os.path.join(pdir, "IndexedDB", name))
        ff = self.firefox["profiles"][0]["id"]
        for name in ("https+++example.com", "moz-extension+++1234-uuid"):
            os.makedirs(os.path.join(ff, "storage", "default", name))
        browsers.clean(self.chrome, [], ["cookies"], "all")
        browsers.clean(self.firefox, [], ["cookies"], "all")
        self.assertEqual(os.listdir(os.path.join(pdir, "IndexedDB")), ["chrome-extension_abcdef_0.indexeddb.leveldb"])
        self.assertEqual(os.listdir(os.path.join(ff, "storage", "default")), ["moz-extension+++1234-uuid"])

    def test_stale_profile_choice_is_an_error_not_all_profiles(self):
        with self.assertRaises(browsers.BrowserError):
            browsers.reset(self.chrome, ["Profile 9"], full=True)
        self.assertTrue(os.path.exists(os.path.join(self.chrome["root"], "Default")))

    def test_reset_settings_moves_extensions_aside_with_preferences(self):
        pdir = os.path.join(self.chrome["root"], "Default")
        os.makedirs(os.path.join(pdir, "Extensions", "abc"))
        browsers.reset(self.chrome, [], full=False)
        self.assertFalse(os.path.exists(os.path.join(pdir, "Extensions")))
        self.assertTrue(os.path.exists(os.path.join(pdir, "Bookmarks")))

    def test_no_backup_means_no_edit(self):
        hist = os.path.join(self.chrome["root"], "Default", "History")
        real = engine.trash_paths
        engine.trash_paths = lambda paths, **k: list(paths)  # the backup can't be moved to the Trash
        try:
            with self.assertRaises(browsers.BrowserError):
                browsers.clean(self.chrome, [], ["history"], "all")
        finally:
            engine.trash_paths = real
        self.assertEqual(rows(hist, "SELECT count(*) FROM urls"), [2])


class DiscoveryTest(unittest.TestCase):
    def test_leftover_beta_data_is_not_claimed_by_stable(self):
        apps = {"googlechrome": {"name": "Google Chrome", "path": "/A/Google Chrome.app"}}
        self.assertIsNone(browsers._match_app("Google/Chrome Beta", apps))
        self.assertEqual(browsers._match_app("Google/Chrome", apps)["name"], "Google Chrome")

    def test_electron_apps_are_not_browsers(self):
        apps = {"braveorigin": {"name": "Brave Origin", "path": "/A/Brave Origin.app"}, "helium": {"name": "Helium", "path": "/A/Helium.app"}}
        self.assertEqual(browsers._match_app("BraveSoftware/Brave-Origin", apps)["name"], "Brave Origin")
        self.assertEqual(browsers._match_app("net.imput.helium", apps)["name"], "Helium")
        self.assertIsNone(browsers._match_app("Claude", apps))
        self.assertIsNone(browsers._match_app("obsidian", apps))


if __name__ == "__main__":
    unittest.main()
