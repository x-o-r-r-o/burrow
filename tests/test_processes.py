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


if __name__ == "__main__":
    unittest.main()
