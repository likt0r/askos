"""Settings (#22): `agentbox settings` and `agentbox set-settings`, the timers and journald
retention they regenerate, and the consumers that read the values.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root (never the real
/etc/agentbox) and the usual fakes (virsh, qemu-img, systemctl, virt-install) on PATH.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_journald import Journald  # noqa: E402
from test_vms import VmsTest  # noqa: E402

DEFAULTS = {"reset_age_days": 7, "maintenance_time": "02:00", "rebuild_day": "sun",
            "builds_kept": 3, "default_memory_mib": 26624, "default_data_disk_gib": 300, "default_image": "base",
            "open_web_max_minutes": 240, "temporary_entry_default": "1h", "young_domain_days": 14,
            "feed_interval_hours": 12, "package_min_age_days": 2, "auto_update": True, "orca_version": "",
            "activity_retention_days": 90, "session_hours": 12, "default_language": "browser"}
INVALID = {
    "reset_age_days": ["0", "366", "abc", "1.5", "-1", ""], "maintenance_time": ["24:00", "4:00", "04:60", "x"],
    "rebuild_day": ["monday", "", "7"], "builds_kept": ["0", "21"],
    "default_memory_mib": ["1023", "1048577"], "default_data_disk_gib": ["0", "16385"],
    "default_image": ["Base", "-x", "a b", "a" * 33], "open_web_max_minutes": ["0", "1441"],
    "temporary_entry_default": ["30m", "2h", "1d", "15"], "young_domain_days": ["0", "10", "42", "14.0"],
    "feed_interval_hours": ["0", "169"], "package_min_age_days": ["0", "31"],
    "auto_update": ["", "on", "True", "1", "yes", "false "],
    "orca_version": ["1.4", "v1.4.250", "1.4.250-rc.0", "01.4.250", "1.4.250 ", " ", "latest", "1.4.250;true",
                     "1.4.1234567890"],
    "activity_retention_days": ["0", "3651"], "session_hours": ["0", "169"],
    "default_language": ["fr", "EN", "Browser", "auto", ""],
}
RESTART = ["restart", "agentbox-feeds.timer"]
STAMP = "var/lib/systemd/timers/stamp-agentbox-maintenance.timer"


class SettingsTest(VmsTest):
    SETTINGS = "etc/agentbox/settings.json"

    def stored(self):
        with open(self.path(self.SETTINGS)) as f:
            return json.load(f)

    def settings(self):
        info = json.loads(self.ok("settings", "--json"))["settings"]
        return {k: v["value"] for k, v in info.items()}

    def unit(self, name):
        return self.read("etc/systemd/system/" + name)


class ShowTest(SettingsTest):
    def test_without_a_file_every_setting_has_its_default(self):
        self.assertEqual(self.settings(), DEFAULTS)
        info = json.loads(self.ok("settings", "--json"))["settings"]
        self.assertTrue(all(v["is_default"] and v["default"] == DEFAULTS[k] for k, v in info.items()))
        self.assertFalse(self.exists(self.SETTINGS))

    def test_text_marks_defaults_and_changes_and_what_applies_later(self):
        self.ok("set-settings", "builds_kept=5", "package_min_age_days=3")
        out = self.ok("settings")
        self.assertRegex(out, r"(?m)^reset_age_days +7 +\(default\)$")
        self.assertRegex(out, r"(?m)^package_min_age_days +3 +\(changed, default: 2; applies from the next Build\)$")
        later = {k: v["applies_later"] for k, v in json.loads(self.ok("settings", "--json"))["settings"].items()}
        self.assertEqual(later["package_min_age_days"], "from the next Build")
        self.assertIsNone(later["maintenance_time"])


class OrcaVersionTest(SettingsTest):
    """orca_version (#113): empty, the default, lets every Build take the newest Orca release
    at least package_min_age_days old; a version pins it."""

    def test_a_version_pins_orca_and_empty_or_default_goes_back_to_automatic(self):
        out = self.ok("set-settings", "orca_version=1.4.250")
        self.assertIn('orca_version: "" -> 1.4.250 (applies from the next Build)', out)
        self.assertEqual(self.stored(), {"orca_version": "1.4.250"})
        self.assertRegex(self.ok("settings"), r'(?m)^orca_version +1\.4\.250 +\(changed, default: ""; applies '
                                              r'from the next Build\)$')
        self.assertIn('orca_version: 1.4.250 -> ""', self.ok("set-settings", "orca_version="))
        self.assertEqual(self.stored(), {"orca_version": ""})
        self.ok("set-settings", "orca_version=0.10.3")
        self.ok("set-settings", "orca_version=default")
        self.assertEqual(self.stored(), {})
        self.assertRegex(self.ok("settings"), r'(?m)^orca_version +"" +\(default; applies from the next Build\)$')

    def test_the_file_holds_text(self):
        for content, ok in (('{"orca_version": "1.4.250"}', True), ('{"orca_version": ""}', True),
                            ('{"orca_version": 1}', False), ('{"orca_version": null}', False),
                            ('{"orca_version": "1.4"}', False)):
            with self.subTest(content=content):
                self.write(self.SETTINGS, content)
                if ok:
                    self.assertEqual(self.settings()["orca_version"], json.loads(content)["orca_version"])
                else:
                    self.assertIn("orca_version: invalid value", self.refused("settings"))

    def test_the_fetch_of_the_version_check_gets_it_from_its_unit(self):
        self.ok("regenerate")
        unit = self.unit("agentbox-versions-fetch.service")
        self.assertIn("Environment=AGENTBOX_VERSIONS_ORCA_VERSION=\n", unit)
        self.forget_calls()
        self.ok("set-settings", "orca_version=1.4.250")
        self.assertIn("Environment=AGENTBOX_VERSIONS_ORCA_VERSION=1.4.250\n", self.unit("agentbox-versions-fetch.service"))
        # a changed pin checks at once
        self.assertIn(["start", "--no-block", "agentbox-versions.service"], self.systemctl())


class AutoUpdateTest(SettingsTest):
    """auto_update (#114): on by default; true or false, in the file a JSON boolean."""

    def test_on_by_default_and_off_and_back(self):
        self.assertRegex(self.ok("settings"), r"(?m)^auto_update +true +\(default\)$")
        self.assertIn("auto_update: true -> false", self.ok("set-settings", "auto_update=false"))
        self.assertEqual(self.stored(), {"auto_update": False})
        self.assertRegex(self.ok("settings"), r"(?m)^auto_update +false +\(changed, default: true\)$")
        result = json.loads(self.ok("set-settings", "auto_update=true", "--json"))
        self.assertEqual(result["changed"], {"auto_update": {"from": False, "to": True}})
        self.assertEqual(self.stored(), {"auto_update": True})
        self.ok("set-settings", "auto_update=default")
        self.assertEqual(self.stored(), {})
        info = json.loads(self.ok("settings", "--json"))["settings"]["auto_update"]
        self.assertEqual((info["value"], info["default"], info["allowed"], info["applies_later"]),
                         (True, True, "true or false", None))

    def test_the_file_holds_a_boolean(self):
        for content, ok in (('{"auto_update": false}', True), ('{"auto_update": true}', True),
                            ('{"auto_update": "false"}', False), ('{"auto_update": 0}', False),
                            ('{"auto_update": null}', False)):
            with self.subTest(content=content):
                self.write(self.SETTINGS, content)
                if ok:
                    self.assertEqual(self.settings()["auto_update"], json.loads(content)["auto_update"])
                else:
                    self.assertIn("auto_update: invalid value", self.refused("settings"))

    def test_the_switch_needs_no_unit(self):
        # the version check and the Reset check of the Maintenance window read it when they run
        self.ok("regenerate")
        self.forget_calls()
        self.ok("set-settings", "auto_update=false")
        self.assertEqual([c for c in self.systemctl() if c[0] != "enable"], [])


class SetTest(SettingsTest):
    def test_saves_and_shows_the_values(self):
        out = self.ok("set-settings", "reset_age_days=10", "default_language=de")
        self.assertIn("reset_age_days: 7 -> 10", out)
        self.assertEqual(self.stored(), {"default_language": "de", "reset_age_days": 10})
        self.assertEqual(self.settings(), {**DEFAULTS, "reset_age_days": 10, "default_language": "de"})
        self.assertEqual(self.mode(self.SETTINGS), 0o644)

    def test_json_says_what_changed_and_what_applies_later(self):
        result = json.loads(self.ok("set-settings", "package_min_age_days=5", "maintenance_time=05:30", "--json"))
        self.assertEqual(result["changed"], {"package_min_age_days": {"from": 2, "to": 5},
                                             "maintenance_time": {"from": "02:00", "to": "05:30"}})
        self.assertEqual(result["applies_later"], {"package_min_age_days": "from the next Build"})
        self.assertTrue(result["regenerated"]["timers"])

    def test_every_setting_accepts_a_valid_value(self):
        valid = {"reset_age_days": "1", "maintenance_time": "23:59", "rebuild_day": "mon",
                 "builds_kept": "20", "default_memory_mib": "1024", "default_data_disk_gib": "16384",
                 "default_image": "rust-2", "open_web_max_minutes": "1440", "temporary_entry_default": "7d",
                 "young_domain_days": "35", "feed_interval_hours": "168", "package_min_age_days": "30",
                 "auto_update": "false", "orca_version": "1.4.250", "activity_retention_days": "3650",
                 "session_hours": "168", "default_language": "de"}
        self.ok("set-settings", *[f"{k}={v}" for k, v in valid.items()])
        typed = {k: (v == "true" if isinstance(DEFAULTS[k], bool) else int(v) if isinstance(DEFAULTS[k], int) else v)
                 for k, v in valid.items()}
        self.assertEqual(self.settings(), typed)

    def test_invalid_values_are_refused_and_nothing_is_saved(self):
        for key, values in INVALID.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    err = self.refused("set-settings", f"{key}={value}")
                    self.assertRegex(err, rf"^agentbox: {key}: invalid value .*\(allowed: .+\)\n$")
        self.assertFalse(self.exists(self.SETTINGS))
        self.assertEqual(self.systemctl(), [])

    def test_one_invalid_value_keeps_the_valid_ones_unsaved(self):
        self.ok("set-settings", "builds_kept=4")
        err = self.refused("set-settings", "reset_age_days=9", "maintenance_time=99:99")
        self.assertIn("maintenance_time", err)
        self.assertEqual(self.stored(), {"builds_kept": 4})

    def test_unknown_keys_are_refused_password_network_and_ports_among_them(self):
        for key in ("password", "web_password", "home_network", "network", "port", "web_port", "bind", "orca_port",
                    "nonsense", "", "Maintenance_Time"):
            with self.subTest(key=key):
                self.assertIn("unknown setting", self.refused("set-settings", f"{key}=x"))
                # `default` is no way around the check (it once crashed with a KeyError)
                self.assertIn("unknown setting", self.refused("set-settings", f"{key}=default"))
        self.assertFalse(self.exists(self.SETTINGS))

    def test_malformed_calls_are_refused(self):
        for args in ([], ["--json"], ["maintenance_time"], ["maintenance_time=05:00", "maintenance_time=06:00"]):
            with self.subTest(args=args):
                self.assertIn("set-settings KEY=VALUE", self.refused("set-settings", *args))

    def test_default_goes_back_to_the_built_in_value(self):
        self.ok("set-settings", "reset_age_days=10", "builds_kept=5")
        self.assertIn("reset_age_days: 10 -> 7", self.ok("set-settings", "reset_age_days=default"))
        self.assertEqual(self.stored(), {"builds_kept": 5})

    def test_default_removes_a_stored_value_equal_to_the_default(self):
        # stored on purpose, or next to another change: `default` must still remove it, so a
        # later change of the built-in default applies
        self.ok("set-settings", "reset_age_days=10", "builds_kept=3", "package_min_age_days=2")
        self.assertEqual(self.stored(), {"builds_kept": 3, "package_min_age_days": 2, "reset_age_days": 10})
        self.forget_calls()
        self.assertEqual(self.ok("set-settings", "builds_kept=default"), "unchanged\n")
        self.assertEqual(self.stored(), {"package_min_age_days": 2, "reset_age_days": 10})
        self.assertEqual(self.mode(self.SETTINGS), 0o644)
        self.assertEqual(self.systemctl(), [])        # no value changed: nothing to apply
        self.assertEqual(json.loads(self.ok("set-settings", "package_min_age_days=default", "--json")),
                         {"changed": {}, "applies_later": {}, "regenerated": None})
        self.assertEqual(self.stored(), {"reset_age_days": 10})
        result = json.loads(self.ok("set-settings", "reset_age_days=default", "builds_kept=default", "--json"))
        self.assertEqual(result["changed"], {"reset_age_days": {"from": 10, "to": 7}})
        self.assertEqual(self.stored(), {})

    def test_default_of_a_key_not_stored_leaves_the_file_alone(self):
        self.ok("set-settings", "builds_kept=5")
        before = os.stat(self.path(self.SETTINGS))
        self.forget_calls()
        self.assertEqual(self.ok("set-settings", "reset_age_days=default"), "unchanged\n")
        after = os.stat(self.path(self.SETTINGS))
        # write_atomic renames a new file over it: a rewrite, even with the same bytes, is a new inode
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(self.stored(), {"builds_kept": 5})
        self.assertEqual(self.systemctl(), [])

    def test_unchanged_values_change_nothing(self):
        self.ok("set-settings", "builds_kept=5")
        before = self.files()
        self.forget_calls()
        self.assertEqual(self.ok("set-settings", "builds_kept=5", "reset_age_days=7"), "unchanged\n")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.systemctl(), [])

    def test_broken_files_are_a_clear_error_everywhere(self):
        for text in ("kaputt", "[]", '{"builds_kept": 0}', '{"nonsense": 1}', '{"maintenance_time": "x"}',
                     '{"reset_time": "x"}', '{"reset_time": 4}'):
            with self.subTest(text=text):
                self.write(self.SETTINGS, text)
                for args in (["settings"], ["set-settings", "builds_kept=4"], ["regenerate"], ["create", "lab"],
                             ["open-web", "work", "5"]):
                    code, out, err = self.run_agentbox(*args)
                    self.assertEqual((code, out), (1, ""), args)
                    self.assertRegex(err, r"^agentbox: .*settings\.json: ", args)
                self.assertEqual(self.read(self.SETTINGS), text)


class DefaultLanguageTest(SettingsTest):
    """default_language: `browser` (the default) leaves the language to each browser; en or de
    wins over the browser's language after sign-in (the order itself is the UI's, web/ui)."""

    def test_browser_is_the_default_and_en_and_de_are_the_other_choices(self):
        info = json.loads(self.ok("settings", "--json"))["settings"]["default_language"]
        self.assertEqual((info["value"], info["default"], info["is_default"]), ("browser", "browser", True))
        self.assertEqual(info["allowed"], "one of browser, en, de")
        self.assertEqual(info["applies_later"], "from the next sign-in or the next load of the Control Room")
        for value in ("en", "de", "browser"):
            with self.subTest(value=value):
                self.ok("set-settings", f"default_language={value}")
                self.assertEqual(self.settings()["default_language"], value)
        self.assertEqual(self.stored(), {"default_language": "browser"})

    def test_a_stored_en_stays_a_choice_of_english(self):
        # a key is stored only when a CLI or API call named it (the Control Room sends `default`
        # for the default): on purpose, or restating the old default next to another change.
        # It is kept, not migrated (the owner may decide otherwise; titan's file is {})
        self.write(self.SETTINGS, '{"default_language": "en"}')
        info = json.loads(self.ok("settings", "--json"))["settings"]["default_language"]
        self.assertEqual((info["value"], info["is_default"]), ("en", False))
        self.assertRegex(self.ok("settings"), r"(?m)^default_language +en +\(changed, default: browser; "
                                              r"applies from the next sign-in or the next load of the Control Room\)$")
        self.assertIn("default_language: en -> browser", self.ok("set-settings", "default_language=default"))
        self.assertEqual(self.stored(), {})


class TimerTest(SettingsTest):
    def test_regenerate_writes_the_timers_with_the_defaults(self):
        self.ok("regenerate")
        self.assertIn("OnCalendar=*-*-* 02:00\n", self.unit("agentbox-maintenance.timer"))
        self.assertIn("ExecStart=/usr/bin/agentbox --actor=timer maintenance\n",
                      self.unit("agentbox-maintenance.service"))
        self.assertIn("OnUnitActiveSec=12h\n", self.unit("agentbox-feeds.timer"))
        self.assertIn("OnBootSec=10min\n", self.unit("agentbox-feeds.timer"))
        self.assertIn("Environment=AGENTBOX_FEEDS_YOUNG_DAYS=14\n", self.unit("agentbox-feeds.service"))
        self.assertIn("User=agentbox-feeds", self.unit("agentbox-feeds.service"))
        self.assertIn(["enable", "--now", "agentbox-maintenance.timer"], self.systemctl())
        self.assertIn(["enable", "--now", "agentbox-feeds.timer"], self.systemctl())

    def test_saving_regenerates_and_restarts_the_timers(self):
        self.ok("regenerate")
        self.forget_calls()
        self.ok("set-settings", "maintenance_time=05:30", "feed_interval_hours=6", "young_domain_days=21")
        self.assertIn("OnCalendar=*-*-* 05:30\n", self.unit("agentbox-maintenance.timer"))
        self.assertIn("OnUnitActiveSec=6h\n", self.unit("agentbox-feeds.timer"))
        self.assertIn("Environment=AGENTBOX_FEEDS_YOUNG_DAYS=21\n", self.unit("agentbox-feeds.service"))
        calls = self.systemctl()
        self.assertLess(calls.index(["daemon-reload"]), calls.index(RESTART))
        self.assertIn(["enable", "--now", "agentbox-maintenance.timer"], calls)
        self.assertIn(["enable", "--now", "agentbox-feeds.timer"], calls)

    def test_a_new_maintenance_time_does_not_catch_up_a_run(self):
        # Persistent=true would run the window at once if the timer were reloaded or restarted
        # with its last run before the new time: stopped, stamped now, started
        self.ok("regenerate")
        os.utime(self.path(STAMP), (0, 0))
        self.forget_calls()
        self.ok("set-settings", "maintenance_time=05:30")
        calls = self.systemctl()
        self.assertNotIn(["restart", "agentbox-maintenance.timer"], [c[:2] for c in calls])
        self.assertNotIn(["start", "agentbox-maintenance.service"], calls)
        stop, reload, start = (calls.index(["stop", "agentbox-maintenance.timer"]), calls.index(["daemon-reload"]),
                               calls.index(["start", "agentbox-maintenance.timer"]))
        self.assertLess(stop, reload)
        self.assertLess(reload, start)
        self.assertGreater(os.path.getmtime(self.path(STAMP)), time.time() - 60)
        # the Feeds keep their own timer
        self.assertNotIn(RESTART, calls)

    def test_settings_without_timers_leave_them_alone(self):
        self.ok("regenerate")
        self.forget_calls()
        self.ok("set-settings", "builds_kept=5", "package_min_age_days=4", "default_memory_mib=8192")
        self.assertNotIn(RESTART, self.systemctl())
        self.assertNotIn(["stop", "agentbox-maintenance.timer"], self.systemctl())
        # the rebuild day needs no unit: the window reads it when it runs
        self.forget_calls()
        self.ok("set-settings", "rebuild_day=fri")
        self.assertEqual([c for c in self.systemctl() if c[0] != "enable"], [])

    def test_an_unchanged_regenerate_does_not_restart_them(self):
        self.ok("regenerate")
        self.forget_calls()
        self.ok("regenerate")
        self.assertEqual([c for c in self.systemctl() if c[0] != "enable"], [])

    def test_a_failed_restart_is_retried_by_the_next_regenerate(self):
        self.ok("regenerate")
        self.scenario(systemctl_fails=["restart"])
        code, out, err = self.run_agentbox("set-settings", "feed_interval_hours=6")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("saved, but not applied", err)
        self.assertEqual(self.stored(), {"feed_interval_hours": 6})
        self.scenario()
        self.forget_calls()
        self.ok("regenerate")
        self.assertIn(RESTART, self.systemctl())

    def test_a_failed_start_of_the_timer_is_retried_by_the_next_regenerate(self):
        self.ok("regenerate")
        self.scenario(systemctl_fails=["start"])
        code, out, err = self.run_agentbox("set-settings", "maintenance_time=06:00")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("saved, but not applied", err)
        self.scenario()
        self.forget_calls()
        self.ok("regenerate")
        self.assertIn(["start", "agentbox-maintenance.timer"], self.systemctl())

    def test_retention_is_applied_to_the_journal_namespace(self):
        self.ok("regenerate")
        self.forget_calls()
        self.ok("set-settings", "activity_retention_days=30")
        self.assertIn("MaxRetentionSec=30day", self.read("etc/systemd/journald@agentbox.conf"))
        self.assertIn(["try-restart", "systemd-journald@agentbox.service"], self.systemctl())


class MigrationTest(SettingsTest):
    """A settings.json from before the Maintenance window (#49), with reset_time and rebuild_time:
    maintenance_time is reset_time minus two hours (when the version check ran), rebuild_time is
    dropped, and both keys leave the file at its next write."""

    def test_reset_time_becomes_the_maintenance_time_two_hours_earlier(self):
        for reset, maintenance in (("04:00", "02:00"), ("05:30", "03:30"), ("02:00", "00:00"), ("01:15", "23:15"),
                                   ("00:00", "22:00"), ("23:59", "21:59")):
            with self.subTest(reset=reset):
                self.write(self.SETTINGS, json.dumps({"reset_time": reset, "rebuild_day": "sat",
                                                      "rebuild_time": "03:00"}))
                values = self.settings()
                self.assertEqual((values["maintenance_time"], values["rebuild_day"]), (maintenance, "sat"))
                self.assertEqual(set(values), set(DEFAULTS))
                info = json.loads(self.ok("settings", "--json"))["settings"]["maintenance_time"]
                self.assertEqual(info["is_default"], maintenance == "02:00")

    def test_a_maintenance_time_in_the_file_wins(self):
        self.write(self.SETTINGS, '{"reset_time": "05:30", "maintenance_time": "01:00"}')
        self.assertEqual(self.settings()["maintenance_time"], "01:00")
        self.write(self.SETTINGS, '{"rebuild_time": "03:00"}')
        self.assertEqual(self.settings(), DEFAULTS)

    def test_regenerate_writes_the_timer_at_that_time_and_leaves_the_file(self):
        text = '{"reset_time": "05:30", "rebuild_time": "03:00"}'
        self.write(self.SETTINGS, text)
        self.ok("regenerate")
        self.assertIn("OnCalendar=*-*-* 03:30\n", self.unit("agentbox-maintenance.timer"))
        self.assertEqual(self.read(self.SETTINGS), text)

    def test_the_old_keys_are_dropped_at_the_next_write(self):
        self.write(self.SETTINGS, '{"builds_kept": 5, "rebuild_time": "03:00", "reset_time": "05:30"}')
        self.assertIn("reset_age_days: 7 -> 9", self.ok("set-settings", "reset_age_days=9"))
        self.assertEqual(self.stored(), {"builds_kept": 5, "maintenance_time": "03:30", "reset_age_days": 9})
        self.write(self.SETTINGS, '{"reset_time": "05:30"}')
        self.assertIn("maintenance_time: 03:30 -> 02:00", self.ok("set-settings", "maintenance_time=default"))
        self.assertEqual(self.stored(), {})

    def test_the_old_keys_are_no_settings_any_more(self):
        for key in ("reset_time", "rebuild_time"):
            for value in ("05:00", "default"):
                with self.subTest(key=key, value=value):
                    err = self.refused("set-settings", f"{key}={value}")
                    self.assertIn(f"{key} is no longer a setting", err)
                    self.assertIn("maintenance_time", err)
        self.assertFalse(self.exists(self.SETTINGS))
        self.assertNotIn("reset_time", json.loads(self.ok("settings", "--json"))["settings"])


class ConsumerTest(SettingsTest):
    def record(self, name):
        with open(self.path(f"etc/agentbox/vms/{name}.json")) as f:
            return json.load(f)

    def test_new_vms_start_with_the_defaults_of_the_settings(self):
        self.ok("set-settings", "default_memory_mib=8192", "default_data_disk_gib=50")
        self.ok("create", "lab")
        rec = self.record("lab")
        self.assertEqual((rec["image"], rec["memory_mib"], rec["data_disk_gib"]), ("base", 8192, 50))

    def test_options_of_create_win_over_the_settings(self):
        self.ok("set-settings", "default_memory_mib=8192", "default_data_disk_gib=50")
        self.ok("create", "lab", "--memory-mib", "4096", "--data-disk-gib", "20")
        rec = self.record("lab")
        self.assertEqual((rec["memory_mib"], rec["data_disk_gib"]), (4096, 20))

    def test_the_default_image_must_exist_at_create(self):
        self.ok("set-settings", "default_image=rust")
        self.assertIn("No such Image: rust", self.refused("create", "lab"))

    def test_the_longest_open_web_comes_from_the_settings(self):
        with open(self.path("etc/agentbox/watchlist.json"), "w") as f:
            f.write('{"entries": []}\n')         # Open web needs a readable Watchlist
        self.assertIn("from 1 to 240", self.refused("open-web", "work", "241"))
        self.ok("set-settings", "open_web_max_minutes=60")
        self.assertIn("from 1 to 60", self.refused("open-web", "work", "61"))
        self.ok("open-web", "work", "60")
        self.ok("set-settings", "open_web_max_minutes=1000")
        self.ok("open-web", "work", "999")

    def disk(self, ctx, days):
        """A System disk on the current Build, so only its age makes a Reset due."""
        path = self.path(f"var/lib/agentbox/vms/{ctx}-system.qcow2")
        with open(path, "w") as f:
            json.dump({"backing-filename": "/var/lib/agentbox/images/base-20261101.qcow2"}, f)
        t = int(time.time() - days * 86400)
        os.utime(path, (t, t))

    def test_reset_age_comes_from_the_settings(self):
        self.scenario(vms={"work": {"state": "shut off"}, "priv": {"state": "shut off"}})
        self.disk("work", 3)
        self.disk("priv", 3)
        self.assertEqual(self.ok("weekly-check"), "")
        self.ok("set-settings", "reset_age_days=2")
        out = self.ok("weekly-check")
        self.assertIn("work: fresh System disk", out)
        self.assertIn("priv: fresh System disk", out)
        self.ok("set-settings", "reset_age_days=default")
        self.disk("work", 6)
        self.assertEqual(self.ok("weekly-check"), "")


class ActivityRecordTest(SettingsTest):
    def test_changing_settings_is_recorded_and_showing_them_is_not(self):
        journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(journald.close)
        self.ok("settings")
        self.assertEqual(journald.entries(), [])
        self.ok("--actor=Firefox/Linux (192.0.2.23)", "set-settings", "builds_kept=4")
        self.assertEqual(self.run_agentbox("set-settings", "builds_kept=99")[0], 1)
        entries = [e for e in journald.entries() if e["AGENTBOX_ACTION"] == "set-settings"]
        self.assertEqual([(e["AGENTBOX_KIND"], e["AGENTBOX_OUTCOME"]) for e in entries],
                         [("settings", "ok"), ("settings", "failed")])
        self.assertIn("builds_kept", entries[0]["AGENTBOX_DESCRIPTION"])
        self.assertEqual(entries[0]["AGENTBOX_ACTOR"], "Firefox/Linux (192.0.2.23)")


if __name__ == "__main__":
    unittest.main()
