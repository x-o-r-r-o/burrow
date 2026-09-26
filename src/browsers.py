#!/usr/bin/python3
"""Burrow browser cleaning and reset.

Finds every installed browser and its profiles by how browsers store data, so
browsers not listed anywhere here still work:
  Chromium family  a data folder with "Local State"  (Chrome, Brave, Edge, Opera, Vivaldi,
                   Arc, Dia, Comet, Helium, Sigma, Chromium, Yandex, Thorium…)
  Firefox family   a data folder with "profiles.ini" (Firefox, LibreWolf, Floorp, Zen,
                   Tor Browser, Waterfox, Mullvad…)
  WebKit           Safari and Orion (their own layouts)
A data folder only counts as a browser's when it matches an installed app that
handles web links, so Electron apps (which also have "Local State") are ignored.

Everything is reversible: files go to the Trash, and databases edited for a time
range are backed up to the Trash first (Undo restores the backup).
Written for the system /usr/bin/python3 (3.9).
"""

import configparser
import subprocess
import json
import os
import plistlib
import re
import shutil
import sqlite3
import time

import engine
from engine import HOME, _listdir, _normalize, dir_size

SUPPORT = os.path.join(HOME, "Library", "Application Support")
# Current macOS runs Safari from here; /Applications/Safari.app is a stub
SAFARI_CRYPTEX = "/System/Volumes/Preboot/Cryptexes/App/System/Applications/Safari.app"
CACHES = os.path.join(HOME, "Library", "Caches")

CATEGORIES = [
    # key, label, supports a time range, description
    ("cache", "Cache", False, "Temporary files for faster loading"),
    ("history", "Browsing history", True, "Sites you visited, suggestions from them, top sites"),
    ("downloads", "Download history", True, "The list of downloads (the files themselves stay)"),
    ("cookies", "Cookies and site data", True, "Signs you out of websites; site storage is cleared for all time"),
    ("sessions", "Open tabs and sessions", False, "Tabs and windows restored at launch"),
    ("forms", "Saved form data", True, "Text remembered in web forms (not addresses or cards)"),
    ("passwords", "Saved passwords", False, "Removes every saved password in the profile"),
]
CATEGORY_KEYS = [c[0] for c in CATEGORIES]
RANGES = [("hour", "Last hour", 3600), ("day", "Last 24 hours", 86400), ("week", "Last 7 days", 7 * 86400),
          ("month", "Last 4 weeks", 28 * 86400), ("all", "All time", None)]

# Known data folders (relative to Application Support), for browsers that aren't
# installed any more and to disambiguate. Everything else is discovered.
# Data folders several apps use at once (they all count when checking what's running)
SHARED_ROOTS = {"Firefox": "Firefox", "Google/Chrome": "Google Chrome", "Microsoft Edge": "Microsoft Edge"}

KNOWN_ROOTS = {
    "Google/Chrome": "Google Chrome", "Google/Chrome Beta": "Google Chrome Beta", "Google/Chrome Canary": "Google Chrome Canary",
    "BraveSoftware/Brave-Browser": "Brave Browser", "BraveSoftware/Brave-Browser-Beta": "Brave Browser Beta",
    "BraveSoftware/Brave-Browser-Nightly": "Brave Browser Nightly", "BraveSoftware/Brave-Origin": "Brave Origin",
    "Microsoft Edge": "Microsoft Edge", "Microsoft Edge Beta": "Microsoft Edge Beta", "Microsoft Edge Dev": "Microsoft Edge Dev",
    "Vivaldi": "Vivaldi", "com.operasoftware.Opera": "Opera", "com.operasoftware.OperaGX": "Opera GX",
    "com.operasoftware.OperaAir": "Opera Air", "Arc/User Data": "Arc", "Dia/User Data": "Dia", "Comet": "Comet",
    "Chromium": "Chromium", "Yandex/YandexBrowser": "Yandex", "Thorium": "Thorium", "net.imput.helium": "Helium",
    "Firefox": "Firefox", "librewolf": "LibreWolf", "LibreWolf": "LibreWolf", "Floorp": "Floorp", "zen": "Zen",
    "Waterfox": "Waterfox", "TorBrowser-Data/Browser": "Tor Browser", "Mullvad/MullvadBrowser": "Mullvad Browser",
}


class BrowserError(Exception):
    pass


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def browser_apps():
    """Installed apps that handle http/https links: {normalized name: app}."""
    found = {}
    dirs = engine.app_folders() + ["/System/Applications", "/System/Volumes/Preboot/Cryptexes/App/System/Applications"]
    for d in dirs:
        for n in _listdir(d):
            if not n.endswith(".app"):
                continue
            path = os.path.join(d, n)
            try:
                with open(engine.app_info_plist(path), "rb") as f:
                    info = plistlib.load(f)
            except Exception:  # noqa: BLE001
                continue
            schemes = set()
            for t in info.get("CFBundleURLTypes") or []:
                schemes.update(s.lower() for s in (t.get("CFBundleURLSchemes") or []) if isinstance(s, str))
            if {"http", "https"} & schemes:
                found.setdefault(_normalize(n[:-4]), {"name": n[:-4], "path": path, "bundle_id": info.get("CFBundleIdentifier")})
    safari = "/Applications/Safari.app"
    if os.path.exists(safari):
        found.setdefault("safari", {"name": "Safari", "path": safari, "bundle_id": "com.apple.Safari"})
    return found


def _match_app(rel_root, apps):
    """The installed browser app a data folder belongs to."""
    if rel_root in KNOWN_ROOTS:
        want = _normalize(KNOWN_ROOTS[rel_root])
        return apps.get(want)  # known folder: its own browser or nothing (leftover data)
    parts = [_normalize(p) for p in rel_root.split("/") if p and _normalize(p) not in ("userdata", "browser", "profiles")]
    joined = "".join(parts)
    best = None
    for key, app in apps.items():
        if len(key) < 3:
            continue
        if key == joined or key in parts or (len(key) >= 5 and key in joined) or any(p.startswith(key) and len(p) - len(key) <= 8 for p in parts):
            if best is None or len(key) > len(_normalize(best["name"])):
                best = app
    return best


def _find_roots(marker, max_depth=3):
    roots = []
    for base, dirs, files in os.walk(SUPPORT):
        depth = base[len(SUPPORT):].count(os.sep)
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("CloudStorage", "Mobile Documents", "MobileSync")]
        if marker in files:
            roots.append(os.path.relpath(base, SUPPORT))
            dirs[:] = []
    return roots


def chromium_profiles(root):
    """[(profile folder, display name)] from Local State (falls back to folder names)."""
    names = {}
    try:
        with open(os.path.join(root, "Local State")) as f:
            cache = (json.load(f).get("profile") or {}).get("info_cache") or {}
        names = {k: (v.get("name") or k) for k, v in cache.items()}
    except Exception:  # noqa: BLE001
        pass
    profiles = []
    for d in sorted(_listdir(root)):
        if (d == "Default" or d.startswith("Profile ")) and os.path.isdir(os.path.join(root, d)):
            profiles.append((d, names.get(d, d)))
    return profiles


def firefox_profiles(root):
    cp = configparser.ConfigParser()
    try:
        cp.read(os.path.join(root, "profiles.ini"))
    except configparser.Error:
        return []
    out = []
    for sec in cp.sections():
        if not sec.startswith("Profile") or "Path" not in cp[sec]:
            continue
        rel = cp[sec].get("IsRelative", "1") == "1"
        path = os.path.join(root, cp[sec]["Path"]) if rel else cp[sec]["Path"]
        if os.path.isdir(path):
            out.append((path, cp[sec].get("Name", os.path.basename(path))))
    return out


def discover():
    """Every browser with data on this Mac, cached for a minute (and refreshed when
    apps or browser data folders change)."""
    key = "{}-{}".format(int(_mtime("/Applications")), int(_mtime(SUPPORT)))
    memo = engine.load_state("memo-browsers.json")
    if memo.get("key") == key and time.time() - memo.get("time", 0) < 60:
        return memo["value"]
    value = _discover()
    engine.save_state("memo-browsers.json", {"key": key, "time": time.time(), "value": value})
    return value


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0


def _discover():
    apps = browser_apps()
    browsers = []
    for kind, marker in (("chromium", "Local State"), ("firefox", "profiles.ini")):
        for rel in _find_roots(marker):
            app = _match_app(rel, apps)
            if not app and rel not in KNOWN_ROOTS:
                continue  # an Electron app or something else that isn't a browser
            root = os.path.join(SUPPORT, rel)
            profiles = chromium_profiles(root) if kind == "chromium" else firefox_profiles(root)
            if not profiles:
                continue
            name = app["name"] if app else KNOWN_ROOTS[rel]
            sharing = [a["path"] for a in apps.values() if a is not app and rel in SHARED_ROOTS
                       and a["name"].startswith(SHARED_ROOTS[rel])]
            browsers.append({
                "also_apps": sharing,
                "id": "{}:{}".format(kind, rel), "kind": kind, "name": name, "root": root, "rel": rel,
                "app": app["path"] if app else None, "bundle_id": app.get("bundle_id") if app else None,
                "installed": bool(app), "profiles": [{"id": p, "name": n} for p, n in profiles],
            })
    if "safari" in apps:
        browsers.append({"id": "safari", "kind": "safari", "name": "Safari", "root": os.path.join(HOME, "Library", "Safari"),
                         "app": apps["safari"]["path"], "bundle_id": "com.apple.Safari", "installed": True,
                         "also_apps": [SAFARI_CRYPTEX],
                         "profiles": [{"id": "default", "name": "Safari"}], "access": safari_access()})
    orion = os.path.join(SUPPORT, "Orion")
    if os.path.isdir(orion):
        app = apps.get("orion") or apps.get("orionrc")
        browsers.append({"id": "orion", "kind": "orion", "name": "Orion", "root": orion, "app": app["path"] if app else None,
                         "bundle_id": app.get("bundle_id") if app else "com.kagi.kagimacOS", "installed": bool(app),
                         "profiles": [{"id": "default", "name": "Orion"}]})
    browsers.sort(key=lambda b: (not b["installed"], b["name"].lower()))
    return browsers


def safari_access():
    """Safari's data is protected: readable only with Full Disk Access."""
    try:
        os.listdir(os.path.join(HOME, "Library", "Safari"))
        return True
    except OSError:
        return False


def is_running(browser, executables=None):
    """Open right now, by app path (including Safari's system location and every app
    sharing the data folder, like Firefox Developer Edition). Only when there's no
    app path to check is macOS asked by bundle id (slower)."""
    paths = [p for p in [browser.get("app")] + browser.get("also_apps", []) if p]
    executables = executables if executables is not None else engine.running_executables()
    if paths:
        return any(engine.app_is_running(p, executables) for p in paths)
    ids = [browser.get("bundle_id")]
    for bid in [i for i in ids if i]:
        out = subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e", "return application id (item 1 of argv) is running", "-e", "end run", bid],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode().strip()
        if out == "true":
            return True
    return False


def bundle_id_of(app_path):
    return engine.read_plist_key(engine.app_info_plist(app_path), "CFBundleIdentifier")


# ---------------------------------------------------------------------------
# What each category means for each engine
# ---------------------------------------------------------------------------

# Site data folders. Extension storage lives elsewhere ("Local Extension Settings",
# "Extension State") or in per-origin entries named chrome-extension_*, which are kept.
# "Service Worker" is left alone: extensions (Manifest V3) register theirs there.
CHROMIUM_SITE_DATA = ["Local Storage", "Session Storage", "databases", "blob_storage", "Shared Dictionary", "SharedStorage"]
CHROMIUM_PER_ORIGIN = ["IndexedDB", "File System", "WebStorage"]


def _site_entries(folder, extension_prefixes):
    """Per-website entries of a storage folder, skipping browser extensions' own."""
    return [os.path.join(folder, n) for n in _listdir(folder) if not n.startswith(extension_prefixes) and not n.startswith(".")]
CHROMIUM_CACHE_IN_PROFILE = ["Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache", "ShaderCache"]


def _first_existing(paths):
    return next((p for p in paths if os.path.exists(p)), paths[0])


def _chromium_cache_root(browser, profile):
    # Chrome: Caches/Google/Chrome/Default; Arc/Dia keep "User Data" in the path
    return _first_existing([os.path.join(CACHES, browser["rel"], profile),
                            os.path.join(CACHES, browser["rel"].replace("/User Data", ""), profile)])


def _firefox_cache_root(browser, profile_path):
    rel = os.path.relpath(profile_path, SUPPORT)  # Firefox/Profiles/x, TorBrowser-Data/Browser/profile.default…
    return _first_existing([os.path.join(CACHES, rel),
                            os.path.join(CACHES, browser["rel"], "Profiles", os.path.basename(profile_path))])


def pick_profiles(browser, profile_ids):
    """The chosen profiles; no choice means all. A choice that no longer exists is an
    error, never a silent "all profiles"."""
    if not profile_ids:
        return browser["profiles"]
    chosen = [p for p in browser["profiles"] if p["id"] in profile_ids]
    if len(chosen) != len(set(profile_ids)):
        raise BrowserError("the selected profile no longer exists; choose it again")
    return chosen


def plan(browser, profile_ids, categories, range_key):
    """What cleaning would do: {"trash": [paths], "sql": [(db, [statements], label)], "notes": [..]}."""
    cutoff = dict((k, s) for k, _, s in RANGES).get(range_key)
    since = time.time() - cutoff if cutoff else None
    trash, sql, notes = [], [], []
    profiles = pick_profiles(browser, profile_ids)

    def exist(*paths):
        trash.extend(p for p in paths if os.path.lexists(p))

    for prof in profiles:
        if browser["kind"] == "chromium":
            pdir = os.path.join(browser["root"], prof["id"])
            cw = int((since + 11644473600) * 1e6) if since else 0  # Chromium: µs since 1601
            if "cache" in categories:
                croot = _chromium_cache_root(browser, prof["id"])
                exist(os.path.join(croot, "Cache"), os.path.join(croot, "Code Cache"),
                      *[os.path.join(pdir, d) for d in CHROMIUM_CACHE_IN_PROFILE])
            if "history" in categories:
                db = os.path.join(pdir, "History")
                if os.path.exists(db):
                    sql.append((db, [
                        "DELETE FROM visits WHERE visit_time >= {}".format(cw),
                        "DELETE FROM visit_source WHERE id NOT IN (SELECT id FROM visits)",
                        "DELETE FROM urls WHERE id NOT IN (SELECT url FROM visits)",
                        "DELETE FROM keyword_search_terms WHERE url_id NOT IN (SELECT id FROM urls)",
                        "DELETE FROM segment_usage WHERE time_slot >= {}".format(cw),
                        "DELETE FROM segments WHERE url_id NOT IN (SELECT id FROM urls)",
                        "DELETE FROM content_annotations WHERE visit_id NOT IN (SELECT id FROM visits)",
                        "DELETE FROM context_annotations WHERE visit_id NOT IN (SELECT id FROM visits)",
                    ], "history"))
                # Derived files are rebuilt from what's left
                exist(*[os.path.join(pdir, n) for n in ("Visited Links", "Top Sites", "Top Sites-journal", "Shortcuts",
                                                        "Shortcuts-journal", "Network Action Predictor", "Network Action Predictor-journal")])
            if "downloads" in categories:
                db = os.path.join(pdir, "History")
                if os.path.exists(db):
                    sql.append((db, [
                        "DELETE FROM downloads WHERE start_time >= {}".format(cw),
                        "DELETE FROM downloads_url_chains WHERE id NOT IN (SELECT id FROM downloads)",
                        "DELETE FROM downloads_slices WHERE download_id NOT IN (SELECT id FROM downloads)",
                    ], "downloads"))
            if "cookies" in categories:
                for db in (os.path.join(pdir, "Network", "Cookies"), os.path.join(pdir, "Cookies")):
                    if os.path.exists(db):
                        sql.append((db, ["DELETE FROM cookies WHERE creation_utc >= {}".format(cw)], "cookies"))
                exist(*[os.path.join(pdir, d) for d in CHROMIUM_SITE_DATA])
                for d in CHROMIUM_PER_ORIGIN:
                    exist(*_site_entries(os.path.join(pdir, d), ("chrome-extension",)))
                if since:
                    notes.append("Site storage (local storage, databases) can only be cleared for all time")
            if "sessions" in categories:
                exist(os.path.join(pdir, "Sessions"), *[os.path.join(pdir, n) for n in ("Current Session", "Current Tabs", "Last Session", "Last Tabs")])
            if "forms" in categories:
                db = os.path.join(pdir, "Web Data")
                if os.path.exists(db):
                    sql.append((db, ["DELETE FROM autofill WHERE date_created >= {}".format(int(since or 0))], "form data"))
            if "passwords" in categories:
                exist(*[os.path.join(pdir, n) for n in ("Login Data", "Login Data-journal", "Login Data For Account", "Login Data For Account-journal")])
                notes.append("If password sync is on, passwords come back from your account")
        elif browser["kind"] == "firefox":
            pdir = prof["id"]
            fw = int(since * 1e6) if since else 0  # Firefox: µs since 1970
            places = os.path.join(pdir, "places.sqlite")
            if "cache" in categories:
                croot = _firefox_cache_root(browser, pdir)
                exist(os.path.join(croot, "cache2"), os.path.join(croot, "startupCache"), os.path.join(croot, "thumbnails"),
                      os.path.join(pdir, "cache2"), os.path.join(pdir, "startupCache"))
            if ("history" in categories or "downloads" in categories) and os.path.exists(places):
                kinds = []
                if "history" in categories and "downloads" in categories:
                    kinds.append("DELETE FROM moz_historyvisits WHERE visit_date >= {}".format(fw))
                elif "history" in categories:
                    kinds.append("DELETE FROM moz_historyvisits WHERE visit_date >= {} AND visit_type != 7".format(fw))
                else:
                    kinds.append("DELETE FROM moz_historyvisits WHERE visit_date >= {} AND visit_type = 7".format(fw))
                sql.append((places, kinds + [
                    # Pages with no visits left go, unless bookmarked (bookmarks live in the same file)
                    "DELETE FROM moz_places WHERE id NOT IN (SELECT place_id FROM moz_historyvisits) AND foreign_count = 0",
                    "DELETE FROM moz_inputhistory WHERE place_id NOT IN (SELECT id FROM moz_places)",
                    "DELETE FROM moz_annos WHERE place_id NOT IN (SELECT id FROM moz_places)",
                    "DELETE FROM moz_origins WHERE id NOT IN (SELECT origin_id FROM moz_places)",
                ], "history"))
            if "cookies" in categories:
                db = os.path.join(pdir, "cookies.sqlite")
                if os.path.exists(db):
                    sql.append((db, ["DELETE FROM moz_cookies WHERE creationTime >= {}".format(fw)], "cookies"))
                exist(*_site_entries(os.path.join(pdir, "storage", "default"), ("moz-extension",)))
                exist(*_site_entries(os.path.join(pdir, "storage", "temporary"), ("moz-extension",)))
                exist(os.path.join(pdir, "webappsstore.sqlite"))
                if since:
                    notes.append("Site storage can only be cleared for all time")
            if "sessions" in categories:
                exist(os.path.join(pdir, "sessionstore.jsonlz4"), os.path.join(pdir, "sessionstore-backups"))
            if "forms" in categories:
                db = os.path.join(pdir, "formhistory.sqlite")
                if os.path.exists(db):
                    sql.append((db, ["DELETE FROM moz_formhistory WHERE firstUsed >= {}".format(fw)], "form data"))
            if "passwords" in categories:
                exist(os.path.join(pdir, "logins.json"), os.path.join(pdir, "logins-backup.json"))
                notes.append("If you use Firefox Sync, passwords come back from your account")
        elif browser["kind"] == "safari":
            lib = os.path.join(HOME, "Library")
            box = os.path.join(lib, "Containers", "com.apple.Safari", "Data", "Library")
            sw = (since - 978307200) if since else 0  # Safari: seconds since 2001
            if not browser.get("access"):
                raise BrowserError("Safari's data is protected. Give Alfred Full Disk Access in System Settings → Privacy & Security")
            if "cache" in categories:
                exist(os.path.join(lib, "Caches", "com.apple.Safari"), os.path.join(box, "Caches", "com.apple.Safari"))
            if "history" in categories:
                db = os.path.join(lib, "Safari", "History.db")
                if os.path.exists(db):
                    sql.append((db, [
                        "DELETE FROM history_visits WHERE visit_time >= {}".format(sw),
                        "DELETE FROM history_items WHERE id NOT IN (SELECT history_item FROM history_visits)",
                    ], "history"))
                notes.append("If Safari syncs with iCloud, your other devices may bring history back")
            if "downloads" in categories:
                exist(os.path.join(lib, "Safari", "Downloads.plist"))
            if "cookies" in categories:
                exist(os.path.join(box, "Cookies", "Cookies.binarycookies"), os.path.join(lib, "Safari", "LocalStorage"),
                      os.path.join(lib, "Safari", "Databases"), os.path.join(box, "WebKit", "WebsiteData"))
                if since:
                    notes.append("Safari's cookies and site data can only be cleared for all time")
            if "sessions" in categories:
                exist(os.path.join(lib, "Safari", "LastSession.plist"), os.path.join(box, "Safari", "LastSession.plist"))
            if "forms" in categories or "passwords" in categories:
                notes.append("Safari keeps form data and passwords in your Keychain; manage them in the Passwords app")
        elif browser["kind"] == "orion":
            obid = browser.get("bundle_id") or "com.kagi.kagimacOS"
            if "cache" in categories:
                exist(os.path.join(CACHES, obid))
            if "history" in categories:
                h = os.path.join(browser["root"], "Defaults", "history")
                exist(h, h + "-wal", h + "-shm")
                if since:
                    notes.append("Orion's history can only be cleared for all time")
            if "cookies" in categories:
                exist(os.path.join(HOME, "Library", "HTTPStorages", obid), os.path.join(HOME, "Library", "WebKit", obid))
            if {"downloads", "sessions", "forms", "passwords"} & set(categories):
                notes.append("Orion supports cache, history and cookies here; use Orion's settings for the rest")
    return {"trash": sorted(set(trash)), "sql": sql, "notes": sorted(set(notes))}


def reset_plan(browser, profile_ids, full):
    """Reset settings (keeps bookmarks, history, passwords) or a full reset (whole profile)."""
    trash = []
    profiles = pick_profiles(browser, profile_ids)
    for prof in profiles:
        if browser["kind"] == "chromium":
            pdir = os.path.join(browser["root"], prof["id"])
            if full:
                trash += [pdir, _chromium_cache_root(browser, prof["id"])]
            else:
                trash += [os.path.join(pdir, n) for n in ("Preferences", "Secure Preferences", "Extensions", "Extension State",
                                                           "Local Extension Settings", "Extension Rules", "Extension Scripts")]
        elif browser["kind"] == "firefox":
            if full:
                trash += [prof["id"], _firefox_cache_root(browser, prof["id"])]
            else:
                trash += [os.path.join(prof["id"], n) for n in ("prefs.js", "xulstore.json", "extension-preferences.json")]
        elif browser["kind"] == "orion" and full:
            trash += [browser["root"], os.path.join(CACHES, browser.get("bundle_id") or "com.kagi.kagimacOS")]
        else:
            raise BrowserError("{} can't be reset from Burrow; use its own settings".format(browser["name"]))
    return [p for p in trash if os.path.lexists(p)]


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------


def _backup_to_trash(db, label, batch):
    """Put a copy of a database in the Trash first, so Undo can restore it."""
    copy = "{}.burrow-backup-{}".format(db, int(time.time()))
    try:
        src = sqlite3.connect(db, timeout=10)
        dst = sqlite3.connect(copy)
        src.backup(dst)  # consistent copy, including what's still in the -wal file
        dst.close()
        src.close()
    except sqlite3.Error as e:
        if os.path.exists(copy):
            os.remove(copy)
        raise BrowserError("couldn't back up {} first ({}), so it wasn't changed".format(os.path.basename(db), e))
    moved = {}
    engine.trash_paths([copy], finder_fallback=False, moved_out=moved)
    if copy not in moved:
        if os.path.exists(copy):
            os.remove(copy)
        raise BrowserError("couldn't back up {} first, so it wasn't changed".format(os.path.basename(db)))
    engine.record_trash_batch(label, {db: moved[copy]}, batch)
    _mark_replace(batch, db)


def _mark_replace(batch_id, path):
    def change(d):
        for b in d.get("batches", []):
            if b.get("id") == batch_id:
                for it in b["items"]:
                    if it["from"] == path:
                        it["replace"] = True
        return d
    engine.update_state(engine.TRASH_HISTORY, change)


def run_sql(db, statements):
    con = sqlite3.connect(db, timeout=10)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        for st in statements:
            try:
                con.execute(st)
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e) and "no such column" not in str(e):
                    raise
        con.commit()
        try:
            con.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
    finally:
        con.close()


def clean(browser, profile_ids, categories, range_key, label=None, batch_id=None):
    """Clean the chosen categories. The browser must not be running. Pass the same
    batch_id to make cleaning several browsers one Undo step."""
    if is_running(browser):
        raise BrowserError("{} is open".format(browser["name"]))
    p = plan(browser, profile_ids, categories, range_key)
    label = label or "Clean {} ({})".format(browser["name"], dict((k, l) for k, l, _ in RANGES)[range_key].lower())
    batch = batch_id or engine.new_batch_id()
    sizes_before = {x: dir_size(x) for x in p["trash"]}
    failed = engine.trash_paths(p["trash"], finder_fallback=False, label=label, batch_id=batch)
    size = sum(v for k, v in sizes_before.items() if k not in failed)
    backed = set()
    for db, statements, _ in p["sql"]:
        if not os.path.exists(db):
            continue
        if db not in backed:
            _backup_to_trash(db, label, batch)
            backed.add(db)
        run_sql(db, statements)
    return {"freed": size, "failed": failed, "notes": p["notes"], "files": len(p["trash"]), "databases": len(backed)}


def reset(browser, profile_ids, full):
    if is_running(browser):
        raise BrowserError("{} is open".format(browser["name"]))
    paths = reset_plan(browser, profile_ids, full)
    label = "{} {}".format("Fully reset" if full else "Reset settings of", browser["name"])
    failed = engine.trash_paths(paths, finder_fallback=False, label=label, batch_id=engine.new_batch_id())
    return {"failed": failed, "items": len(paths)}


def sizes(browser):
    """Cache size for the list (cached for 5 minutes)."""
    return engine.cached("browser-size-" + _normalize(browser["id"]), 300, lambda: _sizes(browser))


def _sizes(browser):
    cache = 0
    for prof in browser["profiles"]:
        if browser["kind"] == "chromium":
            cache += dir_size(_chromium_cache_root(browser, prof["id"]))
        elif browser["kind"] == "firefox":
            cache += dir_size(_firefox_cache_root(browser, prof["id"]))
    if browser["kind"] == "orion":
        cache = dir_size(os.path.join(CACHES, browser.get("bundle_id") or "com.kagi.kagimacOS"))
    return {"cache": cache}


def cli(argv):
    """JSON commands for Burrow's window."""
    def say(obj):
        print(json.dumps(obj), flush=True)
    cmd = argv[0] if argv else ""
    if cmd == "state":
        items = []
        for b in discover():
            items.append(dict(b, running=is_running(b), cache=sizes(b)["cache"]))
        say({"browsers": items, "categories": [{"key": k, "label": l, "ranged": r, "detail": d} for k, l, r, d in CATEGORIES],
             "ranges": [{"key": k, "label": l} for k, l, _ in RANGES],
             "choices": engine.load_state("browser-choices.json")})
    elif cmd == "choose":
        # cli choose <browser id> <json {"categories": [...], "range": "...", "profile": "..."}>  (shared with Alfred)
        change = json.loads(argv[2])
        engine.update_state("browser-choices.json", lambda d: dict(d, **{argv[1]: dict(d.get(argv[1]) or {}, **change)}))
        say({"ok": True})
    elif cmd in ("plan", "clean", "reset"):
        # cli clean <browser id> <cat,cat> <range> [profile,profile]
        b = next((x for x in discover() if x["id"] == argv[1]), None)
        if not b:
            say({"error": "browser not found"})
            return
        profiles = json.loads(argv[4]) if len(argv) > 4 and argv[4].startswith("[") else ([argv[4]] if len(argv) > 4 and argv[4] else [])
        try:
            if cmd == "plan":
                p = plan(b, profiles, argv[2].split(","), argv[3])
                say({"files": len(p["trash"]), "databases": len(set(x[0] for x in p["sql"])), "notes": p["notes"]})
            elif cmd == "clean":
                if is_running(b) and not engine.quit_app(b["app"], b.get("bundle_id"), wait=15):
                    say({"error": "{} didn't quit".format(b["name"])})
                    return
                r = clean(b, profiles, [c for c in argv[2].split(",") if c], argv[3])
                say({"done": "{} cleaned".format(b["name"]), "freed": r["freed"], "failed": len(r["failed"]), "notes": r["notes"]})
            else:
                if is_running(b) and not engine.quit_app(b["app"], b.get("bundle_id"), wait=15):
                    say({"error": "{} didn't quit".format(b["name"])})
                    return
                r = reset(b, profiles, argv[2] == "full")
                say({"done": "{} reset".format(b["name"]), "failed": len(r["failed"])})
        except Exception as e:  # noqa: BLE001
            say({"error": str(e)})
    else:
        say({"error": "unknown command"})


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        cli(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(discover(), indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == "plan":
        b = next(x for x in discover() if x["id"] == sys.argv[2])
        cats = sys.argv[3].split(",") if len(sys.argv) > 3 else CATEGORY_KEYS
        print(json.dumps(plan(b, [], cats, sys.argv[4] if len(sys.argv) > 4 else "all"), indent=2))
    else:
        print(__doc__)
