"""Unit tests for classify_state. Run with `python3 -m unittest`."""
import os
import sys
import unittest

# Make the parent dir importable when running this file directly.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from classify_state import classify, to_bool, BoolParseError, safe_bool


def row(**overrides):
    base = {
        "fqdn": "host.example.com",
        "ipv4": "10.0.0.1",
        "reachable": "true",
        "ssh_open": "true",
        "error_class": "",
        "detection_method": "ssh-facts",
    }
    base.update(overrides)
    return base


class TestBool(unittest.TestCase):
    def test_true_literals(self):
        for v in ("true", "True", "1", "yes", "YES"):
            self.assertTrue(to_bool(v))

    def test_false_literals(self):
        for v in ("false", "0", "no", "", None):
            self.assertFalse(to_bool(v))

    def test_garbage_raises(self):
        with self.assertRaises(BoolParseError):
            to_bool("maybe")

    def test_safe_bool_swallows(self):
        self.assertFalse(safe_bool("maybe"))


class TestClassify(unittest.TestCase):
    def test_up(self):
        self.assertEqual(classify(row()), "up")

    def test_auth_failed(self):
        self.assertEqual(
            classify(row(error_class="auth-failed", detection_method="")),
            "auth_failed",
        )

    def test_ssh_blocked_timeout(self):
        self.assertEqual(
            classify(row(error_class="timeout", detection_method="")),
            "ssh_blocked",
        )

    def test_ssh_blocked_python_missing(self):
        self.assertEqual(
            classify(row(error_class="python-missing", detection_method="")),
            "ssh_blocked",
        )

    def test_online_unmanaged(self):
        self.assertEqual(
            classify(row(ssh_open="false", error_class="", detection_method="nmap-guess")),
            "online_unmanaged",
        )

    def test_down(self):
        self.assertEqual(
            classify(row(reachable="false", ssh_open="false",
                          detection_method="none")),
            "down",
        )

    def test_excluded_via_allowlist_specific_inv(self):
        al = {("prod", "host.example.com")}
        r = row()
        r["inventory"] = "prod"
        self.assertEqual(classify(r, allowlist=al), "excluded")

    def test_excluded_via_allowlist_wildcard_inv(self):
        al = {(None, "host.example.com")}
        r = row()
        r["inventory"] = "prod"
        self.assertEqual(classify(r, allowlist=al), "excluded")

    def test_not_excluded_wrong_inv(self):
        al = {("prod", "host.example.com")}
        r = row()
        r["inventory"] = "qa"
        self.assertEqual(classify(r, allowlist=al), "up")

    def test_priority_up_beats_other(self):
        # SSH-open, no error, ssh-facts → up regardless of reachable phrasing.
        self.assertEqual(classify(row()), "up")

    def test_up_requires_ssh_facts(self):
        # reachable + ssh_open but no facts → falls through to ssh_blocked/online_unmanaged.
        # Here: error_class empty + detection_method='nmap-partial' → online_unmanaged would
        # only match if ssh_open=false. With ssh_open=true we'd fall through to default.
        # The classifier returns 'down' as last-resort.
        r = row(detection_method="nmap-partial")
        # ssh_open=true, error_class empty, but detection != ssh-facts → no rule matches
        # until the final default 'down'.
        self.assertEqual(classify(r), "down")


if __name__ == "__main__":
    unittest.main()
