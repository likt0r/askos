"""Control Room backend (`web/agentbox-web`): the Settings API and the session length,
against a fake `agentbox` (see fake_agentbox.py), with the server harness of
test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

SHOWN = {"settings": {"maintenance_time": {"value": "02:00", "default": "02:00", "is_default": True,
                                     "applies_later": None, "allowed": "Uhrzeit HH:MM"}}}
SAVED = {"changed": {"maintenance_time": {"from": "02:00", "to": "05:30"}}, "applies_later": {},
         "regenerated": {}}


class SettingsApiTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()

    def test_get_shows_the_settings_of_agentbox(self):
        self.respond(SHOWN)
        self.assertEqual(self.get("/api/v1/settings"), (200, SHOWN))
        self.assertEqual(self.calls(), [["settings", "--json"]])

    def test_patch_passes_the_values_to_agentbox(self):
        self.respond(SAVED)
        answer = self.request("PATCH", "/api/v1/settings", {"maintenance_time": "05:30", "session_hours": 8,
                                                            "default_language": "browser"})
        self.assertEqual(answer[:2], (200, SAVED))
        self.assertEqual(self.calls(), [["set-settings", "maintenance_time=05:30", "session_hours=8",
                                         "default_language=browser", "--json"]])

    def test_a_refusal_of_agentbox_is_passed_on(self):
        self.respond("", stderr=failure("invalid_setting",
                                        "maintenance_time: invalid value 99:99 (allowed: time HH:MM)",
                                        setting="maintenance_time"), exitcode=1)
        answer = self.request("PATCH", "/api/v1/settings", {"maintenance_time": "99:99"})
        self.assertError(answer, 400, "invalid_setting")
        self.assertEqual((answer[1]["error"]["field"], answer[1]["error"]["detail"]),
                         ("maintenance_time", "maintenance_time: invalid value 99:99 (allowed: time HH:MM)"))

    def test_only_known_keys_are_accepted(self):
        for key in ("password", "home_network", "network", "port", "bind", "orca_port", "--json", "x=y",
                    "Maintenance_Time"):
            with self.subTest(key=key):
                answer = self.request("PATCH", "/api/v1/settings", {"maintenance_time": "05:30", key: "x"})
                self.assertError(answer, 400, "unknown_setting")
                self.assertEqual(answer[1]["error"]["field"], key)
        self.assertEqual(self.calls(), [])

    def test_auto_update_takes_true_and_false(self):
        self.respond(SAVED)
        self.assertEqual(self.request("PATCH", "/api/v1/settings", {"auto_update": False})[0], 200)
        self.assertEqual(self.request("PATCH", "/api/v1/settings", {"auto_update": "true"})[0], 200)
        self.assertEqual(self.calls(), [["set-settings", "auto_update=false", "--json"],
                                        ["set-settings", "auto_update=true", "--json"]])

    def test_odd_values_and_empty_bodies_are_rejected(self):
        for body in ({}, {"maintenance_time": True}, {"builds_kept": 1.5}, {"builds_kept": [3]},
                     {"maintenance_time": None}, {"maintenance_time": {"a": 1}}):
            with self.subTest(body=body):
                self.assertError(self.request("PATCH", "/api/v1/settings", body), 400, "invalid_field")
        self.assertEqual(self.calls(), [])

    def test_needs_a_session_and_the_csrf_header(self):
        self.assertError(self.request("PATCH", "/api/v1/settings", None, header=False), 403, "csrf_header_missing")
        self.cookie = None
        self.assertError(self.request("GET", "/api/v1/settings"), 401, "unauthenticated")
        self.assertError(self.request("PATCH", "/api/v1/settings", {"maintenance_time": "05:30"}), 401,
                         "unauthenticated")
        self.assertEqual(self.calls(), [])

class SessionLengthTest(ControlRoomTest):
    def setUp(self):
        # the server reads the Settings as agentbox does, under AGENTBOX_ROOT: never the Host's file
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.settings = os.path.join(root, "etc", "agentbox", "settings.json")
        os.makedirs(os.path.dirname(self.settings))
        with mock.patch.dict(os.environ, AGENTBOX_ROOT=root):
            super().setUp()

    def max_age(self):
        cookie = self.request("POST", "/api/v1/session", {"password": test_control_room.PASSWORD})[2] \
            .getheader("Set-Cookie")
        return int(re.search(r"Max-Age=(\d+)", cookie).group(1))

    def write(self, text):
        with open(self.settings, "w") as f:
            f.write(text)

    def test_default_without_settings(self):
        self.assertEqual(self.max_age(), 12 * 3600)

    def test_session_hours_apply_to_the_next_sign_in(self):
        self.write('{"session_hours": 3}')
        self.assertEqual(self.max_age(), 3 * 3600)
        self.write('{"session_hours": 24}')
        self.assertEqual(self.max_age(), 24 * 3600)

    def test_unusable_settings_fall_back_to_the_default(self):
        # a file agentbox refuses, also for another key, is no file of Settings
        for text in ("kaputt", "[]", '{"session_hours": 0}', '{"session_hours": "5"}', '{"session_hours": true}',
                     '{"session_hours": 169}', '{"session_hours": 3, "bogus": 1}',
                     '{"session_hours": 3, "builds_kept": 0}'):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(self.max_age(), 12 * 3600)


class DocumentedKeysTest(unittest.TestCase):
    def test_the_api_docs_list_the_keys_the_control_room_may_change(self):
        # the backend takes them from settings.SPEC: a new Setting is changeable through
        # PATCH /settings at once, so the docs name it too
        sys.path.insert(0, test_control_room.ROOT)
        from agentbox import settings
        with open(os.path.join(test_control_room.ROOT, "docs", "control-room-api.md")) as f:
            text = f.read()
        listed = text.split("Settings keys the Control Room may change:", 1)[1].split("The password", 1)[0]
        self.assertEqual(sorted(re.findall(r"`([a-z_]+)`", listed)), sorted(settings.SPEC))


if __name__ == "__main__":
    unittest.main()
