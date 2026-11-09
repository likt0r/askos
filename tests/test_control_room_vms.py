"""Control Room backend (`web/agentbox-web`): creating and deleting a VM, against a fake
`agentbox` (see fake_agentbox.py), with the server harness of test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
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

    def create(self, body, ctx="lab"):
        status, obj, _ = self.post(f"/api/{ctx}/create", body)
        return status, obj

    def test_fields_map_to_options(self):
        status, obj = self.create({"image": "base", "memory_mib": 8192, "data_disk_gib": "50",
                                   "github_owners": ["likt0r", " acme-corp ", ""], "copy_allowlist_from": "priv"})
        self.assertEqual((status, obj), (200, CREATED))
        self.assertEqual(self.calls(), [["create", "lab", "--image=base", "--memory-mib=8192", "--data-disk-gib=50",
                                         "--copy-allowlist-from=priv", "--owner=likt0r", "--owner=acme-corp",
                                         "--json"]])

    def test_without_fields_agentbox_uses_its_defaults(self):
        for body in ({}, {"image": None, "memory_mib": "", "github_owners": None, "copy_allowlist_from": None}):
            with self.subTest(body=body):
                self.assertEqual(self.create(body)[0], 200)
                self.assertEqual(self.calls()[-1], ["create", "lab", "--json"])

    def test_values_stay_values(self):
        self.create({"image": "--delete-data-disk", "copy_allowlist_from": "priv --json", "github_owners": ["-x"]})
        self.assertEqual(self.calls(), [["create", "lab", "--image=--delete-data-disk",
                                         "--copy-allowlist-from=priv --json", "--owner=-x", "--json"]])

    def test_odd_values_are_rejected_not_converted(self):
        for body in ({"memory_mib": True}, {"data_disk_gib": 5.5}, {"image": ["base"]}, {"github_owners": "likt0r"},
                     {"github_owners": [1]}, {"copy_allowlist_from": {"vm": "priv"}}):
            with self.subTest(body=body):
                status, obj = self.create(body)
                self.assertEqual(status, 400)
                self.assertFalse(obj["ok"])
        self.assertEqual(self.calls(), [])

    def test_refusal_of_agentbox_is_passed_on(self):
        self.respond("", stderr=failure("name_reserved", "the name status is reserved", name="status"), exitcode=1)
        self.assertEqual(self.create({}, ctx="status"), (400, {"ok": False, "message": "the name status is reserved"}))
        self.assertEqual(self.calls(), [["create", "status", "--json"]])
        self.assertIn("status create -> FAILED", self.server_log())

    def test_invalid_names_never_reach_agentbox(self):
        for ctx in ("Lab", "x", "a" * 17, "--all", ".."):
            with self.subTest(ctx=ctx):
                self.assertEqual(self.create({}, ctx=ctx)[0], 404)
        self.assertEqual(self.calls(), [])


class DeleteTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()
        self.respond(DELETED)

    def delete(self, body, ctx="lab"):
        status, obj, _ = self.post(f"/api/{ctx}/delete", body)
        return status, obj

    def test_requires_the_typed_name(self):
        for body in ({}, {"confirm": "priv"}, {"confirm": "LAB"}, {"confirm": " lab"}, {"confirm": None},
                     {"confirm": ["lab"]}):
            with self.subTest(body=body):
                self.assertEqual(self.delete(body),
                                 (400, {"ok": False, "message": "invalid input: type the name lab to delete it"}))
        self.assertEqual(self.calls(), [])

    def test_archives_the_data_disk_by_default(self):
        self.assertEqual(self.delete({"confirm": "lab"}), (200, DELETED))
        self.assertEqual(self.calls(), [["delete", "lab", "--confirm=lab", "--json"]])
        self.assertIn("lab delete -> ok", self.server_log())

    def test_deletes_the_data_disk_only_when_asked(self):
        self.delete({"confirm": "lab", "delete_data_disk": False})
        self.delete({"confirm": "lab", "delete_data_disk": True})
        self.assertEqual(self.calls(), [["delete", "lab", "--confirm=lab", "--json"],
                                        ["delete", "lab", "--confirm=lab", "--delete-data-disk", "--json"]])
        for odd in ("true", 1, "yes", None):
            with self.subTest(delete_data_disk=odd):
                self.assertEqual(self.delete({"confirm": "lab", "delete_data_disk": odd})[0], 400)
        self.assertEqual(len(self.calls()), 2)

    def test_failure_is_reported(self):
        self.respond("", stderr=failure("vm_not_found", "No such VM: lab", vm="lab"), exitcode=1)
        self.assertEqual(self.delete({"confirm": "lab"}), (400, {"ok": False, "message": "No such VM: lab"}))


class AccessTest(ControlRoomTest):
    def test_create_and_delete_need_a_session(self):
        for action, body in (("create", {}), ("delete", {"confirm": "lab"})):
            with self.subTest(action=action):
                status, _, _ = self.post(f"/api/lab/{action}", body)
                self.assertEqual(status, 401)
        self.assertEqual(self.calls(), [])

    def test_create_and_delete_need_the_x_agentbox_header(self):
        self.login()
        for action in ("create", "delete"):
            with self.subTest(action=action):
                # empty body: the server answers 403 before reading the body (see test_control_room.py)
                status, _, _ = self.post(f"/api/lab/{action}", raw=b"", header=False)
                self.assertEqual(status, 403)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
