"""`agentbox logs` and `agentbox pair` against a fake `virsh` (see fake_virsh.py).

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(ROOT, "bin", "agentbox")
FAKE = os.path.join(ROOT, "tests", "fake_virsh.py")
WRAPPER_PATH = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-journal"
WRAPPER = os.path.join(ROOT, "vm", "files", WRAPPER_PATH.lstrip("/"))
CODE = "orca://pair?code=Ab3_x-9"
OLD = "orca://pair?code=old1"
# when Orca printed its codes in a boot of VM priv: at boot, and after revoke-pairings
AT_BOOT = {"__SEQNUM": "1599", "__SEQNUM_ID": "q1", "__MONOTONIC_TIMESTAMP": "12000000",
           "__REALTIME_TIMESTAMP": "1761722520000000"}
AT_REVOKE = {"__SEQNUM": "2011", "__SEQNUM_ID": "q1", "__MONOTONIC_TIMESTAMP": "40929000000",
             "__REALTIME_TIMESTAMP": "1761763440000000"}


def stamped(message, when, without=()):
    """entry() with the ordering fields of `when`, less those named in `without`."""
    e = entry(message, **when)
    for k in without:
        del e[k]
    return e


def entry(message, **fields):
    e = {
        "MESSAGE": message,
        "__REALTIME_TIMESTAMP": "1761732000123456",
        "__CURSOR": "s=1a;i=7;b=b1",
        "_BOOT_ID": "b1",
        "PRIORITY": "6",
        "SYSLOG_IDENTIFIER": "orca",
        "_SYSTEMD_UNIT": "agentbox-orca.service",
        "_HOSTNAME": "priv",
        "_PID": "42",
    }
    e.update(fields)
    return e


class AgentboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.root = os.path.join(self.dir, "root")
        vm_records.write(self.root)
        os.mkdir(os.path.join(self.dir, "bin"))
        virsh = os.path.join(self.dir, "bin", "virsh")
        with open(virsh, "w") as f:
            f.write(f'#!/bin/sh\nexec python3 "{FAKE}" "$@"\n')
        os.chmod(virsh, 0o755)
        self.scenario()

    def tearDown(self):
        self.tmp.cleanup()

    def scenario(self, **s):
        with open(os.path.join(self.dir, "scenario.json"), "w") as f:
            json.dump(s, f)

    def run_agentbox(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "FAKE_VIRSH_DIR": self.dir, "LANG": "C.UTF-8", "TZ": "UTC", "AGENTBOX_ROOT": self.root}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def run_agentbox_env(self, extra, *args):
        """Like run_agentbox, with extra environment (e.g. shorter limits) and a wall-clock budget."""
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "FAKE_VIRSH_DIR": self.dir, "LANG": "C.UTF-8", "TZ": "UTC", "AGENTBOX_ROOT": self.root, **extra}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=25)
        return r.returncode, r.stdout, r.stderr

    def calls(self, cmd=None):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                calls = [json.loads(line) for line in f]
        except FileNotFoundError:
            return []
        return [c for c in calls if cmd is None or c["cmd"] == cmd]

    def journalctl_args(self):
        """Arguments of the last journalctl run in the VM (through the wrapper in the Image)."""
        call = self.calls("guest-exec")[-1]
        self.assertEqual(call["path"], WRAPPER_PATH)
        return call["arg"]


class LogsTest(AgentboxTest):
    """#4: reading the journal through the guest agent."""

    def test_reads_current_boot_through_guest_agent(self):
        self.scenario(responses=[{"entries": [entry("Orca bereit")]}])
        code, out, _ = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 0)
        self.assertIn("priv orca[42]: Orca bereit", out)
        args = self.journalctl_args()
        self.assertIn("--boot=0", args)
        self.assertIn("--lines=200", args)
        self.assertIn("--output=json", args)
        self.assertEqual(self.calls("start"), [])

    def test_runs_only_the_journal_wrapper_in_the_vm(self):
        self.run_agentbox("logs", "priv")
        self.assertEqual({c["path"] for c in self.calls("guest-exec")}, {WRAPPER_PATH})

    def test_strips_control_characters(self):
        self.scenario(responses=[{"entries": [entry("\x1b[31mrot\x07 und\x9b2J \u202eweiter\nzweite Zeile")]}])
        code, out, _ = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 0)
        for bad in ("\x1b", "\x07", "\x9b", "\u202e"):
            self.assertNotIn(bad, out)
        # the color sequence is removed as a whole, not just its escape byte
        self.assertIn("rot und2J weiter\nzweite Zeile", out)
        self.assertNotIn("[31m", out)

    def test_hides_pairing_codes(self):
        self.scenario(responses=[{"entries": [entry(f"Pairing: {CODE}")]}])
        code, out, _ = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 0)
        self.assertNotIn("Ab3_x-9", out)
        self.assertIn("orca://pair?[hidden]", out)

    def test_hides_pairing_codes_in_any_case(self):
        self.scenario(responses=[{"entries": [entry("open ORCA://Pair?Code=Zz9yy here")]}])
        _, out, _ = self.run_agentbox("logs", "priv")
        self.assertNotIn("Zz9yy", out)

    def test_hides_url_encoded_pairing_codes(self):
        # Orca also prints the code inside its web client URL, percent-encoded
        msg = "Web client URL: http://192.0.2.8:6802/web-index.html#pairing=orca%3A%2F%2Fpair%3Fcode%3DZz9yy"
        self.scenario(responses=[{"entries": [entry(msg), entry("ORCA%3a%2f%2fPAIR%3fcode%3dQq1")]}])
        _, out, _ = self.run_agentbox("logs", "priv")
        self.assertNotIn("Zz9yy", out)
        self.assertNotIn("Qq1", out)

    def test_invisible_characters_cannot_fake_log_lines(self):
        # zero-width space, word joiner, line and paragraph separators
        self.scenario(responses=[{"entries": [entry("ok\u200b\u2060\u2028Nov 03 root: fake\u2029end")]}])
        _, out, _ = self.run_agentbox("logs", "priv")
        for ch in ("\u200b", "\u2060", "\u2028", "\u2029"):
            self.assertNotIn(ch, out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_colored_messages_stay_readable(self):
        msg = list(f"\u001b[31mfailed\u001b[0m to start".encode())
        self.scenario(responses=[{"entries": [entry(msg)]}])
        _, out, _ = self.run_agentbox("logs", "priv")
        self.assertIn("failed to start", out)
        self.assertNotIn("binary data", out)

    def test_killed_by_a_signal_is_reported_as_timeout(self):
        self.scenario(responses=[{"signal": 9}])
        code, _, err = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 5)
        self.assertNotIn("None", err)

    def test_vm_not_running(self):
        self.scenario(state="shut off")
        code, _, err = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 3)
        self.assertIn("is not running", err)
        self.assertEqual(self.calls("guest-exec"), [])

    def test_guest_agent_not_answering(self):
        self.scenario(agent_down=True)
        code, _, err = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 4)
        self.assertIn("the guest agent does not answer", err)

    def test_query_timed_out_in_vm(self):
        self.scenario(responses=[{"stdout": "", "exitcode": 124}])
        code, _, err = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 5)
        self.assertEqual(err, "agentbox: priv: query in the VM stopped after 20 s, narrow the filters\n")

    def test_journalctl_failing_in_vm(self):
        self.scenario(responses=[{"stdout": "", "exitcode": 1, "stderr": "Failed to open\x1b[2J journal"}])
        code, _, err = self.run_agentbox("logs", "priv")
        self.assertEqual(code, 6)
        self.assertIn("Failed to open[2J journal", err)

    def test_search_without_match_is_not_an_error(self):
        self.scenario(responses=[{"stdout": "", "exitcode": 1}])
        code, out, _ = self.run_agentbox("logs", "priv", "--grep", "nichts")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_hanging_guest_agent_is_given_up_on_host(self):
        self.scenario(agent_hangs=30)
        code, _, err = self.run_agentbox_env({"AGENTBOX_VIRSH_TIMEOUT": "2"}, "logs", "priv")
        self.assertEqual(code, 4)
        self.assertIn("guest agent", err)

    def test_rejects_extra_arguments(self):
        for args in (["-f"], ["extra"], ["--follow"], ["-u", "x"]):
            code, _, err = self.run_agentbox("logs", "priv", *args)
            self.assertEqual(code, 2, args)
            self.assertIn("unknown option", err)
        self.assertEqual(self.calls(), [])

    def test_unknown_vm(self):
        code, _, _ = self.run_agentbox("logs", "nope")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.calls(), [])


@unittest.skipIf(os.geteuid() == 0, "checks what happens without root")
class RootTest(AgentboxTest):
    """Commands other than logs and pair need root; without it agentbox says so at once.
    Runs without a test root: under one, agentbox never asks for root."""

    def run_agentbox(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "FAKE_VIRSH_DIR": self.dir, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def test_observe_without_root_asks_for_sudo(self):
        code, out, err = self.run_agentbox("observe", "priv")
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("sudo agentbox observe priv", err)
        self.assertEqual(self.calls(), [])

    def test_reset_without_root_asks_for_sudo(self):
        code, _, err = self.run_agentbox("reset", "priv")
        self.assertNotEqual(code, 0)
        self.assertIn("sudo agentbox reset priv", err)

    def test_logs_and_pair_do_not_ask_for_root(self):
        # Without a test root they would read the Host's registry and record in the
        # Host's journal. An invalid --actor is refused right after the root check,
        # so the command ends there: a refusal of the actor means it passed.
        for cmd in ("logs", "pair"):
            code, out, err = self.run_agentbox("--actor=", cmd, "priv")
            self.assertEqual((code, out), (2, ""))
            self.assertIn("--actor", err)
            self.assertNotIn("root", err)
        self.assertEqual(self.calls(), [])


class WrapperTest(unittest.TestCase):
    """The program the guest agent runs in the VM (part of the Image)."""

    def test_ignores_fsfreeze_calls(self):
        for arg in ("freeze", "thaw"):
            r = subprocess.run([WRAPPER, arg], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_runs_journalctl(self):
        r = subprocess.run([WRAPPER, "--version"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("systemd", r.stdout)

    @unittest.skipUnless(shutil.which("matchpathcon"), "needs SELinux tools")
    def test_lies_where_selinux_lets_the_guest_agent_run_it(self):
        r = subprocess.run(["matchpathcon", WRAPPER_PATH], capture_output=True, text=True)
        self.assertIn("virt_qemu_ga_unconfined_exec_t", r.stdout)


class PairTest(AgentboxTest):
    """#5: the Pairing code through the guest agent."""

    def test_returns_code(self):
        self.scenario(responses=[{"entries": [entry(f"Pairing: {CODE}")]}])
        code, out, _ = self.run_agentbox("pair", "priv")
        self.assertEqual(code, 0)
        self.assertEqual(out, CODE + "\n")
        args = self.journalctl_args()
        self.assertIn("--unit=agentbox-orca.service", args)
        self.assertIn("--boot=0", args)

    def test_returns_newest_code(self):
        self.scenario(responses=[{"entries": [entry(f"Pairing: {OLD}"), entry(f"Pairing: {CODE}")]}])
        _, out, _ = self.run_agentbox("pair", "priv")
        self.assertEqual(out, CODE + "\n")

    def test_returns_newest_code_when_journalctl_writes_it_first(self):
        # journalctl --grep --lines in the VM writes the newest match first: after
        # revoke-pairings `pair` returned the code of Orca's old identity
        self.scenario(responses=[{"entries": [stamped(f"Pairing: {CODE}", AT_REVOKE),
                                              stamped(f"Pairing: {OLD}", AT_BOOT)]}])
        code, out, _ = self.run_agentbox("pair", "priv")
        self.assertEqual(code, 0)
        self.assertEqual(out, CODE + "\n")

    def test_newest_code_of_a_boot_in_either_order(self):
        """By __SEQNUM, else by __MONOTONIC_TIMESTAMP, else by the wall-clock time;
        never by the order journalctl wrote the records in."""
        for without in ((), ("__SEQNUM",), ("__SEQNUM", "__MONOTONIC_TIMESTAMP")):
            old, new = stamped(f"Pairing: {OLD}", AT_BOOT, without), stamped(f"Pairing: {CODE}", AT_REVOKE, without)
            for written in ([old, new], [new, old]):
                with self.subTest(without=without, newest_first=written[0] is new):
                    self.scenario(responses=[{"entries": written}])
                    _, out, _ = self.run_agentbox("pair", "priv")
                    self.assertEqual(out, CODE + "\n")

    def test_journal_order_wins_over_a_clock_set_back(self):
        # the VM's clock went back between the codes (NTP after boot)
        set_back = {**AT_REVOKE, "__REALTIME_TIMESTAMP": "1761700000000000"}
        for without in ((), ("__SEQNUM",)):
            old, new = stamped(f"Pairing: {OLD}", AT_BOOT, without), stamped(f"Pairing: {CODE}", set_back, without)
            for written in ([old, new], [new, old]):
                with self.subTest(without=without, newest_first=written[0] is new):
                    self.scenario(responses=[{"entries": written}])
                    _, out, _ = self.run_agentbox("pair", "priv")
                    self.assertEqual(out, CODE + "\n")

    def test_ignores_lookalikes(self):
        self.scenario(responses=[{"entries": [
            entry(f"Pairing: {CODE}"),
            entry("orca://pair?token=abc orca://pair?code= orca://pairing?code=x"),
        ]}])
        _, out, _ = self.run_agentbox("pair", "priv")
        self.assertEqual(out, CODE + "\n")

    def test_retries_until_orca_printed_a_code(self):
        self.scenario(responses=[{"stdout": "", "exitcode": 1}, {"entries": [entry(f"Pairing: {CODE}")]}])
        code, out, _ = self.run_agentbox("pair", "priv")
        self.assertEqual(code, 0)
        self.assertEqual(out, CODE + "\n")
        self.assertEqual(len(self.calls("guest-exec")), 2)

    def test_gives_up_after_its_time_budget(self):
        # every attempt is slow (guest agent takes 2 s) and never finds a code
        self.scenario(agent_hangs=2, responses=[{"stdout": "", "exitcode": 1}])
        start = time.monotonic()
        code, out, err = self.run_agentbox_env({"AGENTBOX_PAIR_SECONDS": "6"}, "pair", "priv")
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("no Pairing code", err)
        self.assertLess(time.monotonic() - start, 15)

    def test_says_when_the_guest_agent_never_answered(self):
        self.scenario(agent_down=True)
        code, _, err = self.run_agentbox_env({"AGENTBOX_PAIR_SECONDS": "2"}, "pair", "priv")
        self.assertNotEqual(code, 0)
        self.assertIn("guest agent", err)

    def test_finds_a_colored_code(self):
        # journalctl hands messages with control characters over as a list of bytes
        msg = list(f"\u001b[32mPairing: {CODE}\u001b[0m".encode())
        self.scenario(responses=[{"entries": [entry(msg)]}])
        code, out, _ = self.run_agentbox("pair", "priv")
        self.assertEqual(code, 0)
        self.assertEqual(out, CODE + "\n")

    def test_starts_a_stopped_vm(self):
        self.scenario(state="shut off", responses=[{"entries": [entry(f"Pairing: {CODE}")]}])
        code, out, _ = self.run_agentbox("pair", "priv")
        self.assertEqual(code, 0)
        self.assertEqual(out, CODE + "\n")
        self.assertEqual(len(self.calls("start")), 1)


class FilterTest(AgentboxTest):
    """#6: fixed, validated filters."""

    def test_filters_reach_journalctl_as_option_value_pairs(self):
        code, _, _ = self.run_agentbox(
            "logs", "priv", "--unit", "podman.service", "--priority", "err", "--since", "-1h",
            "--until", "2026-10-29 10:00", "--grep", "timeout", "--lines", "50")
        self.assertEqual(code, 0)
        args = self.journalctl_args()
        for a in ("--unit=podman.service", "--priority=err", "--since=-1h", "--until=2026-10-29 10:00:00 UTC",
                  "--grep=timeout", "--lines=50", "--boot=0"):
            self.assertIn(a, args)

    def test_equals_form_is_accepted(self):
        code, _, _ = self.run_agentbox("logs", "priv", "--unit=sshd.service", "--lines=5")
        self.assertEqual(code, 0)
        self.assertIn("--unit=sshd.service", self.journalctl_args())

    def test_local_times_are_sent_as_utc(self):
        self.run_agentbox_env({"TZ": "Europe/Berlin"}, "logs", "priv", "--since", "2026-11-03 10:00",
                              "--until", "2026-11-03T11:30:15")
        args = self.journalctl_args()
        self.assertIn("--since=2026-11-03 09:00:00 UTC", args)
        self.assertIn("--until=2026-11-03 10:30:15 UTC", args)

    def test_relative_times_pass_unchanged(self):
        self.run_agentbox("logs", "priv", "--since", "-2h")
        self.assertIn("--since=-2h", self.journalctl_args())

    def test_all_boots(self):
        self.run_agentbox("logs", "priv", "--boot", "all")
        self.assertFalse([a for a in self.journalctl_args() if a.startswith("--boot")])

    def test_search_starting_with_dash_stays_a_value(self):
        code, _, _ = self.run_agentbox("logs", "priv", "--grep", "-x")
        self.assertEqual(code, 0)
        self.assertIn("--grep=-x", self.journalctl_args())

    def test_rejects_invalid_values(self):
        cases = [
            ["--unit", "a b"], ["--unit", "-x"], ["--priority", "9"], ["--priority", "loud"],
            ["--since", "next week"], ["--until", "1h"], ["--boot", "previous"],
            ["--lines", "0"], ["--lines", "5001"], ["--lines", "abc"],
            ["--grep", "x" * 201], ["--grep", "a\x1bb"], ["--after-cursor", "s=1 2"], ["--unit"],
        ]
        for args in cases:
            code, _, err = self.run_agentbox("logs", "priv", *args)
            self.assertEqual(code, 2, args)
            self.assertTrue("invalid value" in err or "needs a value" in err, (args, err))
        self.assertEqual(self.calls(), [])


class JsonTest(AgentboxTest):
    """#7: JSON output and cursor."""

    def logs_json(self, *args):
        code, out, err = self.run_agentbox("logs", "priv", "--json", *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_contract(self):
        self.scenario(responses=[{"entries": [
            entry("eins", __CURSOR="c1"),
            entry("zwei", __CURSOR="c2", PRIORITY="3", _SYSTEMD_UNIT=None, _SYSTEMD_USER_UNIT="app.service"),
        ]}])
        data = self.logs_json()
        self.assertEqual(data["vm"], "priv")
        self.assertEqual(data["cursor"], "c2")
        self.assertFalse(data["truncated"])
        first, second = data["entries"]
        self.assertEqual(first, {"time": "2025-10-29T10:00:00.123456Z", "unit": "agentbox-orca.service",
                                 "identifier": "orca", "priority": 6, "message": "eins", "boot": "b1",
                                 "cursor": "c1"})
        self.assertEqual(second["unit"], "app.service")
        self.assertEqual(second["priority"], 3)

    def test_empty(self):
        data = self.logs_json()
        self.assertEqual(data["entries"], [])
        self.assertIsNone(data["cursor"])

    def test_binary_and_long_messages(self):
        self.scenario(responses=[{"entries": [entry([104, 105, 0, 255]), entry("x" * 9000)]}])
        binary, long = self.logs_json()["entries"]
        self.assertEqual(binary["message"], "[binary data]")
        self.assertLess(len(long["message"]), 8300)
        self.assertTrue(long["message"].endswith("[truncated]"))

    def test_every_field_is_length_limited(self):
        self.scenario(responses=[{"entries": [entry("m", _SYSTEMD_UNIT="u" * 5000, SYSLOG_IDENTIFIER="i" * 5000,
                                                    _BOOT_ID="b" * 5000)]}])
        e = self.logs_json()["entries"][0]
        for key in ("unit", "identifier", "boot"):
            self.assertLess(len(e[key]), 600, key)

    def test_hostile_output_stays_valid(self):
        stdout = ("not json\n" + json.dumps(entry("a\ud800b\x1b")) + "\n" + "[" * 100000 + "\n"
                  + json.dumps(entry("ok", SYSLOG_IDENTIFIER=f"x\x1b{CODE}")) + "\n{\"MESSAGE\": ")
        self.scenario(responses=[{"stdout": stdout}])
        entries = self.logs_json()["entries"]
        self.assertEqual([e["message"] for e in entries], ["ab", "ok"])
        self.assertEqual(entries[1]["identifier"], "xorca://pair?[hidden]")

    def test_truncation_is_reported(self):
        self.scenario(responses=[{"entries": [entry("eins")], "truncated": True}])
        self.assertTrue(self.logs_json()["truncated"])
        _, _, err = self.run_agentbox("logs", "priv")
        self.assertIn("output truncated", err)

    def test_after_cursor(self):
        self.scenario(responses=[{"entries": [entry(str(i), __CURSOR=f"c{i}") for i in range(5)]}])
        data = self.logs_json("--after-cursor", "s=1a;i=7;b=b1", "--lines", "3")
        args = self.journalctl_args()
        self.assertIn("--after-cursor=s=1a;i=7;b=b1", args)
        self.assertFalse([a for a in args if a.startswith("--lines")])
        self.assertEqual([e["message"] for e in data["entries"]], ["0", "1", "2"])
        self.assertEqual(data["cursor"], "c2")
        self.assertTrue(data["truncated"])

    def test_search_results_oldest_first_with_the_cursor_of_the_newest(self):
        # journalctl --grep --lines in the VM writes the newest match first; the
        # Control Room's Follow asks for entries after `cursor`
        self.scenario(responses=[{"entries": [stamped("neu", {**AT_REVOKE, "__CURSOR": "c2"}),
                                              stamped("alt", {**AT_BOOT, "__CURSOR": "c1"})]}])
        data = self.logs_json("--grep", "Orca")
        self.assertEqual([e["message"] for e in data["entries"]], ["alt", "neu"])
        self.assertEqual(data["cursor"], "c2")
        _, out, _ = self.run_agentbox("logs", "priv", "--grep", "Orca")
        self.assertLess(out.index(": alt"), out.index(": neu"))

    def test_numbers_of_other_sequences_and_boots_are_not_compared(self):
        # a sequence number counts within its __SEQNUM_ID, time since boot within its boot
        early = {"__SEQNUM": "900", "__SEQNUM_ID": "q1", "__MONOTONIC_TIMESTAMP": "40929000000",
                 "__REALTIME_TIMESTAMP": "1761722520000000", "_BOOT_ID": "b1"}
        cases = {"one boot": {"__SEQNUM": "5", "__SEQNUM_ID": "q2", "__MONOTONIC_TIMESTAMP": "40930000000",
                              "__REALTIME_TIMESTAMP": "1761700000000000", "_BOOT_ID": "b1"},
                 "two boots": {"__SEQNUM": "5", "__SEQNUM_ID": "q2", "__MONOTONIC_TIMESTAMP": "12000000",
                               "__REALTIME_TIMESTAMP": "1761763440000000", "_BOOT_ID": "b2"}}
        for case, late in cases.items():
            for written in ([stamped("frueh", early), stamped("spaet", late)],
                            [stamped("spaet", late), stamped("frueh", early)]):
                with self.subTest(case, written_first=written[0]["MESSAGE"]):
                    self.scenario(responses=[{"entries": written}])
                    data = self.logs_json("--boot", "all")
                    self.assertEqual([e["message"] for e in data["entries"]], ["frueh", "spaet"])

    def test_sequence_number_first_and_only_where_every_record_has_one(self):
        # time since boot and wall-clock time say the opposite of the field that should order
        later_clock, earlier_clock = "1761763440000000", "1761722520000000"
        cases = [("sequence number first",
                  {"__SEQNUM": "1", "__MONOTONIC_TIMESTAMP": "2000", "__REALTIME_TIMESTAMP": later_clock},
                  {"__SEQNUM": "2", "__MONOTONIC_TIMESTAMP": "1000", "__REALTIME_TIMESTAMP": earlier_clock})]
        # one record without a usable sequence number: time since boot orders
        for seqnum in ("missing", ["2"], 2, None, "1" * 21, "٢"):
            late = {"__MONOTONIC_TIMESTAMP": "2000", "__REALTIME_TIMESTAMP": earlier_clock}
            if seqnum != "missing":
                late["__SEQNUM"] = seqnum
            cases.append((f"__SEQNUM {seqnum!r}",
                          {"__SEQNUM": "9", "__MONOTONIC_TIMESTAMP": "1000", "__REALTIME_TIMESTAMP": later_clock}, late))
        for case, early, late in cases:
            for written in ([stamped("frueh", early), stamped("spaet", late)],
                            [stamped("spaet", late), stamped("frueh", early)]):
                with self.subTest(case, written_first=written[0]["MESSAGE"]):
                    self.scenario(responses=[{"entries": written}])
                    data = self.logs_json()
                    self.assertEqual([e["message"] for e in data["entries"]], ["frueh", "spaet"])


if __name__ == "__main__":
    unittest.main()
