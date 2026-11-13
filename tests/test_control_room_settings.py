"""Control Room backend (`web/agentbox-web`): the Settings API and the session length,
against a fake `agentbox` (see fake_agentbox.py), with the server harness of
test_control_room.py.

Run from the repo root: python3 -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room  # noqa: E402
from test_control_room import ControlRoomTest, failure  # noqa: E402

setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule


def server_module():
    loader = importlib.machinery.SourceFileLoader("agentbox_web", test_control_room.SERVER)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


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
    def max_age(self):
        cookie = self.request("POST", "/api/v1/session", {"password": test_control_room.PASSWORD})[2] \
            .getheader("Set-Cookie")
        return int(re.search(r"Max-Age=(\d+)", cookie).group(1))

    def write(self, text):
        with open(self.settings, "w") as f:
            f.write(text)

    def test_default_without_settings(self):
        self.assertEqual(self.max_age(), 12 * 3600)
        self.write("{}")
        self.assertEqual(self.max_age(), 12 * 3600)

    def test_session_hours_apply_to_the_next_sign_in(self):
        self.write('{"session_hours": 3}')
        self.assertEqual(self.max_age(), 3 * 3600)
        self.write('{"session_hours": 24}')
        self.assertEqual(self.max_age(), 24 * 3600)


# files agentbox refuses: not JSON, no object, an invalid value (also of another key), an unknown key
REFUSED = ("kaputt", "[]", '{"session_hours": 0}', '{"session_hours": "5"}', '{"session_hours": true}',
           '{"session_hours": 169}', '{"session_hours": 3, "bogus": 1}', '{"session_hours": 3, "builds_kept": 0}',
           '{"default_language": "fr"}')


class RefusedSettingsTest(ControlRoomTest):
    """A settings.json agentbox refuses stops the backend (#97) instead of giving it the defaults,
    which can be wider than what the owner set (12-hour sessions instead of 1): it does not
    start, and a request that finds the file refused while it runs is answered 503
    settings_unreadable before the backend ends. Either way with exit status 78, which its unit
    does not restart."""

    def write(self, text):
        with open(self.settings, "w") as f:
            f.write(text)

    def ended(self):
        """The server's exit status once it ended on its own."""
        try:
            return self.server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("the server did not end: " + self.server_log())

    def restart(self):
        """A server started anew, without a settings.json."""
        self.stop_server()
        if os.path.exists(self.settings):
            os.remove(self.settings)
        self.start_server()
        self.wait_for_server()

    def test_it_does_not_start_with_one(self):
        for text in REFUSED:
            with self.subTest(text=text):
                self.stop_server()
                self.write(text)
                self.start_server()
                self.assertEqual(self.ended(), 78)
                log = self.server_log()
                # one line with file and reason, as for a broken Host setup
                self.assertRegex(log, rf"\Aagentbox-web: Settings {re.escape(self.settings)}: [^\n]+\n\Z")

    def test_a_sign_in_that_finds_one_is_answered_503_and_the_backend_ends(self):
        for text in REFUSED:
            with self.subTest(text=text):
                self.restart()
                self.write(text)
                status, obj, resp = self.request("POST", "/api/v1/session", {"password": test_control_room.PASSWORD})
                self.assertEqual((status, obj["error"]["code"]), (503, "settings_unreadable"), obj)
                self.assertTrue(obj["error"]["detail"].startswith(f"{self.settings}: "), obj)
                self.assertIsNone(resp.getheader("Set-Cookie"))
                self.assertEqual(self.ended(), 78)
                self.assertIn(f"Settings {self.settings}: ", self.server_log())
                self.assertIn("; the Control Room stops", self.server_log())

    def test_a_page_load_that_finds_one_is_answered_503_and_the_backend_ends(self):
        self.write('{"session_hours": 1}')
        self.login()
        self.write('{"session_hours": 12, "bogus": 1}')
        self.assertError(self.request("GET", "/api/v1/session"), 503, "settings_unreadable")
        self.assertEqual(self.ended(), 78)

    def test_the_unit_does_not_restart_it(self):
        with open(os.path.join(test_control_room.ROOT, "rpm", "units", "agentbox-web.service")) as f:
            unit = f.read()
        self.assertIn("\nRestart=on-failure\n", unit)
        self.assertIn("\nRestartPreventExitStatus=78\n", unit)
        self.assertEqual(server_module().CONFIG_REFUSED, 78)


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
