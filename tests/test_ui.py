"""Runs every Script Filter the way Alfred does and checks the JSON it returns.
Run: /usr/bin/python3 -m unittest discover tests"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)

import burrow  # noqa: E402

COMMANDS = [c for c in burrow.COMMANDS if c != "run"]


def run(command, query="", cache=None):
    env = dict(os.environ, alfred_workflow_cache=cache or tempfile.mkdtemp(), alfred_workflow_bundleid="io.github.burrow-alfred")
    out = subprocess.run(["/bin/bash", os.path.join(SRC, "run.sh"), command, query], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=120)
    return json.loads(out.stdout.decode() or "{}")


class ScriptFilterJSONTest(unittest.TestCase):
    def check(self, data, command):
        self.assertIn("items", data, command)
        self.assertTrue(data["items"], "{} returned no items".format(command))
        for it in data["items"]:
            self.assertIsInstance(it.get("title"), str, command)
            self.assertNotEqual(it["title"], "Something Went Wrong", "{}: {}".format(command, it.get("subtitle")))
            if it.get("valid", True) and "variables" in it:
                self.assertIn("action", it["variables"], command)
            for key, m in (it.get("mods") or {}).items():
                self.assertIn(key, ("cmd", "alt", "ctrl", "shift", "fn"), command)
                self.assertIn("action", m.get("variables", {}), command)
            if "rerun" in data:
                self.assertTrue(0.1 <= data["rerun"] <= 5, command)

    def test_every_command_returns_valid_items(self):
        cache = tempfile.mkdtemp()
        for command in COMMANDS:
            with self.subTest(command=command):
                self.check(run(command, "", cache), command)

    def test_background_scans_finish(self):
        cache = tempfile.mkdtemp()
        for command in ("clean", "purge", "analyze"):
            query = "~/Documents/" if command == "analyze" else ""
            data = run(command, query, cache)
            deadline = time.time() + 90
            while "rerun" in data and time.time() < deadline:
                time.sleep(0.5)
                data = run(command, query, cache)
            self.assertNotIn("rerun", data, command + " never finished")
            self.check(data, command)

    def test_typing_a_path_does_not_start_a_scan(self):
        cache = tempfile.mkdtemp()
        data = run("dupes", "~/", cache)
        self.assertNotIn("rerun", data)
        self.assertFalse(os.path.isdir(os.path.join(cache, "jobs")) and os.listdir(os.path.join(cache, "jobs")))
        self.assertTrue(data["items"][0]["autocomplete"].startswith("@"))

    def test_uninstall_words_only_special_alone(self):
        self.check(run("uninstall", "big"), "uninstall big")
        name = next(a["name"] for a in burrow.engine.list_apps() if not a["name"].startswith("Alfred"))
        word = name.split()[0].lower()
        self.assertTrue(all(word in i["title"].lower() for i in run("uninstall", word)["items"]))
        self.assertFalse(any(i["title"].startswith("Alfred") for i in run("uninstall", "alfred")["items"]))

    def test_dupes_scope_with_spaces(self):
        cache = tempfile.mkdtemp()
        folder = tempfile.mkdtemp(suffix=" My Stuff")
        run("dupes", "@" + folder + "/", cache)
        with open(os.path.join(cache, "dupes-last.json")) as f:
            self.assertEqual(json.load(f)["roots"], [os.path.realpath(folder)] if os.path.realpath(folder) == folder else [folder])

    def test_uninstall_preview(self):
        data = run("uninstall", "=/Applications/Safari.app")
        self.check(data, "uninstall preview")
        self.assertTrue(any(i["title"].startswith(("Uninstall", "App Not Found")) for i in data["items"]))

    def test_renamed_keywords_are_followed(self):
        import plistlib
        import shutil
        wf = tempfile.mkdtemp()
        build = os.path.join(SRC, "..", "build")
        if not os.path.exists(os.path.join(build, "info.plist")):
            self.skipTest("build first")
        with open(os.path.join(build, "info.plist"), "rb") as f:
            info = plistlib.load(f)
        for obj in info["objects"]:  # a keyword typed directly in the Script Filter
            if obj["config"].get("keyword") == "{var:keyword_clean}":
                obj["config"]["keyword"] = "sweep"
        with open(os.path.join(wf, "info.plist"), "wb") as f:
            plistlib.dump(info, f)
        old = burrow.WF_DIR
        burrow.WF_DIR = wf
        try:
            words = burrow.load_keywords()
            self.assertEqual(words["clean"], "sweep")
            self.assertEqual(words["status"], "bustatus")  # the configuration default
            self.assertEqual(words["hub"], "burrow")
            os.environ["keyword_status"] = "health"  # set in the Workflow's Configuration
            self.assertEqual(burrow.load_keywords()["status"], "health")
        finally:
            burrow.WF_DIR = old
            os.environ.pop("keyword_status", None)
            shutil.rmtree(wf, ignore_errors=True)

    def test_first_run_screen_is_valid_json(self):
        import shutil
        wf = tempfile.mkdtemp()
        with open(os.path.join(SRC, "run.sh")) as f:
            script = f.read().replace("python_ready() {", "python_ready() { return 1;", 1)
        with open(os.path.join(wf, "run.sh"), "w") as f:
            f.write(script)
        cache = tempfile.mkdtemp()
        out = subprocess.run(["/bin/bash", os.path.join(wf, "run.sh"), "hub", ""], stdout=subprocess.PIPE,
                             env=dict(os.environ, alfred_workflow_cache=cache)).stdout
        data = json.loads(out.decode())
        self.assertNotIn("rerun", data)  # nothing is installed automatically, so nothing to wait for
        self.assertEqual([i.get("variables", {}).get("action") for i in data["items"]], ["setup", None])
        self.assertIn("xcode-select --install", data["items"][0]["subtitle"])
        shutil.rmtree(wf, ignore_errors=True)

    def test_companion_rows(self):
        real, real_emit, out = burrow.companion_path, burrow.emit, []
        burrow.emit = lambda items, **k: out.append(items)
        try:
            burrow.companion_path = lambda: None
            burrow.cmd_menubar("")
            self.assertEqual(out[-1][0]["variables"]["action"], "open")
            self.assertIsNone(burrow.window_item("updates", "t", "s"))  # no nagging where it's optional
            burrow.companion_path = lambda: "/Applications/Burrow Companion.app"
            burrow.cmd_menubar("")
            self.assertEqual({i["variables"]["target"] for i in out[-1]}, {"menubar", "updates", "browsers"})
            self.assertEqual(burrow.window_item("browsers", "t", "s")["variables"]["action"], "companion")
        finally:
            burrow.companion_path, burrow.emit = real, real_emit

    def test_legacy_login_item_is_removed(self):
        agent = os.path.join(tempfile.mkdtemp(), "io.github.burrow-alfred.menubar.plist")
        open(agent, "w").close()
        real, real_sh = burrow.LEGACY_AGENT, burrow.engine.sh
        burrow.LEGACY_AGENT = agent
        burrow.engine.sh = lambda *a, **k: ""  # don't stop a helper that's really running
        try:
            burrow.retire_legacy_helpers()
            self.assertFalse(os.path.exists(agent))
        finally:
            burrow.LEGACY_AGENT, burrow.engine.sh = real, real_sh

    def test_queries_filter(self):
        data = run("hub", "clean")
        self.assertTrue(all("clean" in (i["title"] + i["subtitle"]).lower() for i in data["items"]))


if __name__ == "__main__":
    unittest.main()
