"""Tests for Processes (bukill): listing, grouping, and ending real throwaway processes.
Run: /usr/bin/python3 -m unittest discover tests"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("alfred_workflow_cache", tempfile.mkdtemp())

import burrow  # noqa: E402
import engine  # noqa: E402


class ListTest(unittest.TestCase):
    def test_cpu_seconds(self):
        self.assertEqual(engine._cpu_seconds("1:02.50"), 62.5)
        self.assertEqual(engine._cpu_seconds("1:00:00.00"), 3600)
        self.assertEqual(engine._cpu_seconds("2-01:00:00"), 2 * 86400 + 3600)

    def test_helpers_are_grouped_under_their_app(self):
        procs = [
            {"pid": 10, "ppid": 1, "uid": 501, "cpu": 1.0, "mem": 100, "path": "/Applications/X.app/Contents/MacOS/X", "name": "X"},
            {"pid": 11, "ppid": 10, "uid": 501, "cpu": 2.0, "mem": 200,
             "path": "/Applications/X.app/Contents/Frameworks/X Helper.app/Contents/MacOS/X Helper", "name": "X Helper"},
            {"pid": 20, "ppid": 1, "uid": 501, "cpu": 0.5, "mem": 50, "path": "/usr/bin/worker", "name": "worker"},
            {"pid": 21, "ppid": 1, "uid": 501, "cpu": 0.5, "mem": 50, "path": "/usr/bin/worker", "name": "worker"},
            {"pid": 30, "ppid": 5, "uid": 0, "cpu": 0, "mem": 1, "path": "/usr/libexec/launchd", "name": "launchd"},
        ]
        groups = {g["name"]: g for g in engine.group_processes(procs)}
        self.assertEqual(groups["X"]["pids"], [10, 11])
        self.assertEqual(groups["X"]["pid"], 10)
        self.assertEqual((groups["X"]["cpu"], groups["X"]["mem"]), (3.0, 300))
        self.assertEqual(groups["worker"]["pids"], [20, 21])
        self.assertTrue(groups["launchd"]["critical"])

    def test_list_is_valid_and_fast_while_typing(self):
        engine.list_processes()
        start = time.time()
        procs = engine.list_processes()
        self.assertLess(time.time() - start, 0.4)
        self.assertTrue(any(p["pid"] == os.getpid() for p in procs))


class EndTest(unittest.TestCase):
    def setUp(self):
        burrow.confirm = lambda *a, **k: True
        burrow.alfred_search = lambda *a, **k: None
        self.proc = subprocess.Popen(["/bin/sleep", "300"])
        time.sleep(0.2)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def payload(self, path="/bin/sleep"):
        return {"pids": [self.proc.pid], "pid": self.proc.pid, "path": path, "app": None, "name": "sleep", "critical": False}

    def test_end(self):
        self.assertEqual(burrow.proc_quit(self.payload(), False), "Ended sleep")
        self.assertIsNotNone(self.proc.poll())

    def test_force_quit(self):
        self.assertEqual(burrow.proc_quit(self.payload(), True), "Force quit sleep")
        self.assertIsNotNone(self.proc.poll())

    def test_reused_pid_is_left_alone(self):
        # The list showed another program under this pid: don't touch what runs there now
        msg = burrow.proc_quit(self.payload("/usr/bin/something-else"), True)
        self.assertIn("isn't running any more", msg)
        self.assertIsNone(self.proc.poll())

    def test_cancelled_force_quit_does_nothing(self):
        burrow.confirm = lambda *a, **k: False
        self.assertIsNone(burrow.proc_quit(self.payload(), True))
        self.assertIsNone(self.proc.poll())


LSOF_SAMPLE = """p100
cnode
u501
f20
PTCP
n127.0.0.1:3000
f21
PTCP
n[::1]:3000
p200
cControlCenter
u501
f9
PTCP
n*:7000
"""


class PortsTest(unittest.TestCase):
    def test_parse_merges_addresses(self):
        rows = engine.parse_lsof_ports(LSOF_SAMPLE)
        self.assertEqual([(r["port"], r["pid"], r["command"]) for r in rows], [(3000, 100, "node"), (7000, 200, "ControlCenter")])
        self.assertEqual(rows[0]["addresses"], ["127.0.0.1", "::1"])
        self.assertEqual(rows[1]["addresses"], ["*"])

    def test_real_server_shows_up_and_ends(self):
        burrow.confirm = lambda *a, **k: True
        burrow.alfred_search = lambda *a, **k: None
        server = subprocess.Popen([sys.executable, "-c",
                                   "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
                                   "print(s.getsockname()[1], flush=True); time.sleep(300)"], stdout=subprocess.PIPE)
        try:
            port = int(server.stdout.readline())
            row = next((r for r in engine.listening_ports()[0] if r["port"] == port), None)
            self.assertIsNotNone(row, "the test server's port is listed")
            self.assertEqual(row["pid"], server.pid)
            self.assertFalse(row["system"])
            item = burrow.port_item(row)
            self.assertEqual(item["mods"]["fn"]["variables"]["target"], "http://localhost:{}".format(port))
            payload = {k: v for k, v in __import__("json").loads(item["variables"]["payload"]).items()}
            self.assertEqual(burrow.proc_quit(payload, False), "Ended " + payload["name"])
            server.wait(timeout=5)
        finally:
            if server.poll() is None:
                server.kill()
            server.wait()


class QuitAllTest(unittest.TestCase):
    APPS = [
        {"name": "Finder", "bundle_id": "com.apple.finder", "path": "", "pid": 1, "front": False},
        {"name": "Music", "bundle_id": "com.apple.Music", "path": "", "pid": 2, "front": False},
        {"name": "Notes", "bundle_id": "com.apple.Notes", "path": "", "pid": 3, "front": True},
        {"name": "Terminal", "bundle_id": "com.apple.Terminal", "path": "", "pid": 4, "front": False},
    ]

    def test_exclusions(self):
        names = lambda apps: [a["name"] for a in apps]  # noqa: E731
        self.assertEqual(names(engine.apps_to_quit(self.APPS)), ["Music", "Notes", "Terminal"])
        self.assertEqual(names(engine.apps_to_quit(self.APPS, except_front=True)), ["Music", "Terminal"])
        self.assertEqual(names(engine.apps_to_quit(self.APPS, exclude="music, com.apple.terminal")), ["Notes"])

    def test_quits_a_real_app(self):
        subprocess.run(["/usr/bin/open", "-g", "-a", "Calculator"])
        calc = None
        for _ in range(20):
            calc = next((a for a in engine.foreground_apps() if a["bundle_id"] == "com.apple.calculator"), None)
            if calc:
                break
            time.sleep(0.25)
        self.assertIsNotNone(calc, "Calculator is listed as an open app")
        time.sleep(1.5)  # let it finish launching
        self.assertEqual(engine.quit_apps([calc]), [])
        self.assertFalse(any(a["bundle_id"] == "com.apple.calculator" for a in engine.foreground_apps()))


if __name__ == "__main__":
    unittest.main()
