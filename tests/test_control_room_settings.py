"""Control Room backend (`web/agentbox-web`): the Settings API and the session length,
against a fake `agentbox` (see fake_agentbox.py), with the server harness of
test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, SERVER, failure  # noqa: E402

setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule

SHOWN = {"settings": {"reset_time": {"value": "04:00", "default": "04:00", "is_default": True,
                                     "applies_later": None, "allowed": "Uhrzeit HH:MM"}}}
SAVED = {"changed": {"reset_time": {"from": "04:00", "to": "05:30"}}, "applies_later": {}, "regenerated": {}}


class SettingsApiTest(ControlRoomTest):
    def setUp(self):
        super().setUp()
        self.login()

    def test_get_shows_the_settings_of_agentbox(self):
        self.respond(SHOWN)
        self.assertEqual(self.get("/api/settings"), (200, SHOWN))
        self.assertEqual(self.calls(), [["settings", "--json"]])

    def test_post_passes_the_values_to_agentbox(self):
        self.respond(SAVED)
        status, obj, _ = self.post("/api/settings", {"reset_time": "05:30", "session_hours": 8,
                                                     "default_language": "browser"})
        self.assertEqual((status, obj), (200, SAVED))
        self.assertEqual(self.calls(), [["set-settings", "reset_time=05:30", "session_hours=8",
                                         "default_language=browser", "--json"]])

    def test_a_refusal_of_agentbox_is_passed_on(self):
        self.respond("", stderr=failure("invalid_setting", "reset_time: invalid value 99:99 (allowed: time HH:MM)",
                                        setting="reset_time"), exitcode=1)
        status, obj, _ = self.post("/api/settings", {"reset_time": "99:99"})
        self.assertEqual((status, obj), (400, {"ok": False, "message": "reset_time: invalid value 99:99 "
                                                                      "(allowed: time HH:MM)"}))

    def test_only_known_keys_are_accepted(self):
        for key in ("password", "home_network", "network", "port", "bind", "orca_port", "--json", "x=y",
                    "Reset_Time"):
            with self.subTest(key=key):
                status, obj, _ = self.post("/api/settings", {"reset_time": "05:30", key: "x"})
                self.assertEqual(status, 400)
                self.assertFalse(obj["ok"])
                self.assertIn("unknown setting", obj["message"])
        self.assertEqual(self.calls(), [])

    def test_auto_update_takes_true_and_false(self):
        self.respond(SAVED)
        self.assertEqual(self.post("/api/settings", {"auto_update": False})[0], 200)
        self.assertEqual(self.post("/api/settings", {"auto_update": "true"})[0], 200)
        self.assertEqual(self.calls(), [["set-settings", "auto_update=false", "--json"],
                                        ["set-settings", "auto_update=true", "--json"]])

    def test_odd_values_and_empty_bodies_are_rejected(self):
        for body in ({}, {"reset_time": True}, {"builds_kept": 1.5}, {"builds_kept": [3]}, {"reset_time": None},
                     {"reset_time": {"a": 1}}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/settings", body)[0], 400)
        self.assertEqual(self.calls(), [])

    def test_needs_a_session_and_the_csrf_header(self):
        self.assertEqual(self.post("/api/settings", {}, header=False, raw=b"")[0], 403)
        self.cookie = None
        self.assertEqual(self.get("/api/settings")[0], 401)
        self.assertEqual(self.post("/api/settings", {"reset_time": "05:30"})[0], 401)
        self.assertEqual(self.calls(), [])

    def test_the_known_keys_are_the_ones_of_agentbox(self):
        loader = importlib.machinery.SourceFileLoader("agentbox_web", SERVER)
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        env = dict(os.environ, AGENTBOX_ROOT=self.dir)       # never the real /etc/agentbox
        agentbox = os.path.join(os.path.dirname(os.path.dirname(SERVER)), "bin", "agentbox")
        out = subprocess.run([sys.executable, agentbox, "settings", "--json"],
                             capture_output=True, text=True, env=env, check=True).stdout
        self.assertEqual(list(module.SETTING_KEYS), list(json.loads(out)["settings"]))
        # GET /session reads default_language itself: the same values, the default first
        language = json.loads(out)["settings"]["default_language"]
        self.assertEqual(module.LANGUAGES[0], language["default"])
        self.assertEqual("one of " + ", ".join(module.LANGUAGES), language["allowed"])


class SessionLengthTest(ControlRoomTest):
    def setUp(self):
        self.settings = os.path.join(test_control_room.conf.name, "settings.json")
        os.environ["AGENTBOX_WEB_SETTINGS"] = self.settings
        self.addCleanup(os.environ.pop, "AGENTBOX_WEB_SETTINGS", None)
        self.addCleanup(lambda: os.path.exists(self.settings) and os.remove(self.settings))
        super().setUp()

    def max_age(self):
        cookie = self.post("/api/login", {"password": test_control_room.PASSWORD})[2]
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
        for text in ("kaputt", "[]", '{"session_hours": 0}', '{"session_hours": "5"}', '{"session_hours": true}',
                     '{"session_hours": 169}'):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(self.max_age(), 12 * 3600)


if __name__ == "__main__":
    unittest.main()
