"""#19: Paired devices of a VM (`agentbox pairings`, `unpair`, `revoke-pairings`) and the
Control Room's pair action, against fake virsh (guest agent) and a fake Orca CLI.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_activity  # noqa: E402
from test_activity import ActivityTest, vm_journal  # noqa: E402
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest  # noqa: E402

setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-devices"
OLD = "orca://pair?code=OldOld1"
NEW = "orca://pair?code=NewNew2"
LISTING = {"devices": [
    {"id": "obs-1", "name": "titan", "scope": "runtime", "paired_ms": 1761732000000, "last_seen_ms": 1761735600000},
    {"id": "phone-1", "name": "Pixel", "scope": "mobile", "paired_ms": 1761645600000, "last_seen_ms": 0},
]}
REVOKED = {"removed": ["orca-devices.json", "orca-e2ee-keypair.json"], "started": True}


def lines(*messages):
    return "".join(json.dumps(vm_journal(m)) + "\n" for m in messages)


class PairingsTest(ActivityTest):
    def setUp(self):
        super().setUp()
        self.observer = self.path("var/lib/agentbox/observer-work")

    def make_observer(self, device="obs-1"):
        os.makedirs(self.observer)
        with open(os.path.join(self.observer, "orca-environments.json"), "w") as f:
            json.dump({"environments": [{"id": "e1", "name": "work", "pairedDeviceId": device}]}, f)

    def all_output(self, *args):
        code, out, err = self.run_agentbox(*args)
        return code, out + err

    def wrapper_calls(self):
        return [c["arg"] for c in self.calls("guest-exec") if c["path"] == DEVICES]

    def test_lists_devices_with_times_and_marks_the_observer(self):
        self.make_observer()
        self.scenario(responses=[{"stdout": json.dumps(LISTING)}])
        code, out, _ = self.run_agentbox("pairings", "work", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(self.wrapper_calls(), [["list"]])
        by = {d["id"]: d for d in data["devices"]}
        self.assertTrue(by["obs-1"]["observer"])
        self.assertFalse(by["obs-1"]["removable"])
        self.assertEqual(by["obs-1"]["paired_at"], "2025-10-29T10:00:00Z")
        self.assertEqual(by["obs-1"]["last_seen_at"], "2025-10-29T11:00:00Z")
        self.assertFalse(by["phone-1"]["observer"])
        self.assertIsNone(by["phone-1"]["last_seen_at"])
        self.assertFalse(data["single_removal"])
        self.assertTrue(data["observer_known"])

    def test_text_listing_marks_the_observer(self):
        self.make_observer()
        self.scenario(responses=[{"stdout": json.dumps(LISTING)}])
        code, out, _ = self.run_agentbox("pairings", "work")
        self.assertEqual(code, 0)
        obs = next(l for l in out.splitlines() if "obs-1" in l)
        self.assertIn("Observer", obs)
        self.assertNotIn("Observer", next(l for l in out.splitlines() if "phone-1" in l))

    def test_without_observer_nobody_is_marked(self):
        self.scenario(responses=[{"stdout": json.dumps(LISTING)}])
        data = json.loads(self.run_agentbox("pairings", "work", "--json")[1])
        self.assertFalse(data["observer_known"])
        self.assertFalse(any(d["observer"] for d in data["devices"]))

    def test_invalid_input_and_broken_answers(self):
        self.assertEqual(self.run_agentbox("pairings", "nope")[0], 1)
        self.assertEqual(self.run_agentbox("pairings", "work", "--bogus")[0], 2)
        self.scenario(responses=[{"stdout": "kein JSON"}])
        self.assertEqual(self.run_agentbox("pairings", "work")[0], 6)

    def test_listing_is_not_recorded_as_activity(self):
        self.scenario(responses=[{"stdout": json.dumps(LISTING)}])
        self.run_agentbox("pairings", "work")
        self.assertEqual(self.entries(), [])

    def test_the_observer_cannot_be_removed(self):
        self.make_observer()
        self.scenario(responses=[{"stdout": json.dumps(LISTING)}])
        code, out = self.all_output("unpair", "work", "obs-1")
        self.assertEqual(code, 2)
        self.assertIn("Observer", out)

    def test_single_removal_is_refused_because_orca_cannot_do_it(self):
        self.make_observer()
        self.scenario(responses=[{"stdout": json.dumps(LISTING)}])
        code, out = self.all_output("unpair", "work", "phone-1")
        self.assertEqual(code, 2)
        self.assertIn("revoke-pairings work --confirm work", out)
        self.assertEqual(self.wrapper_calls(), [["list"]])         # nothing was changed in the VM
        self.assertEqual(self.run_agentbox("unpair", "work", "../x")[0], 2)

    def test_revoke_needs_the_confirmation(self):
        # a second --confirm does not override the first (agentbox/options.py)
        for extra in ([], ["--confirm", "priv"], ["--confirm=WORK"], ["--confirm", "priv", "--confirm", "work"]):
            with self.subTest(extra=extra):
                self.assertEqual(self.run_agentbox("revoke-pairings", "work", *extra)[0], 2)
        self.assertEqual(self.calls("guest-exec"), [])
        self.assertEqual(self.calls("orca"), [])

    def test_revoke_all_repairs_the_observer_with_the_new_code(self):
        self.make_observer()
        self.scenario(responses=[
            {"stdout": lines(f"Pairing: {OLD}")},
            {"stdout": json.dumps(REVOKED)},
            {"stdout": lines(f"Pairing: {OLD}")},                     # Orca has not printed the new code yet
            {"stdout": lines(f"Pairing: {OLD}", f"Pairing: {NEW}")},
        ])
        code, out, err = self.run_agentbox("revoke-pairings", "work", "--confirm", "work", "--json",
                                           AGENTBOX_PAIR_SECONDS="30")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["observer_repaired"], True)
        self.assertEqual(self.wrapper_calls(), [["revoke-all"]])
        adds = [c["argv"] for c in self.calls("orca") if c["argv"][:2] == ["environment", "add"]]
        self.assertEqual(adds, [["environment", "add", "--name", "work", "--pairing-code", NEW]])
        self.assertNotIn("NewNew2", out + err)
        self.assertNotIn("OldOld1", out + err)

    def test_revoke_all_without_observer_pairs_nobody(self):
        self.scenario(responses=[{"stdout": lines(f"Pairing: {OLD}")},
                                 {"stdout": json.dumps({"removed": ["orca-devices.json"], "started": True})}])
        code, out, _ = self.run_agentbox("revoke-pairings", "work", "--confirm=work")
        self.assertEqual(code, 0)
        self.assertIn("every device must pair again", out)
        self.assertEqual(self.calls("orca"), [])

    def test_revoke_all_fails_when_the_vm_cannot(self):
        self.scenario(responses=[{"stdout": lines(f"Pairing: {OLD}")}, {"exitcode": 1, "stderr": "boom"}])
        self.assertEqual(self.run_agentbox("revoke-pairings", "work", "--confirm=work")[0], 6)

    def test_pairing_codes_never_reach_activity(self):
        self.make_observer()
        self.scenario(responses=[
            {"stdout": lines(f"Pairing: {OLD}")},
            {"stdout": json.dumps(REVOKED)},
            {"stdout": lines(f"Pairing: {NEW}")},
        ])
        self.assertEqual(self.run_agentbox("revoke-pairings", "work", "--confirm=work")[0], 0)
        self.run_agentbox("unpair", "work", "phone-1")
        self.run_agentbox("pair", "work")
        raw = self.raw()
        for secret in (b"NewNew2", b"OldOld1"):
            self.assertNotIn(secret, raw)
        entries = self.entries()
        self.assertTrue(entries)
        self.assertTrue(all(e["AGENTBOX_KIND"] == "pairing" for e in entries))
        self.assertEqual(entries[0]["AGENTBOX_ACTION"], "revoke-pairings")

    def test_wrapper_hides_tokens_and_offers(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(REPO, "vm", "files", DEVICES.lstrip("/"))) as f:
                src = f.read()
            line = 'DATA = "/var/home/agent/.config/orca"'
            self.assertIn(line, src)
            src = src.replace(line, f"DATA = {d!r}")
            wrapper = os.path.join(d, "w.py")
            with open(wrapper, "w") as f:
                f.write(src)
            with open(os.path.join(d, "orca-devices.json"), "w") as f:
                json.dump([{"deviceId": "a", "name": "n", "scope": "mobile", "pairedAt": 5, "lastSeenAt": 7,
                            "token": "SECRETTOKEN"},
                           {"deviceId": "pending", "pairedAt": 5, "lastSeenAt": 0, "token": "T2"},
                           {"deviceId": "k", "scope": "runtime", "pairedAt": 5, "lastSeenAt": 0,
                            "credentialKind": "device-key", "devicePublicKeyB64": "KEY"}], f)
            r = subprocess.run([sys.executable, wrapper, "list"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("SECRETTOKEN", r.stdout)
            self.assertNotIn("KEY", r.stdout)
            self.assertEqual([x["id"] for x in json.loads(r.stdout)["devices"]], ["a", "k"])
            self.assertEqual(subprocess.run([sys.executable, wrapper, "rm", "-rf"], capture_output=True).returncode, 2)


class PairActionTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()
        self.respond({"vm": "work", "code": "orca://pair?code=SECRET"})

    def test_pair_needs_an_explicit_confirmation(self):
        for body in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, {"confirm": None}):
            with self.subTest(body=body):
                self.assertError(self.request("POST", "/api/v1/vms/work/pairing-code", body), 400,
                                 "confirmation_required")
        self.assertEqual(self.calls(), [])

    def test_confirmed_pair_returns_the_code_but_not_to_the_journal(self):
        status, obj, _ = self.request("POST", "/api/v1/vms/work/pairing-code", {"confirm": True})
        self.assertEqual((status, obj), (200, {"vm": "work", "code": "orca://pair?code=SECRET"}))
        self.assertEqual(self.calls(), [["pair", "work", "--json"]])
        self.assertNotIn("SECRET", self.server_log())

    def test_pairings_and_revoke(self):
        self.respond({"vm": "work", "devices": [], "observer_known": False, "single_removal": False})
        status, obj = self.get("/api/v1/vms/work/pairings")
        self.assertEqual((status, obj["vm"]), (200, "work"))
        self.assertEqual(self.calls(), [["pairings", "work", "--json"]])
        for body in ({}, {"confirm": "priv"}, {"confirm": True}):
            with self.subTest(body=body):
                self.assertError(self.request("POST", "/api/v1/vms/work/pairings/revoke", body), 400,
                                 "confirmation_required")
        self.assertEqual(len(self.calls()), 1)
        self.respond({"vm": "work", "observer_repaired": True, "message": "ok"})
        self.assertEqual(self.request("POST", "/api/v1/vms/work/pairings/revoke", {"confirm": "work"})[0], 200)
        self.assertEqual(self.calls()[-1], ["revoke-pairings", "work", "--confirm=work", "--json"])


if __name__ == "__main__":
    unittest.main()
