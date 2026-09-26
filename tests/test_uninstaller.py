"""Tests for the Uninstaller window's commands (uninstaller.py), on a throwaway app.
Run: /usr/bin/python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_actions  # noqa: E402
from test_actions import BID, NAME  # noqa: E402

import burrow  # noqa: E402
import engine  # noqa: E402
import uninstaller  # noqa: E402


class UninstallerCLITest(unittest.TestCase):
    # The throwaway app helpers, without re-running test_actions' own tests
    setUp = test_actions.ActionTest.setUp
    tearDown = test_actions.ActionTest.tearDown
    make = test_actions.ActionTest.make
    make_app = test_actions.ActionTest.make_app

    def test_list_review_toggle_uninstall_undo(self):
        app, leftovers = self.make_app()
        self.assertIn(app, [a["path"] for a in uninstaller.list_apps()["apps"]])

        r = uninstaller.review(app)
        self.assertEqual((r["name"], r["bundle_id"]), (NAME, BID))
        found = {i["path"] for i in r["items"]}
        self.assertTrue(set(leftovers) <= found, set(leftovers) - found)

        keep = leftovers[0]
        uninstaller.toggle(app, keep)
        self.assertTrue(next(i for i in uninstaller.review(app)["items"] if i["path"] == keep)["kept"])

        out = uninstaller.uninstall({"path": app})
        self.assertIn("uninstalled", out.get("done", ""), out)
        self.assertNotIn("undo with", out["done"])
        self.assertFalse(os.path.exists(app))
        self.assertTrue(os.path.exists(keep), "a kept leftover stays")
        for p in leftovers[1:]:
            self.assertFalse(os.path.lexists(p), p)
        self.assertEqual(uninstaller.list_apps()["undo"], "Uninstall " + NAME)

        out = uninstaller.undo()
        self.assertIn("Put back", out["done"])
        for p in [app] + leftovers:
            self.assertTrue(os.path.lexists(p), p)

    def test_reset_keeps_app(self):
        app, leftovers = self.make_app()
        out = uninstaller.uninstall({"path": app, "reset": True})
        self.assertIn("reset", out["done"])
        self.assertTrue(os.path.exists(app))
        engine.undo_trash_batch(engine.last_trash_batch())

    def test_missing_app(self):
        self.assertIn("error", uninstaller.review("/Applications/Nope-Burrow.app"))
        self.assertIn("error", uninstaller.uninstall({"path": "/Applications/Nope-Burrow.app"}))


if __name__ == "__main__":
    unittest.main()
