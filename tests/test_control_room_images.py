"""Control Room backend (`web/agentbox-web`): the Images and Builds endpoints of #17, against
a fake `agentbox` (see fake_agentbox.py), with the server harness of test_control_room.py.

Live progress of a Build: the UI polls GET /api/images/<image>/log with after_cursor while
GET /api/images shows the Build as running (the backend cannot read the build journal itself).

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

IMAGES = {"images": [{"name": "base", "base": True, "in_release": True, "current": "20261103-120000",
                      "vms": ["priv", "work"], "running": None,
                      "builds": [{"build": "20261103-120000", "status": "ok", "release": "0.1.0", "commit": None,
                                  "current": True, "used_by": ["work"], "pinned_by": []}]}],
          "builds_kept": 3, "release": "0.1.0", "disks_readable": True}
LOG = {"image": "base", "build": "20261103-120000", "status": "running", "step": "build", "error": None,
       "problems": [], "entries": [{"time": "2026-11-03T12:00:01.000000Z", "message": "STEP 1/12", "cursor": "c2"}],
       "cursor": "c2", "truncated": False}


class ImagesApiTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()

    def test_images_and_their_builds(self):
        self.respond(IMAGES)
        self.assertEqual(self.get("/api/images"), (200, IMAGES))
        self.assertEqual(self.calls(), [["images", "--json"]])

    def test_the_log_of_a_build_for_a_live_view(self):
        self.respond(LOG)
        self.assertEqual(self.get("/api/images/base/log"), (200, LOG))
        self.assertEqual(self.get("/api/images/base/log?build=20261103-120000&after_cursor=c1&lines=500"),
                         (200, LOG))
        self.assertEqual(self.get("/api/images/rust-2/log?build=20261101-1530")[0], 200)
        self.assertEqual(self.calls(), [["build-log", "base", "--json"],
                                        ["build-log", "base", "20261103-120000", "--after-cursor=c1", "--lines=500",
                                         "--json"],
                                        ["build-log", "rust-2", "20261101-1530", "--json"]])

    def test_odd_log_requests_never_reach_agentbox(self):
        for path in ("/api/images/base/log?build=--all", "/api/images/base/log?build=x",
                     "/api/images/base/log?build=20261103-120000&build=20261103-120001"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 400)
        for path in ("/api/images/Base/log", "/api/images/-x/log", "/api/images/base/other", "/api/images/base"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)
        self.assertEqual(self.calls(), [])

    def test_a_refusal_of_agentbox_is_passed_on(self):
        self.respond("", stderr=failure("image_has_no_build", "Image rust has no Builds", image="rust"), exitcode=1)
        self.assertEqual(self.get("/api/images/rust/log"), (400, {"error": "Image rust has no Builds"}))

    def test_starting_a_build(self):
        started = {"image": "base", "unit": "agentbox-build@base.service", "started": True, "message": "..."}
        self.respond(started)
        self.assertEqual(self.post("/api/build", {"image": "base"})[:2], (200, started))
        self.assertEqual(self.post("/api/build", {"image": "all"})[:2], (200, started))
        self.assertEqual(self.calls(), [["build", "base", "--json"], ["build", "--all", "--json"]])

    def test_a_running_build_is_reported(self):
        self.respond("", stderr=failure("build_running", "a Build of base runs already (agentbox build-log base)",
                                        image="base"), exitcode=1)
        status, obj, _ = self.post("/api/build", {"image": "base"})
        self.assertEqual((status, obj["ok"]), (400, False))
        self.assertIn("runs already", obj["message"])

    def test_odd_build_requests_never_reach_agentbox(self):
        for body in ({}, {"image": None}, {"image": "--all"}, {"image": "Base"}, {"image": ["base"]},
                     {"image": True}, {"image": "base x"}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/build", body)[0], 400)
        self.assertEqual(self.calls(), [])

    def test_setting_a_vms_image_and_build(self):
        result = {"vm": "priv", "image": "base", "build": "20261101-1530", "message": "..."}
        self.respond(result)
        self.assertEqual(self.post("/api/priv/set-image", {"image": "base", "build": "20261101-1530"})[:2],
                         (200, result))
        self.assertEqual(self.post("/api/priv/set-image", {"image": "rust", "build": None})[0], 200)
        self.assertEqual(self.post("/api/priv/set-image", {"image": "rust"})[0], 200)
        self.assertEqual(self.calls(), [["set-image", "priv", "base", "--build=20261101-1530", "--json"],
                                        ["set-image", "priv", "rust", "--json"], ["set-image", "priv", "rust", "--json"]])

    def test_odd_image_choices_never_reach_agentbox(self):
        for body in ({}, {"image": "-x"}, {"image": "base", "build": "--force"}, {"image": "base", "build": 20261101},
                     {"image": "base", "build": "latest"}, {"image": 3}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/priv/set-image", body)[0], 400)
        self.assertEqual(self.calls(), [])

    def test_needs_a_session_and_the_csrf_header(self):
        self.assertEqual(self.post("/api/build", {}, header=False, raw=b"")[0], 403)
        self.cookie = None
        self.assertEqual(self.get("/api/images")[0], 401)
        self.assertEqual(self.get("/api/images/base/log")[0], 401)
        self.assertEqual(self.post("/api/build", {"image": "base"})[0], 401)
        self.assertEqual(self.post("/api/priv/set-image", {"image": "base"})[0], 401)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
