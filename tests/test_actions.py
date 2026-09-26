"""End-to-end tests of Burrow's actions (uninstall, reset, clean, duplicates, undo).

These really move files to the Trash and put them back, using a throwaway app in
~/Applications and throwaway files in ~/Library. Only the confirmation dialogs,
notifications and Alfred re-opening are stubbed. Everything is removed afterwards.

Run: /usr/bin/python3 -m unittest discover tests   (build first: python3 build.py)
"""

import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ["alfred_workflow_cache"] = tempfile.mkdtemp(prefix="burrow-actions-")

import burrow  # noqa: E402
import engine  # noqa: E402

HELPER = os.path.join(ROOT, "build", "bin", "BurrowTrash")
HOME = os.path.expanduser("~")
BID = "io.burrowtest.demo"
NAME = "BurrowTestApp"


def stub_ui(answer=True):
    burrow.confirm = lambda *a, **k: answer
    burrow.notify = lambda *a, **k: None
    burrow.alfred_search = lambda *a, **k: None


@unittest.skipUnless(os.access(HELPER, os.X_OK), "build the workflow first (python3 build.py)")
class ActionTest(unittest.TestCase):
    def setUp(self):
        engine.TRASH_HELPER = HELPER
        engine.CACHE_DIR = burrow.CACHE_DIR = tempfile.mkdtemp(prefix="burrow-actions-")
        stub_ui()
        self.created = []

    def tearDown(self):
        # Whatever a failed assertion left behind: put it back from the Trash, then delete.
        for batch in engine.load_state(engine.TRASH_HISTORY).get("batches", []):
            for it in batch["items"]:
                if os.path.lexists(it["to"]):
                    try:
                        os.rename(it["to"], it["from"])
                    except OSError:
                        pass
        for path in self.created:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.lexists(path):
                os.remove(path)

    def make(self, path, content=b"x" * 4096):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def make_app(self):
        app = os.path.join(HOME, "Applications", NAME + ".app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"), exist_ok=True)
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": BID, "CFBundleName": NAME, "CFBundleExecutable": NAME}, f)
        self.make(os.path.join(app, "Contents", "MacOS", NAME))
        self.created.append(app)
        lib = os.path.join(HOME, "Library")
        leftovers = [
            self.make(os.path.join(lib, "Preferences", BID + ".plist")),
            os.path.dirname(self.make(os.path.join(lib, "Caches", BID, "cache.db"))),
            os.path.dirname(self.make(os.path.join(lib, "Application Support", NAME, "data.json"))),
            os.path.dirname(self.make(os.path.join(lib, "Saved Application State", BID + ".savedState", "windows.plist"))),
            os.path.dirname(self.make(os.path.join(lib, "HTTPStorages", BID, "cookies"))),
        ]
        self.created += leftovers
        # installed_apps_by_bundle_id is cached; make sure the new app is seen
        for f in os.listdir(engine.CACHE_DIR):
            if f.startswith("memo-"):
                os.remove(os.path.join(engine.CACHE_DIR, f))
        return app, leftovers

    def test_complete_uninstall_then_undo(self):
        app, leftovers = self.make_app()
        found = {r["path"] for r in engine.find_residual_files(NAME, engine.app_identifiers(app), app)}
        self.assertTrue(set(leftovers) <= found, set(leftovers) - found)

        msg = burrow.uninstall(app, NAME, 0)
        self.assertIn("uninstalled", msg)
        for p in [app] + leftovers:
            self.assertFalse(os.path.lexists(p), p)

        batch = engine.last_trash_batch()
        self.assertEqual(batch["label"], "Uninstall " + NAME)
        self.assertEqual(len(batch["items"]), 1 + len(leftovers))  # one undo step for everything
        restored, skipped = engine.undo_trash_batch(batch)
        self.assertEqual(skipped, [])
        for p in [app] + leftovers:
            self.assertTrue(os.path.lexists(p), p)

    def test_excluded_leftover_stays(self):
        app, leftovers = self.make_app()
        keep = leftovers[0]
        burrow.uninstall(app, NAME, 0, excluded={keep}, reviewed=True)
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(app))

    def test_reset_keeps_the_app(self):
        app, leftovers = self.make_app()
        msg = burrow.uninstall(app, NAME, 0, reviewed=True, reset=True)
        self.assertIn("reset", msg)
        self.assertTrue(os.path.exists(app))
        for p in leftovers:
            self.assertFalse(os.path.lexists(p), p)
        engine.undo_trash_batch(engine.last_trash_batch())
        for p in leftovers:
            self.assertTrue(os.path.lexists(p), p)

    def test_cancelled_uninstall_changes_nothing(self):
        app, leftovers = self.make_app()
        stub_ui(answer=False)
        self.assertIsNone(burrow.uninstall(app, NAME, 0))
        for p in [app] + leftovers:
            self.assertTrue(os.path.lexists(p), p)

    def test_clean_one_item_and_all(self):
        base = tempfile.mkdtemp(prefix="burrow-clean-", dir=os.path.join(HOME, "Library", "Caches"))
        self.created.append(base)
        a = os.path.dirname(self.make(os.path.join(base, "A", "f")))
        b = os.path.dirname(self.make(os.path.join(base, "B", "f")))
        opt = os.path.dirname(self.make(os.path.join(base, "Optional", "f")))
        job = burrow.job_for("clean")
        records = [
            {"type": "item", "section": "Test", "description": "A cache", "paths": [a], "size": 4096},
            {"type": "item", "section": "Test", "description": "B cache", "paths": [b], "size": 4096},
            {"type": "item", "section": "Optional", "description": "Opt", "paths": [opt], "size": 4096, "optional": True},
        ]
        with open(job.out, "w") as f:
            f.write("\n".join(json.dumps(r) for r in records) + "\n")
        with open(job.done, "w") as f:
            f.write("0")

        burrow.clean({"keys": [a]})
        self.assertFalse(os.path.exists(a))
        self.assertTrue(os.path.exists(b))

        with open(job.out, "w") as f:  # clean() clears the job; restore the scan
            f.write("\n".join(json.dumps(r) for r in records[1:]) + "\n")
        with open(job.done, "w") as f:
            f.write("0")
        burrow.clean({})
        self.assertFalse(os.path.exists(b))
        self.assertTrue(os.path.exists(opt), "optional items must never be in Clean all")

    def test_duplicates_keep_newest_verifies_content(self):
        base = tempfile.mkdtemp(prefix="burrow-dupes-", dir=os.path.join(HOME, "Library", "Caches"))
        self.created.append(base)
        blob = os.urandom(200000)
        keep = self.make(os.path.join(base, "keep.bin"), blob)
        copy = self.make(os.path.join(base, "copy.bin"), blob)
        changed = self.make(os.path.join(base, "changed.bin"), blob[:-1] + b"!")
        msg = burrow.dupes_trash({"keep": [keep], "trash": [copy, changed], "size": len(blob), "label": "t"})
        self.assertIn("changed since the scan", msg)
        self.assertTrue(os.path.exists(keep))
        self.assertFalse(os.path.exists(copy))
        self.assertTrue(os.path.exists(changed))

    def test_trash_action_and_undo_action(self):
        base = tempfile.mkdtemp(prefix="burrow-trash-", dir=os.path.join(HOME, "Library", "Caches"))
        self.created.append(base)
        f = self.make(os.path.join(base, "big.file"))
        burrow.dispatch("trash", f, {"confirm": "Move?", "label": "Test trash", "bytes": 4096})
        self.assertFalse(os.path.exists(f))
        msg = burrow.dispatch("undo", "", {})
        self.assertIn("Put back 1 item", msg)
        self.assertTrue(os.path.exists(f))


if __name__ == "__main__":
    unittest.main()
