"""Tests for Network (bunet). Run: /usr/bin/python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("alfred_workflow_cache", tempfile.mkdtemp())

import burrow  # noqa: E402
import engine  # noqa: E402

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1c%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.168.1.38 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 2400:adc5::1 prefixlen 64 autoconf secured
en5: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.0.0.2 netmask 0xffffff00
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 10.8.0.5 --> 10.8.0.5 netmask 0xffffffff
"""
PORTS = "Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: aa\n\nHardware Port: USB LAN\nDevice: en5\n"
SUMMARY = "<dictionary> {\n  InterfaceType : WiFi\n  SSID : <redacted>\n  Security : WPA2_PSK\n}\n"
DNS = "resolver #1\n  nameserver[0] : 192.168.1.254\n  nameserver[1] : 1.1.1.1\nresolver #2\n  nameserver[0] : 9.9.9.9\n"


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.real = engine.sh

        def fake(args, timeout=10):
            joined = " ".join(args)
            if args[0] == "/sbin/ifconfig":
                return IFCONFIG
            if "listallhardwareports" in joined:
                return PORTS
            if "getairportpower" in joined:
                return "Wi-Fi Power (en0): On"
            if "getsummary" in joined:
                return SUMMARY
            if "getifaddr" in joined:
                return "192.168.1.38\n"
            if args[0] == "/usr/sbin/scutil":
                return DNS
            if args[0] == "/sbin/route":
                return "   route to: default\n    gateway: 192.168.1.254\n  interface: en0\n"
            return ""
        engine.sh = fake

    def tearDown(self):
        engine.sh = self.real

    def test_addresses_skip_loopback_down_and_link_local(self):
        rows = engine.local_addresses()
        self.assertEqual([(r["service"], r["ipv4"], r["ipv6"]) for r in rows],
                         [("Wi-Fi", ["192.168.1.38"], ["2400:adc5::1"]), ("VPN", ["10.8.0.5"], [])])

    def test_wifi_state_hidden_name(self):
        st = engine.wifi_state("en0")
        self.assertEqual((st["power"], st["connected"], st["ssid"], st["security"], st["ip"]), (True, True, None, "WPA2 PSK", "192.168.1.38"))

    def test_dns_and_gateway(self):
        self.assertEqual(engine.dns_servers(), ["192.168.1.254", "1.1.1.1"])
        self.assertEqual(engine.default_gateway(), {"gateway": "192.168.1.254", "interface": "en0"})

    def test_formatting(self):
        self.assertEqual(burrow.format_bps(49409564), "49.4 Mbps")
        self.assertEqual(burrow.format_bps(245e6), "245 Mbps")
        self.assertEqual(engine.signal_quality(-33), "excellent")
        self.assertEqual(engine.signal_quality(-75), "weak")


class SpeedTest(unittest.TestCase):
    def test_parses_network_quality(self):
        import subprocess
        real = engine.subprocess.run

        class R:
            stdout = b'{"dl_throughput": 49409564, "ul_throughput": 46997408, "responsiveness": 70.4, "base_rtt": 377.0, "interface_name": "en0"}'
            stderr = b""
            returncode = 0
        engine.subprocess.run = lambda *a, **k: R
        try:
            r = engine.speed_test()
        finally:
            engine.subprocess.run = real
        self.assertEqual((r["download"], r["upload"], r["responsiveness"]), (49409564, 46997408, "Low"))
        self.assertIs(subprocess.run, real)

    def test_failure_is_reported(self):
        real = engine.subprocess.run

        class R:
            stdout = b""
            stderr = b"Error: no network\n"
            returncode = 1
        engine.subprocess.run = lambda *a, **k: R
        try:
            with self.assertRaisesRegex(RuntimeError, "no network"):
                engine.speed_test()
        finally:
            engine.subprocess.run = real


class WifiPasswordTest(unittest.TestCase):
    def setUp(self):
        self.real = (engine.run_as_admin, engine.sh)

    def tearDown(self):
        engine.run_as_admin, engine.sh = self.real

    def test_password_for_a_network_with_quotes_in_its_name(self):
        seen = {}

        def admin(script, prompt):
            seen["script"] = script
            return True, "s3cret pass\n"
        engine.run_as_admin = admin
        self.assertEqual(engine.wifi_password("Bob's Wi-Fi"), "s3cret pass")
        import shlex
        self.assertIn(shlex.quote("Bob's Wi-Fi"), seen["script"])

    def test_cancelled_and_missing(self):
        engine.run_as_admin = lambda s, p: (None, "Cancelled")
        self.assertIsNone(engine.wifi_password("Home"))
        engine.run_as_admin = lambda s, p: (False, "security: SecKeychainSearchCopyNext: The specified item could not be found")
        self.assertEqual(engine.wifi_password("Home"), "")

    def test_saved_networks_in_preferred_order(self):
        dump = ("keychain: \"/Library/Keychains/System.keychain\"\nclass: \"genp\"\nattributes:\n    \"acct\"<blob>=\"Cafe\"\n"
                "    \"desc\"<blob>=\"AirPort network password\"\n"
                "keychain: \"/Library/Keychains/System.keychain\"\nclass: \"genp\"\nattributes:\n    \"acct\"<blob>=\"Home\"\n"
                "    \"desc\"<blob>=\"AirPort network password\"\n"
                "keychain: \"/Library/Keychains/System.keychain\"\nclass: \"genp\"\nattributes:\n    \"acct\"<blob>=\"com.apple.other\"\n")
        engine.sh = lambda args, timeout=10: dump if "dump-keychain" in args else "Preferred networks on en0:\n\tHome\n\tOffice\n"
        self.assertEqual(engine.saved_wifi_networks("en0"), ["Home", "Cafe"])


class ConcealedCopyTest(unittest.TestCase):
    def test_marks_clipboard_concealed(self):
        import subprocess
        before = subprocess.run(["/usr/bin/pbpaste"], capture_output=True).stdout
        try:
            self.assertTrue(engine.copy_concealed("burrow-test-secret"))
            self.assertEqual(subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True).stdout, "burrow-test-secret")
            types = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e",
                                    "ObjC.import('AppKit'); $.NSPasteboard.generalPasteboard.types.js.map(t => t.js).join(',')"],
                                   capture_output=True, text=True).stdout
            self.assertIn("org.nspasteboard.ConcealedType", types)
        finally:
            subprocess.run(["/usr/bin/pbcopy"], input=before)


if __name__ == "__main__":
    unittest.main()
