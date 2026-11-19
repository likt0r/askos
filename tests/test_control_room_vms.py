"""Control Room backend (`web/agentbox-web`): creating and deleting a VM and its SSH access,
against a fake `agentbox` (see fake_agentbox.py), with the server harness of
test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

# the throwaway certificate and password of the harness
setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

CREATED = {"vm": "lab", "network": 3, "orca_port": 6803, "image": "base", "memory_mib": 8192, "data_disk_gib": 50,
           "github_owners": ["likt0r"], "allowlist_from": "priv", "message": "lab angelegt."}
DELETED = {"vm": "lab", "data_disk": "archived", "archive": "/var/lib/agentbox/archive/lab-data-20261103T120000Z.qcow2",
           "resumed": False, "message": "lab geloescht"}


class CreateTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()
        self.respond(CREATED)

    def create(self, body, name="lab"):
        return self.request("POST", "/api/v1/vms", {"name": name, **body})[:2]

    def test_fields_map_to_options(self):
        status, obj = self.create({"image": "base", "memory_mib": 8192, "data_disk_gib": "50",
                                   "github_owners": ["likt0r", " acme-corp ", ""], "copy_allowlist_from": "priv"})
        self.assertEqual((status, obj), (201, CREATED))
        self.assertEqual(self.calls(), [["create", "lab", "--image=base", "--memory-mib=8192", "--data-disk-gib=50",
                                         "--copy-allowlist-from=priv", "--owner=likt0r", "--owner=acme-corp",
                                         "--json"]])

    def test_without_fields_agentbox_uses_its_defaults(self):
        for body in ({}, {"image": None, "memory_mib": "", "github_owners": None, "copy_allowlist_from": None}):
            with self.subTest(body=body):
                self.assertEqual(self.create(body)[0], 201)
                self.assertEqual(self.calls()[-1], ["create", "lab", "--json"])

    def test_values_stay_values(self):
        self.create({"image": "--delete-data-disk", "copy_allowlist_from": "priv --json"})
        self.assertEqual(self.calls(), [["create", "lab", "--image=--delete-data-disk",
                                         "--copy-allowlist-from=priv --json", "--json"]])

    def test_odd_values_are_rejected_not_converted(self):
        for body in ({"memory_mib": True}, {"data_disk_gib": 5.5}, {"image": ["base"]}, {"github_owners": "likt0r"},
                     {"github_owners": [1]}, {"github_owners": ["-x"]}, {"copy_allowlist_from": {"vm": "priv"}}):
            with self.subTest(body=body):
                self.assertError(self.request("POST", "/api/v1/vms", {"name": "lab", **body}), 400, "invalid_field")
        self.assertEqual(self.calls(), [])

    def test_refusal_of_agentbox_is_passed_on(self):
        self.respond("", stderr=failure("name_reserved", "the name status is reserved", name="status"), exitcode=1)
        answer = self.request("POST", "/api/v1/vms", {"name": "status"})
        self.assertError(answer, 400, "name_reserved")
        self.assertEqual(answer[1]["error"]["detail"], "the name status is reserved")
        self.assertEqual(self.calls(), [["create", "status", "--json"]])
        self.assertIn("v1 POST post_vms -> FAILED name_reserved", self.server_log())

    def test_ssh_keys_are_copied_only_from_a_vm_name(self):
        """#136: copy_ssh_keys_from goes only as the value of --copy-ssh-keys-from, and only a VM
        name: anything else is no VM before agentbox runs."""
        self.assertEqual(self.create({"copy_ssh_keys_from": " priv "})[0], 201)
        self.assertEqual(self.calls(), [["create", "lab", "--copy-ssh-keys-from=priv", "--json"]])
        for odd in ("priv --json", "--mine", "Priv", "../priv", "a" * 17, 5):
            with self.subTest(copy_ssh_keys_from=odd):
                answer = self.request("POST", "/api/v1/vms", {"name": "lab", "copy_ssh_keys_from": odd})
                self.assertError(answer, 404, "vm_not_found")
                self.assertEqual(answer[1]["error"]["field"], "copy_ssh_keys_from")
        for odd in ({"vm": "priv"}, ["priv"], True):
            with self.subTest(copy_ssh_keys_from=odd):
                answer = self.request("POST", "/api/v1/vms", {"name": "lab", "copy_ssh_keys_from": odd})
                self.assertError(answer, 400, "invalid_field")
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_names_never_reach_agentbox(self):
        for name in ("Lab", "x", "a" * 17, "--all", "..", 5):
            with self.subTest(name=name):
                answer = self.request("POST", "/api/v1/vms", {"name": name})
                self.assertEqual(answer[0], 400)
                self.assertIn(answer[1]["error"]["code"], ("invalid_name", "invalid_field"))
        self.assertEqual(self.calls(), [])


class DeleteTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()
        self.respond(DELETED)

    def delete(self, body, name="lab"):
        return self.request("DELETE", f"/api/v1/vms/{name}", body)

    def test_requires_the_typed_name(self):
        for body in ({}, {"confirm": "priv"}, {"confirm": "LAB"}, {"confirm": " lab"}, {"confirm": None},
                     {"confirm": ["lab"]}):
            with self.subTest(body=body):
                self.assertError(self.delete(body), 400, "confirmation_required")
        self.assertEqual(self.calls(), [])

    def test_archives_the_data_disk_by_default(self):
        self.assertEqual(self.delete({"confirm": "lab"})[:2], (200, DELETED))
        self.assertEqual(self.calls(), [["delete", "lab", "--confirm=lab", "--json"]])
        self.assertIn("lab v1 DELETE delete_vm -> ok", self.server_log())

    def test_deletes_the_data_disk_only_when_asked(self):
        self.delete({"confirm": "lab", "delete_data_disk": False})
        self.delete({"confirm": "lab", "delete_data_disk": True})
        self.assertEqual(self.calls(), [["delete", "lab", "--confirm=lab", "--json"],
                                        ["delete", "lab", "--confirm=lab", "--delete-data-disk", "--json"]])
        for odd in ("true", 1, "yes", None):
            with self.subTest(delete_data_disk=odd):
                self.assertError(self.delete({"confirm": "lab", "delete_data_disk": odd}), 400, "invalid_field")
        self.assertEqual(len(self.calls()), 2)

    def test_failure_is_reported(self):
        self.respond("", stderr=failure("vm_not_found", "No such VM: lab", vm="lab"), exitcode=1)
        self.assertError(self.delete({"confirm": "lab"}), 404, "vm_not_found")


def rsa_line(bits, comment="laptop"):
    """An ssh-rsa public key line of this size (of the right form, not a real key)."""
    def string(data):
        return len(data).to_bytes(4, "big") + data
    blob = string(b"ssh-rsa") + string(b"\x01\x00\x01") + string(b"\x00" + b"\xa5" * (bits // 8))
    return f"ssh-rsa {base64.b64encode(blob).decode()} {comment}"


ADDED = {"vm": "work", "fingerprint": "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA", "name": "laptop",
         "type": "ssh-rsa", "enabled": True, "keys": 1, "state": "running", "applies": "now", "message": "added"}


class SshAccessTest(ControlRoomTest):
    """#135: SSH access from the Control Room. The key goes to agentbox only on stdin, never as
    an argument (agentbox reads no file for the Control Room); the body limit leaves room for
    any RSA key agentbox takes."""

    def setUp(self):
        super().setUp()
        self.login()
        self.respond(ADDED)

    def test_the_key_goes_only_on_stdin(self):
        line = rsa_line(4096)
        status, obj, _ = self.request("POST", "/api/v1/vms/work/ssh-keys", {"key": line + "\n", "name": "laptop"})
        self.assertEqual((status, obj), (201, ADDED))
        self.assertEqual(self.calls(), [["add-ssh-key", "work", "--name=laptop", "--json"]])
        self.assertEqual(self.stdins(), [line + "\n"])
        self.assertNotIn(line.split()[1], json.dumps(self.raw_calls()))
        self.assertIn("work v1 POST post_ssh_key -> ok", self.server_log())
        self.assertNotIn(line.split()[1], self.server_log())

    def test_the_body_limit_holds_and_takes_the_largest_rsa_key(self):
        """RSA 4096 is the usual large key; agentbox takes RSA up to 16384 bits, ~2.8 KiB."""
        for bits in (4096, 16384):
            with self.subTest(bits=bits):
                self.assertEqual(self.request("POST", "/api/v1/vms/work/ssh-keys", {"key": rsa_line(bits)})[0], 201)
        self.assertEqual(len(self.calls()), 2)
        answer = self.request("POST", "/api/v1/vms/work/ssh-keys", {"key": rsa_line(4096) + " " * 8192})
        self.assertError(answer, 413, "body_too_large")
        self.assertEqual(len(self.calls()), 2)

    def test_the_connection_help_is_ssh_config_unchanged(self):
        config = {"mode": "local", "hosts": [{"vm": "work", "host": "work.agentbox", "options": {"Port": 2201},
                                              "config": "Host work.agentbox\n    Port 2201\n",
                                              "known_hosts": "work.agentbox ssh-ed25519 AAAA",
                                              "command": "ssh work.agentbox"}]}
        self.respond(config)
        self.assertEqual(self.get("/api/v1/vms/work/ssh-config"), (200, config))
        self.assertEqual(self.calls(), [["ssh-config", "work", "--json"]])

    def test_a_copy_names_only_the_other_vm(self):
        """#136: {"from": <vm>} is add-ssh-key --from=<vm>, nothing on stdin, nothing else: the
        answer, with which keys went in and which not, passes through unchanged."""
        copied = {"vm": "work", "from": "priv", "folder": None,
                  "added": [{"type": "ssh-ed25519", "fingerprint": ADDED["fingerprint"], "name": "laptop"}],
                  "skipped": [{"type": "ssh-ed25519", "fingerprint": "SHA256:" + "A" * 43, "name": "phone",
                               "error": "ssh_key_duplicate", "message": "entered already"}],
                  "enabled": True, "keys": 2, "state": "running", "applies": "now", "message": "copied"}
        self.respond(copied)
        self.assertEqual(self.request("POST", "/api/v1/vms/work/ssh-keys", {"from": "priv"})[:2], (200, copied))
        self.assertEqual(self.calls(), [["add-ssh-key", "work", "--from=priv", "--json"]])
        self.assertEqual(self.stdins(), [""])
        for body, status, code in (({"from": "priv", "key": "ssh-ed25519 AAAA"}, 400, "invalid_field"),
                                   ({"from": "priv", "name": "x"}, 400, "invalid_field"),
                                   ({"from": "priv", "key": ""}, 400, "invalid_field"),
                                   ({"from": "work"}, 400, "invalid_field"),
                                   ({"from": "--mine"}, 404, "vm_not_found"),
                                   ({"from": "priv --yes"}, 404, "vm_not_found"),
                                   ({"from": "/root/.ssh/id_ed25519.pub"}, 404, "vm_not_found"),
                                   ({"from": ["priv"]}, 404, "vm_not_found"),
                                   ({"from": None}, 404, "vm_not_found")):
            with self.subTest(body=body):
                answer = self.request("POST", "/api/v1/vms/work/ssh-keys", body)
                self.assertError(answer, status, code)
                self.assertEqual(answer[1]["error"]["field"], "from")
        self.assertEqual(len(self.calls()), 1)

    def test_refusals_of_agentbox_pass_through(self):
        self.respond("", stderr=failure("ssh_key_too_weak", "RSA keys need at least 3072 bits", vm="work"),
                     exitcode=1)
        answer = self.request("POST", "/api/v1/vms/work/ssh-keys", {"key": rsa_line(2048)})
        self.assertError(answer, 400, "ssh_key_too_weak")
        self.respond("", stderr=failure("ssh_pending", "the change is saved, but the VM did not take it", vm="work"),
                     exitcode=1)
        self.assertError(self.request("PUT", "/api/v1/vms/work/ssh", {"enabled": False}), 503, "ssh_pending")
        self.assertEqual(self.calls()[-1], ["set-ssh", "work", "off", "--json"])


class AccessTest(ControlRoomTest):
    def test_create_and_delete_need_a_session(self):
        for method, path, body in (("POST", "/api/v1/vms", {"name": "lab"}),
                                   ("DELETE", "/api/v1/vms/lab", {"confirm": "lab"})):
            with self.subTest(method=method):
                self.assertError(self.request(method, path, body), 401, "unauthenticated")
        self.assertEqual(self.calls(), [])

    def test_create_and_delete_need_the_x_agentbox_header(self):
        self.login()
        for method, path in (("POST", "/api/v1/vms"), ("DELETE", "/api/v1/vms/lab")):
            with self.subTest(method=method):
                # no body: the server answers 403 before reading it (see test_control_room.py)
                self.assertError(self.request(method, path, None, header=False), 403, "csrf_header_missing")
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
