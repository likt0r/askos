"""The proxy's log lines (agentbox/proxy_log.py): what the proxy writes, agentbox reads back."""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from agentbox import proxy_log as log  # noqa: E402
from agentbox.proxy_log import Event  # noqa: E402

SIGNALS = ("exfil", "young", "look-alike", "[weak]", "c2(x)", "a]b") + tuple(f"s{n}" for n in range(60))
ODD = "[odd](host).example:443"


class RoundTripTest(unittest.TestCase):
    def test_every_kind_formats_and_parses_back_unchanged(self):
        cases = [
            (log.allowed("work", "docs.example", ("domain", "example", "all")),
             Event("work", "ALLOWED", "docs.example", entry=("domain", "example", "all"))),
            (log.allowed("work", ODD, ("regex", r"^(a|b)\.[x-z]+\(c\)\.example$", "work")),
             Event("work", "ALLOWED", ODD, entry=("regex", r"^(a|b)\.[x-z]+\(c\)\.example$", "work"))),
            (log.allowed("vm-2", "regex", ("domain", "regex", "vm-2")),
             Event("vm-2", "ALLOWED", "regex", entry=("domain", "regex", "vm-2"))),
            (log.allowed("work", ODD), Event("work", "ALLOWED", ODD)),
            (log.alert("work", ODD, SIGNALS), Event("work", "ALERT", ODD, signals=SIGNALS)),
            (log.alert("work", "a.example", ["exfil"]), Event("work", "ALERT", "a.example", signals=("exfil",))),
            (log.alert("work", "a.example", []), Event("work", "ALERT", "a.example")),
            (log.blocked("work", ODD, log.suspicious(SIGNALS)),
             Event("work", "BLOCKED", ODD, log.suspicious(SIGNALS), SIGNALS)),
            (log.blocked("work", "a.example", log.suspicious([])),
             Event("work", "BLOCKED", "a.example", log.SUSPICIOUS)),
            (log.blocked("work", "fe80::1", log.NOT_TLS), Event("work", "BLOCKED", "fe80::1", log.NOT_TLS)),
            (log.blocked("work", ODD, "a (nested) reason (x)"), Event("work", "BLOCKED", ODD, "a (nested) reason (x)")),
            (log.closed("work", ODD, log.OPEN_WEB_ENDED), Event("work", "CLOSED", ODD, log.OPEN_WEB_ENDED)),
            (log.closed("work", "a.example", log.REMOVED), Event("work", "CLOSED", "a.example", log.REMOVED)),
            (log.error("work", ODD, "[Errno 111] Connect call failed ('192.0.2.1', 443)"),
             Event("work", "ERROR", ODD, "[Errno 111] Connect call failed ('192.0.2.1', 443)")),
        ]
        for reason in log.BLOCK_REASONS:
            cases.append((log.blocked("work", "a.example", reason), Event("work", "BLOCKED", "a.example", reason)))
        for line, event in cases:
            with self.subTest(line=line):
                self.assertEqual(log.parse(line), event)

    def test_the_lines_are_the_documented_format(self):
        self.assertEqual(log.allowed("work", "a.example", ("domain", "example", "all")),
                         "[work] ALLOWED a.example (allowlist example all)")
        self.assertEqual(log.allowed("work", "a.example", ("regex", "^a$", "work")),
                         "[work] ALLOWED a.example (allowlist regex ^a$ work)")
        self.assertEqual(log.allowed("work", "a.example"), "[work] ALLOWED a.example (open web)")
        self.assertEqual(log.blocked("work", "a.example", log.suspicious(["exfil", "c2"])),
                         "[work] BLOCKED a.example (suspicious: exfil,c2)")
        self.assertEqual(log.alert("work", "a.example", ["exfil", "c2"]), "[work] ALERT a.example [exfil,c2]")
        self.assertEqual(log.closed("work", "a.example", log.REMOVED),
                         "[work] CLOSED a.example (removed from the Allowlist)")
        self.assertEqual(log.error("work", "a.example", "timed out"), "[work] ERROR a.example (timed out)")

    def test_other_lines_are_no_events(self):
        for line in (log.message("work", "ready"),
                     log.message("work", "ERROR check of open tunnels failed (KeyError: 'x')"),
                     log.message("work", "ERROR /etc/agentbox/allowlist.json: regex entry 'a b' left out (why)"),
                     log.message("work", "ERROR /etc/agentbox/allowlist.json unreadable (x), the previous entries stay"),
                     "[work] ALLOWED a.example (somewhere else)", "[Work] ALERT a.example [x]",
                     "[work] ALERT a.example [x] trailing", "ALERT a.example [x]", ""):
            with self.subTest(line=line):
                self.assertIsNone(log.parse(line))

    def test_block_codes(self):
        self.assertEqual({r: log.block_code(r) for r in log.BLOCK_REASONS}, log.BLOCK_REASONS)
        self.assertEqual(log.block_code(log.suspicious(["exfil"])), "suspicious")
        self.assertIsNone(log.block_code("something new"))

    def test_grep(self):
        self.assertEqual(log.grep("ALERT"), r"^\[[a-z0-9-]+\] ALERT ")
        self.assertEqual(log.grep("ALLOWED", "BLOCKED"), r"^\[[a-z0-9-]+\] (ALLOWED|BLOCKED) ")


if __name__ == "__main__":
    unittest.main()
