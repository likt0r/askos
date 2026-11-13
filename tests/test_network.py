"""`agentbox blocked|alerts|open-web|close-web` against a temporary root
(AGENTBOX_ROOT), a fake `journalctl` on PATH and a fake classifier. The Allowlist
commands are in test_allowlist.py.

Expected values are written down as literals (originally the output of the former
Bash version of agentbox for the same input).

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import vm_records  # noqa: E402
from agentbox import network  # noqa: E402
from journal_writers import CLI, FORGED, PROXY, proxy, write_passwd, writer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(ROOT, "bin", "agentbox")
# fixed UTC+1 without daylight saving, so the expected times do not depend on the Host
TZ = "CET-1"

FAKE_JOURNALCTL = '''#!PYTHON
"""Fake journalctl: records its arguments, prints the canned proxy log (one entry
per line, as -o json prints them; newest first with --reverse). It ignores matches
such as _UID=, so agentbox's own check of who wrote a line sees every line."""
import json, os, sys
d = DIR
with open(os.path.join(d, "journalctl.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
try:
    with open(os.path.join(d, "journal.txt")) as f:
        lines = f.read().splitlines()
except FileNotFoundError:
    lines = []
for line in reversed(lines) if "--reverse" in sys.argv else lines:
    print(line)
try:
    with open(os.path.join(d, "journal-exit")) as f:
        code = int(f.read())
    print("Failed to open journal: Permission denied", file=sys.stderr)
    sys.exit(code)
except FileNotFoundError:
    pass
'''


def as_json(text, **trusted):
    """Proxy lines as journalctl -o short-unix prints them, as the entries of -o json:
    written by the proxy their identifier names, or by whoever `trusted` says."""
    out = []
    for line in text.splitlines():
        sec, usec, ident, message = re.fullmatch(r"(\d+)\.(\d{6}) \S+ (\S+)\[\d+\]: (.*)", line).groups()
        out.append(json.dumps({"__REALTIME_TIMESTAMP": sec + usec, "SYSLOG_IDENTIFIER": ident, "MESSAGE": message,
                               **(trusted or writer(ident))}) + "\n")
    return "".join(out)

FAKE_CLASSIFIER = '''#!PYTHON
"""Fake agentbox-suspicious: records its arguments, answers from suspicious.json
({domain: [exit code, output]}); other domains are unremarkable."""
import json, os, sys
d = DIR
with open(os.path.join(d, "suspicious.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
try:
    with open(os.path.join(d, "suspicious.json")) as f:
        answers = json.load(f)
except FileNotFoundError:
    answers = {}
code, out = answers.get(sys.argv[1], [0, "\\n"])
sys.stdout.write(out)
sys.exit(code)
'''

# the proxy log of `work` as journalctl -o short-unix prints it
JOURNAL = """\
1793700000.123456 titan agentbox-proxy-work[812]: [work] ALERT evil.example [exfil]
1793700000.223456 titan agentbox-proxy-work[812]: [work] BLOCKED evil.example (not on the Allowlist)
1793700001.000000 titan agentbox-proxy-work[812]: [work] ready
1793700300.000001 titan agentbox-proxy-work[812]: [work] BLOCKED pypi.org (not on the Allowlist)
1793700400.000001 titan agentbox-proxy-work[812]: [work] BLOCKED 10.77.1.2 (not a TLS handshake)
1793700500.000001 titan agentbox-proxy-work[812]: [work] BLOCKED 10.77.1.2 (no server name)
1793700550.000001 titan agentbox-proxy-work[812]: [work] ALLOWED example.org (allowlist example.org all)
1793700600.000001 titan agentbox-proxy-work[812]: [work] ALERT evil.example [exfil,young]
1793700600.000002 titan agentbox-proxy-work[812]: [work] BLOCKED evil.example (suspicious: exfil,young)
1793700900.500000 titan agentbox-proxy-work[812]: [work] ALERT paste.example [paste]
1793700950.000001 titan agentbox-proxy-work[812]: [work] CLOSED example.org (removed from the Allowlist)
1793701100.000001 titan agentbox-proxy-work[812]: [work] BLOCKED ten.internal (does not resolve to a public address)
1793701200.000001 titan agentbox-proxy-work[812]: [work] BLOCKED pypi.org (not on the Allowlist)
"""

UNKNOWN_VM = "agentbox: No such VM: dev\n"
NO_VM = "agentbox: no VM given\n"
BAD_MINUTES = "agentbox: minutes must be from 1 to 240\n"


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.root = os.path.join(self.dir, "root")
        self.conf = os.path.join(self.root, "etc", "agentbox")
        os.makedirs(self.conf)
        vm_records.write(self.root)
        write_passwd(self.root)
        os.makedirs(os.path.join(self.dir, "bin"))
        libexec = os.path.join(self.root, "usr", "libexec")
        os.makedirs(libexec)
        self.fake(os.path.join(self.dir, "bin", "journalctl"), FAKE_JOURNALCTL)
        self.fake(os.path.join(libexec, "agentbox-suspicious"), FAKE_CLASSIFIER)

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, path, source):
        with open(path, "w") as f:
            f.write(source.replace("PYTHON", sys.executable, 1).replace("DIR", repr(self.dir), 1))
        os.chmod(path, 0o755)

    def run_agentbox(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": TZ}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def journal(self, text, exit_code=None, forged=""):
        """The proxy log: lines as short-unix prints them, by the proxy (text) or by
        a local user's process (forged)."""
        with open(os.path.join(self.dir, "journal.txt"), "w") as f:
            f.write(as_json(text) + as_json(forged, **FORGED))
        if exit_code is not None:
            with open(os.path.join(self.dir, "journal-exit"), "w") as f:
                f.write(str(exit_code))

    def classifier(self, answers):
        with open(os.path.join(self.dir, "suspicious.json"), "w") as f:
            json.dump(answers, f)

    def recorded(self, name):
        try:
            with open(os.path.join(self.dir, name)) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def write_conf(self, name, data, mode=0o644):
        path = os.path.join(self.conf, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode)

    def read_conf(self, name):
        with open(os.path.join(self.conf, name), "rb") as f:
            return f.read()

    def mode(self, name):
        return stat.S_IMODE(os.stat(os.path.join(self.conf, name)).st_mode)

    def conf_files(self):
        """Files in /etc/agentbox besides the VM registry and the Watchlist."""
        return sorted(f for f in os.listdir(self.conf) if f not in ("vms", "watchlist.json"))

    def journalctl_calls(self):
        """journalctl's arguments, with --since=@<24 hours ago> as --since=@-24h."""
        calls = self.recorded("journalctl.jsonl")
        for argv in calls:
            for i, a in enumerate(argv):
                if a.startswith("--since=@"):
                    self.assertAlmostEqual(int(a[len("--since=@"):]), time.time() - 86400, delta=60)
                    argv[i] = "--since=@-24h"
        return calls


# the Activity namespace the proxy logs into and the default journal it logged into before,
# Alerts and blocked destinations of the last 24 hours, newest first, by the proxies' user
def proxy_log_call(vm):
    return ["--namespace=+agentbox", "--output=json", "--no-pager", "--reverse", "--since=@-24h",
            f"--identifier=agentbox-proxy-{vm}", r"--grep=^\[[a-z0-9-]+\] (ALERT|BLOCKED) ", f"_UID={PROXY}"]


UNTRUSTED = "agentbox: journal entries without a known writer left out: {}\n"


class BlockedTest(NetworkTest):
    def test_text_groups_by_domain_newest_first(self):
        # pypi.org twice; evil.example keeps the reason of its first block and
        # shows the Signals of its last Alert; lines without a plain domain are skipped
        self.journal(JOURNAL)
        code, out, err = self.run_agentbox("blocked", "work")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         "pypi.org                                    2x  last 2026-11-03 11:20  (not on the Allowlist)\n"
                         "ten.internal                                1x  last 2026-11-03 11:18  (does not resolve to a public address)\n"
                         "evil.example                                2x  last 2026-11-03 11:10  (not on the Allowlist)  ALERT: exfil,young\n"
                         "10.77.1.2                                   2x  last 2026-11-03 11:08  (not a TLS handshake)\n")

    def test_json_for_the_control_room(self):
        self.journal(JOURNAL)
        code, out, err = self.run_agentbox("blocked", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         '[{"domain": "pypi.org", "count": 2, "last": 1793701200, "reason": "not on the Allowlist", '
                         '"code": "not-allowed", "params": {}, "suspicious": []}, {"domain": "ten.internal", "count": 1, '
                         '"last": 1793701100, "reason": "does not resolve to a public address", "code": "not-public", '
                         '"params": {}, "suspicious": []}, {"domain": "evil.example", "count": 2, "last": 1793700600, '
                         '"reason": "not on the Allowlist", "code": "not-allowed", "params": {}, '
                         '"suspicious": ["exfil", "young"]}, {"domain": "10.77.1.2", "count": 2, "last": 1793700500, '
                         '"reason": "not a TLS handshake", "code": "not-tls", "params": {}, "suspicious": []}]\n')

    def test_a_client_address_and_a_destination_of_the_same_text_are_two_rows(self):
        # a VM may also name an address as the server: that is a destination, not the client
        self.journal("1793701300.000001 titan agentbox-proxy-work[812]: [work] ALERT 10.77.1.2 [raw-ip]\n"
                     "1793701300.000002 titan agentbox-proxy-work[812]: [work] BLOCKED 10.77.1.2 "
                     "(not on the Allowlist)\n"
                     "1793701300.000003 titan agentbox-proxy-work[812]: [work] BLOCKED 10.77.1.2 (no server name)\n"
                     "1793701300.000004 titan agentbox-proxy-work[812]: [work] BLOCKED 10.77.1.2 "
                     "(not a TLS handshake)\n")
        rows = json.loads(self.run_agentbox("blocked", "work", "--json")[1])
        self.assertEqual(sorted((r["domain"], r["code"], r["count"], r["suspicious"]) for r in rows),
                         [("10.77.1.2", "no-server-name", 2, []), ("10.77.1.2", "not-allowed", 1, ["raw-ip"])])

    def test_the_reason_of_a_block_has_a_code(self):
        # the proxy's reasons are part of its log format; one this does not know has no code
        self.journal("1793701300.000001 titan agentbox-proxy-work[812]: [work] BLOCKED a.example "
                     "(suspicious: exfil,raw-ip)\n"
                     "1793701300.000002 titan agentbox-proxy-work[812]: [work] BLOCKED b.example "
                     "(Watchlist unreadable, Open web closed)\n"
                     "1793701300.000003 titan agentbox-proxy-work[812]: [work] BLOCKED c.example (something new)\n")
        rows = json.loads(self.run_agentbox("blocked", "work", "--json")[1])
        self.assertEqual({r["domain"]: (r["code"], r["params"]) for r in rows}, {
            "a.example": ("suspicious", {"signals": "exfil, raw-ip"}),
            "b.example": ("watchlist-unreadable", {}),
            "c.example": (None, {})})

    def test_reads_the_last_24_hours_of_the_vms_proxy(self):
        self.journal(JOURNAL)
        self.run_agentbox("blocked", "priv", "--json")
        self.assertEqual(self.journalctl_calls(), [proxy_log_call("priv")])

    def test_lines_the_proxy_did_not_write_are_left_out(self):
        self.journal(JOURNAL, forged="1793701250.000001 titan agentbox-proxy-work[4242]: [work] BLOCKED "
                                     "forged.example (not on the Allowlist)\n"
                                     "1793701251.000001 titan agentbox-proxy-work[4242]: [work] ALERT pypi.org [threat]\n")
        code, out, err = self.run_agentbox("blocked", "work", "--json")
        self.assertEqual((code, err), (0, UNTRUSTED.format(2)))
        rows = json.loads(out)
        self.assertEqual([(r["domain"], r["suspicious"]) for r in rows],
                         [("pypi.org", []), ("ten.internal", []), ("evil.example", ["exfil", "young"]), ("10.77.1.2", [])])

    def test_vm_names_with_hyphens(self):
        vm_records.write(self.root, vm_records.record("research-lab", 3))
        self.journal("1793701300.000001 titan agentbox-proxy-research-lab[812]: [research-lab] BLOCKED "
                     "x.example (not on the Allowlist)\n")
        code, out, _ = self.run_agentbox("blocked", "research-lab", "--json")
        self.assertEqual([e["domain"] for e in json.loads(out)], ["x.example"])

    def test_no_entries(self):
        self.assertEqual(self.run_agentbox("blocked", "work"), (0, "", ""))
        self.assertEqual(self.run_agentbox("blocked", "work", "--json"), (0, "[]\n", ""))

    def test_failing_journalctl_is_an_error(self):
        # the Bash version printed an empty result and exited with journalctl's code
        self.journal("", exit_code=1)
        code, out, err = self.run_agentbox("blocked", "work", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        failure = json.loads(err)
        self.assertEqual(failure["error"], "journal_failed")
        self.assertTrue(failure["message"].startswith("journalctl failed"), err)


class BlockReasonsTest(unittest.TestCase):
    """The codes of the proxy's reasons as the Control Room knows them (web/ui)."""

    def ui(self, *parts):
        with open(os.path.join(REPO, "web", "ui", *parts)) as f:
            return f.read()

    def test_the_ui_offers_nothing_to_allow_for_the_reasons_that_name_the_client(self):
        codes = {network.BLOCK_REASONS[r] for r in network.CLIENT_REASONS}
        code = self.ui("app", "utils", "access.ts")
        listed = re.search(r"^export const CLIENT_REASONS = \[([^\]]*)\]$", code, re.M).group(1)
        self.assertEqual(set(re.findall(r"'([a-z-]+)'", listed)), codes)
        with open(os.path.join(REPO, "web", "static", "app.js")) as f:
            legacy = re.search(r"^const CLIENT_REASONS = \[([^\]]*)\];$", f.read(), re.M).group(1)
        self.assertEqual(set(re.findall(r'"([a-z-]+)"', legacy)), codes)

    def test_every_code_of_a_block_reason_is_worded(self):
        for language in ("en", "de"):
            text = self.ui("i18n", "locales", language, "access.ts")
            block = text[text.index("    blocked: {"):]
            block = block[block.index("      reasons: {"):]
            worded = set(re.findall(r"^        '([a-z-]+)': ", block[:block.index("\n      },")], re.M))
            with self.subTest(language=language):
                self.assertEqual(set(network.BLOCK_REASONS.values()) | {"suspicious"}, worded)


class AlertsTest(NetworkTest):
    def test_text_keeps_the_signals_of_the_first_alert(self):
        self.journal(JOURNAL)
        code, out, err = self.run_agentbox("alerts", "work")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         "paste.example                               1x  last 2026-11-03 11:15  paste\n"
                         "evil.example                                2x  last 2026-11-03 11:10  exfil\n")

    def test_json_for_the_control_room(self):
        self.journal(JOURNAL)
        code, out, err = self.run_agentbox("alerts", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         '[{"domain": "paste.example", "tags": ["paste"], "count": 1, "first": 1793700900, '
                         '"last": 1793700900, "seen": false}, {"domain": "evil.example", "tags": ["exfil"], '
                         '"count": 2, "first": 1793700000, "last": 1793700600, "seen": false}]\n')
        self.assertEqual(self.journalctl_calls(), [proxy_log_call("work")])

    def test_only_the_vms_proxy_raises_alerts(self):
        # a local user's process, the proxy of another VM, root
        line = "1793701250.000001 titan agentbox-proxy-work[4242]: [work] ALERT forged.example [exfil]\n"
        with open(os.path.join(self.dir, "journal.txt"), "w") as f:
            f.write(as_json(JOURNAL) + "".join(as_json(line, **w) for w in (FORGED, proxy("priv"), CLI)))
        code, out, err = self.run_agentbox("alerts", "work", "--json")
        self.assertEqual((code, err), (0, UNTRUSTED.format(3)))
        self.assertEqual([a["domain"] for a in json.loads(out)], ["paste.example", "evil.example"])

    def test_no_entries(self):
        self.assertEqual(self.run_agentbox("alerts", "work", "--json"), (0, "[]\n", ""))


class OpenWebTest(NetworkTest):
    def setUp(self):
        super().setUp()
        # Open web only with a readable Watchlist (tests/test_lists.py)
        self.write_conf("watchlist.json", b'{"entries": []}\n')

    def test_rejects_minutes_out_of_range(self):
        for m in ("0", "241", "abc", "-5", "1.5", ""):
            with self.subTest(minutes=m):
                self.assertEqual(self.run_agentbox("open-web", "work", m), (1, "", BAD_MINUTES))
        self.assertEqual(self.run_agentbox("open-web", "work"), (1, "", BAD_MINUTES))
        self.assertEqual(self.conf_files(), [])

    def test_opens_for_the_given_minutes(self):
        before = int(time.time())
        code, out, err = self.run_agentbox("open-web", "work", "30")
        after = int(time.time())
        self.assertEqual((code, err), (0, ""))
        data = self.read_conf("open-work").decode()
        self.assertRegex(data, r"\A[0-9]+\n\Z")
        until = int(data)
        self.assertTrue(before + 1800 <= until <= after + 1800, (before, until, after))
        self.assertEqual(self.mode("open-work"), 0o644)
        # end time in local time (TZ is UTC+1)
        self.assertEqual(out, "work: Open web until " + time.strftime("%H:%M", time.gmtime(until + 3600)) + "\n")
        self.assertEqual(self.conf_files(), ["open-work"])

    def test_bounds_are_inclusive(self):
        self.assertEqual(self.run_agentbox("open-web", "work", "1")[0], 0)
        self.assertEqual(self.run_agentbox("open-web", "priv", "240")[0], 0)
        self.assertEqual(self.conf_files(), ["open-priv", "open-work"])

    def test_json(self):
        code, out, err = self.run_agentbox("open-web", "priv", "30", "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        until = int(self.read_conf("open-priv"))
        self.assertEqual(result, {"vm": "priv", "open_until": until,
                                  "message": "priv: Open web until " + time.strftime("%H:%M", time.gmtime(until + 3600))})


class CloseWebTest(NetworkTest):
    def test_closes(self):
        self.write_conf("open-work", b"4102444800\n")
        self.assertEqual(self.run_agentbox("close-web", "work"), (0, "work: Open web closed\n", ""))
        self.assertEqual(self.conf_files(), [])

    def test_already_closed(self):
        self.assertEqual(self.run_agentbox("close-web", "work"), (0, "work: Open web closed\n", ""))

    def test_json(self):
        self.write_conf("open-priv", b"4102444800\n")
        code, out, err = self.run_agentbox("close-web", "priv", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"vm": "priv", "message": "priv: Open web closed"})
        self.assertEqual(self.conf_files(), [])


class UnknownVmTest(NetworkTest):
    def test_every_command_rejects_it(self):
        self.write_conf("open-work", b"4102444800\n")
        for args in (["blocked", "dev"], ["alerts", "dev"], ["allowlist", "dev"],
                     ["allow", "dev", "example.org"], ["disallow", "dev", "example.org"],
                     ["open-web", "dev", "30"], ["close-web", "dev"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_agentbox(*args), (1, "", UNKNOWN_VM))
        # with --json, the same failure as one JSON object with its code
        code, out, err = self.run_agentbox("alerts", "dev", "--json")
        self.assertEqual((code, out, json.loads(err)), (1, "", {"error": "vm_not_found", "message": "No such VM: dev",
                                                                "params": {"vm": "dev"}}))
        for args in (["blocked"], ["close-web", ""]):
            with self.subTest(args=args):
                self.assertEqual(self.run_agentbox(*args), (1, "", NO_VM))
        self.assertEqual(self.recorded("journalctl.jsonl"), [])
        self.assertEqual(self.recorded("suspicious.jsonl"), [])
        self.assertEqual(self.conf_files(), ["open-work"])


if __name__ == "__main__":
    unittest.main()

