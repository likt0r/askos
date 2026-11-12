"""The Maintenance window (#49): `agentbox maintenance`, its timer and its units.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and the fakes of
test_lifecycle.py, with fake_systemctl.py on PATH: `systemctl start` of a step's unit runs
fake_unit.py, which records its start and end in calls.jsonl next to the calls of the fakes (so
their order is the order things happened), and plays a slow Build that holds the run lock and
then makes a new Build current, or records a failed one.

Run from the repo root: python3 -m unittest discover -s tests
"""
import datetime
import fcntl
import json
import os
import sys
import time
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.dirname(TESTS))
from fake_journald import Journald  # noqa: E402
from test_lifecycle import CURRENT_BASE, LifecycleTest, fake  # noqa: E402
from test_vms import VmsTest  # noqa: E402
from agentbox import maintenance, settings  # noqa: E402

UNIT = [sys.executable, os.path.join(TESTS, "fake_unit.py")]
NEW = "20261112-000000"      # the Build a slow Build makes; newer than the current one, CURRENT_BASE
FEEDS, VERSIONS = "agentbox-feeds.service", "agentbox-versions.service"
UPDATE, ALL = "agentbox-build@update.service", "agentbox-build@all.service"
RESET = ("managedsave-remove", "agentbox-work")      # the first step of work's Reset


def today(offset=0):
    """The day of the week (as rebuild_day names it) `offset` days from today; TZ=UTC in the CLI."""
    return settings.DAYS[(time.gmtime().tm_wday + offset) % 7]


def hhmm(hours_ago):
    """The time of day (HH:MM, as maintenance_time names it) `hours_ago` hours ago; TZ=UTC in the CLI."""
    return time.strftime("%H:%M", time.gmtime(time.time() - hours_ago * 3600))


def stamp(hours_ago):
    """A time `hours_ago` hours ago as RECORD keeps when a window began."""
    return datetime.datetime.fromtimestamp(time.time() - hours_ago * 3600).astimezone().isoformat(timespec="seconds")


def days_ago(days):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).date().isoformat()


class MaintenanceTest(LifecycleTest):
    def setUp(self):
        super().setUp()
        fake(os.path.join(self.dir, "bin", "systemctl"), "fake_systemctl.py")
        os.makedirs(self.path("etc/systemd/system"))
        open(self.path("etc/systemd/system/agentbox-build@.service"), "w").close()     # regenerate wrote it
        self.journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.journald.close)
        self.env["AGENTBOX_MAINTENANCE_POLL_SECONDS"] = "0"
        # a window at 00:00, so today's has begun; no rebuild unless a test says so
        self.settings(maintenance_time="00:00", rebuild_day=today(3))
        # work is off, its System disk a day old on the current Build: only a newer Build makes
        # the Reset check reset it; priv has no System disk
        self.disk("work", days=1, backing=CURRENT_BASE)

    def settings(self, **values):
        self.write("etc/agentbox/settings.json", json.dumps(values))

    def units(self, vms=None, **run):
        """What `systemctl start <unit>` runs: {unit: extra arguments of fake_unit.py}; every
        step's unit runs and succeeds unless named."""
        run = {FEEDS: [], VERSIONS: [], **run}
        self.scenario(vms=vms or {"work": {"state": "shut off"}},
                      systemctl_run={u: UNIT + [u.split(".")[0], *args] for u, args in run.items()})

    def window(self, code=0, *args, by_hand=False):
        got, out, err = self.run_agentbox(*([] if by_hand else ["--actor=timer"]), "maintenance", *args)
        self.assertEqual(got, code, out + err)
        return out, err

    def record(self):
        try:
            with open(self.path("var/lib/agentbox/maintenance.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def entries(self):
        return [(e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"]) for e in self.journald.entries()]

    def order(self):
        """What happened, in order: the units started (systemctl start), what they ran (start
        and end), what changed the VMs; the looks at the Builds (systemctl is-active) and the
        VMs (virsh domstate) left out."""
        out = []
        for c in self.calls():
            if c["cmd"] == "systemctl" and c["argv"][0] == "start":
                out.append(("start", c["argv"][-1]))
            elif c["cmd"] == "unit":
                out.append(tuple(c["argv"]))
            elif "dom" in c:
                out.append((c["cmd"], c["dom"]))
        return out

    def backing(self, vm):
        with open(self.system_disk(vm)) as f:
            return os.path.basename(json.load(f)["backing-filename"])


class StepTest(MaintenanceTest):
    def test_the_steps_run_one_after_another_and_each_is_recorded(self):
        self.settings(maintenance_time="00:00", rebuild_day=today())
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.3", "--build", "base", NEW, "--target", "all"]})
        out, _ = self.window()
        self.assertEqual(self.order(), [
            ("start", FEEDS), ("agentbox-feeds", "start"), ("agentbox-feeds", "end"),
            ("start", VERSIONS), ("agentbox-versions", "start"), ("agentbox-versions", "end"),
            ("start", ALL), ("agentbox-build@all", "start"), ("agentbox-build@all", "end"),
            # the Reset check after the weekly rebuild: work goes onto its new Build
            ("managedsave-remove", "agentbox-work")])
        self.assertEqual(self.backing("work"), f"base-{NEW}.qcow2")
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "ok"),
                                          ("maintenance.rebuild", "ok"), ("reset.automatic", "ok"),
                                          ("maintenance.reset-check", "ok")])
        self.assertEqual({e["AGENTBOX_ACTOR"] for e in self.journald.entries()}, {"timer"})
        self.assertEqual([e["AGENTBOX_DESCRIPTION"] for e in self.journald.entries()
                          if e["AGENTBOX_ACTION"].startswith("maintenance.")],
                         ["Maintenance window: Feeds fetched", "Maintenance window: version check done",
                          "Maintenance window: weekly rebuild of every Image done",
                          "Maintenance window: Reset check done"])
        self.assertIn("== Maintenance window: Reset check\nwork: fresh System disk", out)

    def test_json(self):
        # the update the version check starts makes a new Build, so the Reset check resets work,
        # which in text says so on stdout: with --json, stdout is the JSON result alone
        self.units(**{FEEDS: ["--exit", "1"], VERSIONS: ["--start", UPDATE],
                      UPDATE: ["--hold", "0.2", "--build", "base", NEW]})
        code, out, _ = self.run_agentbox("--actor=timer", "maintenance", "--json")
        self.assertEqual(code, 1)
        self.assertIn(RESET, self.order())
        self.assertEqual(self.backing("work"), f"base-{NEW}.qcow2")
        self.assertEqual(out.count("\n"), 1, out)
        result = json.loads(out)
        self.assertFalse(result["already_ran"])
        steps = result["steps"]
        self.assertEqual([(r["step"], r["outcome"]) for r in steps],
                         [("feeds", "failed"), ("versions", "ok"), ("rebuild", "skipped"), ("reset-check", "ok")])
        self.assertIn(f"systemctl start {FEEDS} failed", steps[0]["message"])
        self.assertIsNone(steps[1]["message"])


class WaitTest(MaintenanceTest):
    def test_the_reset_check_waits_for_the_builds_the_version_check_started(self):
        # the version check starts an automatic update (as check-versions does with auto_update)
        # and ends at once; the update's Build takes its time, then becomes the current one
        self.units(**{VERSIONS: ["--start", UPDATE], UPDATE: ["--hold", "1.5", "--build", "base", NEW]})
        self.window()
        order = self.order()
        self.assertLess(order.index(("agentbox-versions", "end")), order.index(("agentbox-build@update", "end")))
        self.assertLess(order.index(("agentbox-build@update", "end")), order.index(RESET))
        # so the Reset check moved work onto the Build just made
        self.assertEqual(self.backing("work"), f"base-{NEW}.qcow2")
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "ok"),
                                          ("reset.automatic", "ok"), ("maintenance.reset-check", "ok")])

    def test_and_for_the_weekly_rebuild(self):
        self.settings(maintenance_time="00:00", rebuild_day=today())
        self.units(**{ALL: ["--take-request", "all", "--hold", "1.5", "--build", "base", NEW, "--target", "all"]})
        self.window()
        order = self.order()
        self.assertLess(order.index(("agentbox-build@all", "end")), order.index(RESET))
        self.assertEqual(self.backing("work"), f"base-{NEW}.qcow2")

    def test_a_build_that_has_not_taken_the_lock_yet_counts(self):
        # its unit runs, but takes the run lock only a second later: the window waits for the unit
        self.units(**{VERSIONS: ["--start", UPDATE], UPDATE: ["--delay", "1", "--hold", "0.2", "--build", "base", NEW]})
        self.window()
        order = self.order()
        self.assertLess(order.index(("agentbox-build@update", "end")), order.index(RESET))
        self.assertEqual(self.backing("work"), f"base-{NEW}.qcow2")

    def test_a_queued_start_job_counts_while_its_request_file_is_fresh(self):
        # `systemctl start --no-block` queued the Build's unit: it is not active for the first
        # polls, and only the request file start() wrote shows that a Build is about to run
        self.settings(maintenance_time="00:00", rebuild_day=today())
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.2", "--build", "base", NEW, "--target", "all"]})
        self.scenario(vms={"work": {"state": "shut off"}}, systemctl_queue={ALL: 6},
                      systemctl_run={u: UNIT + [u.split(".")[0], *args] for u, args in
                                     {FEEDS: [], VERSIONS: [], ALL: ["--take-request", "all", "--hold", "0.2",
                                                                      "--build", "base", NEW, "--target", "all"]}.items()})
        self.window()
        order = self.order()
        self.assertLess(order.index(("agentbox-build@all", "end")), order.index(RESET))
        self.assertEqual(self.backing("work"), f"base-{NEW}.qcow2")

    def test_a_failed_build_moves_no_vm_and_fails_its_step(self):
        self.units(**{VERSIONS: ["--start", UPDATE], UPDATE: ["--hold", "0.5", "--fail-build", "base", NEW]})
        _, err = self.window(1)
        self.assertEqual(self.backing("work"), os.path.basename(CURRENT_BASE))
        self.assertEqual(self.vm_calls(), [])
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "failed"),
                                          ("maintenance.reset-check", "ok")])
        [failed] = [e for e in self.journald.entries() if e["AGENTBOX_OUTCOME"] == "failed"]
        self.assertEqual(failed["AGENTBOX_DESCRIPTION"],
                         f"Maintenance window: version check failed: the Build of base-{NEW} failed")
        self.assertEqual(failed["AGENTBOX_ERROR"], "action_failed")

    def test_a_failed_build_of_the_weekly_rebuild_moves_no_vm_and_fails_its_step(self):
        self.settings(maintenance_time="00:00", rebuild_day=today())
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.5", "--fail-build", "base", NEW, "--target", "all"]})
        self.window(1)
        self.assertIn(("agentbox-build@all", "end"), self.order())
        self.assertEqual(self.backing("work"), os.path.basename(CURRENT_BASE))
        self.assertEqual(self.vm_calls(), [])
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "ok"),
                                          ("maintenance.rebuild", "failed"), ("maintenance.reset-check", "ok")])
        [failed] = [e for e in self.journald.entries() if e["AGENTBOX_OUTCOME"] == "failed"]
        self.assertEqual(failed["AGENTBOX_DESCRIPTION"],
                         f"Maintenance window: weekly rebuild of every Image failed: the Build of base-{NEW} failed")

    def test_it_waits_also_when_the_version_check_fails_after_starting_a_build(self):
        # the version check starts the update's Build and then fails: the Reset check still waits
        # for that Build, and moves work onto it once it is current
        self.units(**{VERSIONS: ["--start", UPDATE, "--exit", "1"], UPDATE: ["--hold", "1", "--build", "base", NEW]})
        self.window(1)
        order = self.order()
        self.assertLess(order.index(("agentbox-versions", "end")), order.index(("agentbox-build@update", "end")))
        self.assertLess(order.index(("agentbox-build@update", "end")), order.index(RESET))
        self.assertEqual(self.backing("work"), f"base-{NEW}.qcow2")
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "failed"),
                                          ("reset.automatic", "ok"), ("maintenance.reset-check", "ok")])

    def test_a_failed_build_of_another_actor_s_request_is_not_the_window_s(self):
        # the update that ran while the window waited was the owner's Update now (its Build
        # record names the actor of its request): its failure is not the version check's
        self.units(**{VERSIONS: ["--start", UPDATE],
                      UPDATE: ["--hold", "0.3", "--fail-build", "base", NEW, "--actor", "owner", "--exit", "1"]})
        self.window()
        self.assertIn(("agentbox-build@update", "end"), self.order())
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "ok"),
                                          ("maintenance.reset-check", "ok")])

    def test_an_update_that_fails_before_it_begins_a_build_fails_the_step(self):
        # the update's unit fails without a Build record (before it began one): systemd's
        # result of the unit's run since the step began says so
        self.units(**{VERSIONS: ["--start", UPDATE], UPDATE: ["--delay", "0.3", "--exit", "1"]})
        self.window(1)
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "failed"),
                                          ("maintenance.reset-check", "ok")])
        failed = self.journald.entries()[1]
        self.assertEqual(failed["AGENTBOX_DESCRIPTION"], f"Maintenance window: version check failed: {UPDATE} failed "
                                                         f"before it began a Build (journalctl -u {UPDATE})")
        self.assertEqual(failed["AGENTBOX_ERROR_PARAM_UNIT"], UPDATE)

    def test_a_failed_run_of_the_update_before_the_step_does_not_count(self):
        os.makedirs(os.path.join(self.dir, "units"))
        with open(os.path.join(self.dir, "units", UPDATE + ".json"), "w") as f:
            json.dump({"InactiveExitTimestamp": f"@{int(time.time()) - 3600}", "Result": "exit-code"}, f)
        self.units()
        self.window()
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "ok"),
                                          ("maintenance.reset-check", "ok")])


class FailingStepTest(MaintenanceTest):
    def test_a_failing_step_is_recorded_and_the_next_one_runs(self):
        self.units(**{FEEDS: ["--exit", "1"]})
        self.window(1)
        self.assertEqual(self.entries(), [("maintenance.feeds", "failed"), ("maintenance.versions", "ok"),
                                          ("maintenance.reset-check", "ok")])
        failed = self.journald.entries()[0]
        self.assertEqual((failed["AGENTBOX_ACTOR"], failed["AGENTBOX_KIND"], failed["AGENTBOX_ERROR"],
                          failed["AGENTBOX_ERROR_PARAM_UNIT"]), ("timer", "action", "action_failed", FEEDS))
        self.assertTrue(failed["AGENTBOX_DESCRIPTION"].startswith(
            f"Maintenance window: fetching the Feeds failed: systemctl start {FEEDS} failed"))
        self.assertIn(("agentbox-versions", "end"), self.order())

    def test_every_step_fails_on_its_own(self):
        # broken Settings fail the rebuild (which reads them) and the Reset check of each VM,
        # not the Feeds and the version check
        self.write("etc/agentbox/settings.json", "{broken")
        self.units(**{VERSIONS: ["--exit", "3"]})
        self.window(1)
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "failed"),
                                          ("maintenance.rebuild", "failed"), ("reset.failed", "failed"),
                                          ("reset.failed", "failed"), ("maintenance.reset-check", "failed")])
        self.assertIn("failed for work, priv", self.journald.entries()[-1]["AGENTBOX_DESCRIPTION"])

    def test_two_windows_never_run_at_once(self):
        self.units()
        os.makedirs(self.path("var/lib/agentbox/locks"), exist_ok=True)
        with open(self.path("var/lib/agentbox/locks/maintenance.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _, err = self.window(1)
        self.assertIn("a Maintenance window is running already", err)
        self.assertEqual(self.order(), [])


class RebuildDayTest(MaintenanceTest):
    def test_only_on_the_rebuild_day(self):
        self.units()
        self.window()
        self.assertNotIn(("start", ALL), self.order())
        self.assertNotIn("maintenance.rebuild", [a for a, _ in self.entries()])
        self.assertNotIn("rebuild", self.record())

    def test_a_missed_rebuild_day_is_caught_up_once(self):
        # the last window on yesterday's rebuild day did not run (the Host was off)
        self.settings(maintenance_time="00:00", rebuild_day=today(-1))
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"rebuild": days_ago(8)}))
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.1", "--target", "all"]})
        self.window()
        self.assertIn(("start", ALL), self.order())
        date = time.strftime("%Y-%m-%d", time.gmtime())
        self.assertEqual(self.record()["rebuild"], date)
        self.assertEqual(self.record()["timer_window"][:10], date)
        # another window the same day (by hand) does not rebuild again
        self.forget()
        self.window(by_hand=True)
        self.assertIn(("start", VERSIONS), self.order())
        self.assertNotIn(("start", ALL), self.order())

    def test_a_rebuild_that_does_not_start_is_tried_again_by_the_next_window(self):
        self.settings(maintenance_time="00:00", rebuild_day=today())
        self.units(**{ALL: ["--exit", "1"]})
        self.scenario(vms={"work": {"state": "shut off"}}, systemctl_fails=["start"])
        self.window(1)
        self.assertEqual(self.entries(), [("maintenance.feeds", "failed"), ("maintenance.versions", "failed"),
                                          ("maintenance.rebuild", "failed"), ("maintenance.reset-check", "ok")])
        # not started: the next window tries again
        self.assertNotIn("rebuild", self.record())

    def test_a_day_that_only_became_the_rebuild_day_is_no_missed_one(self):
        # rebuilt three days ago; rebuild_day changed today to yesterday's day: yesterday was no
        # rebuild day when its window ran, so this window does not rebuild
        self.settings(maintenance_time="00:00", rebuild_day=today(-1))
        self.write("var/lib/agentbox/maintenance.json",
                   json.dumps({"rebuild": days_ago(3), "rebuild_day_changed": days_ago(0)}))
        self.units()
        self.window()
        self.assertNotIn(("start", ALL), self.order())
        self.assertEqual(self.record()["rebuild"], days_ago(3))
        self.assertEqual(self.record()["rebuild_day_changed"], days_ago(0))
        # changed before that day, it was a rebuild day that was missed
        self.write("var/lib/agentbox/maintenance.json",
                   json.dumps({"rebuild": days_ago(3), "rebuild_day_changed": days_ago(2)}))
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.1", "--target", "all"]})
        self.window(by_hand=True)
        self.assertIn(("start", ALL), self.order())

    def test_the_old_rebuild_timer_still_there_rebuilds_instead(self):
        # the RPM could not disable agentbox-rebuild.timer: it stays until regenerate and runs
        # its own Build of every Image on the rebuild day; the window starts none to collide with it
        self.settings(maintenance_time="00:00", rebuild_day=today())
        open(self.path("etc/systemd/system/agentbox-rebuild.timer"), "w").close()
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.1", "--target", "all"]})
        code, out, _ = self.run_agentbox("--actor=timer", "maintenance", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn(("start", ALL), self.order())
        message = ("weekly rebuild left to agentbox-rebuild.timer, which is still there (sudo agentbox regenerate "
                   "takes it away)")
        self.assertEqual(json.loads(out)["steps"][2], {"step": "rebuild", "outcome": "skipped", "message": message})
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "ok"),
                                          ("maintenance.rebuild", "ok"), ("maintenance.reset-check", "ok")])
        entry = self.journald.entries()[2]
        self.assertEqual((entry["AGENTBOX_DESCRIPTION"], entry["AGENTBOX_PARAM_REASON"]),
                         (f"Maintenance window: {message}", "old-timer"))
        # the day counts as rebuilt: no catch-up once regenerate took the old timer away
        self.assertEqual(self.record()["rebuild"], days_ago(0))

    def forget(self):
        os.remove(os.path.join(self.dir, "calls.jsonl"))


class OneWindowPerTurnTest(MaintenanceTest):
    """The timer runs one window per turn of maintenance_time: a window it starts does nothing
    when its window of the latest turn began already; a late catch-up counts for the turn it
    catches up, and a window by hand never counts."""

    def forget(self):
        os.remove(os.path.join(self.dir, "calls.jsonl"))

    def test_the_timer_runs_one_window_per_turn(self):
        at = hhmm(1)
        self.settings(maintenance_time=at, rebuild_day=today(3))
        self.units()
        self.window()
        self.assertEqual(list(self.record()), ["timer_window"])
        began = self.record()["timer_window"]
        self.forget()
        out, _ = self.window()
        self.assertIn(f"Maintenance window not started: the window of {at} ran already", out)
        self.assertEqual(self.order(), [])
        skipped = self.journald.entries()[-1]
        self.assertEqual((skipped["AGENTBOX_ACTION"], skipped["AGENTBOX_OUTCOME"], skipped["AGENTBOX_ACTOR"],
                          skipped["AGENTBOX_PARAM_REASON"], skipped["AGENTBOX_PARAM_TIME"]),
                         ("maintenance.skipped", "ok", "timer", "already-ran", at))
        code, out, _ = self.run_agentbox("--actor=timer", "maintenance", "--json")
        self.assertEqual((code, json.loads(out)), (0, {"steps": [], "already_ran": True}))
        self.assertEqual([a for a, _ in self.entries()].count("maintenance.skipped"), 2)
        # by hand it runs all the same, and does not count for the timer
        self.window(by_hand=True)
        self.assertIn(("start", FEEDS), self.order())
        self.assertEqual(self.record()["timer_window"], began)

    def test_a_window_by_hand_does_not_make_the_timer_s_skip(self):
        self.settings(maintenance_time=hhmm(1), rebuild_day=today(3))
        self.units()
        self.window(by_hand=True)
        self.assertEqual(self.record(), {})
        self.forget()
        self.window()
        self.assertIn(("start", FEEDS), self.order())

    def test_a_window_of_the_turn_before_does_not_count(self):
        self.settings(maintenance_time=hhmm(1), rebuild_day=today(3))
        self.units()
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"timer_window": stamp(24), "rebuild": days_ago(1)}))
        self.window()
        self.assertIn(("start", FEEDS), self.order())
        self.assertEqual(self.record()["rebuild"], days_ago(1))
        self.assertGreater(datetime.datetime.fromisoformat(self.record()["timer_window"]).timestamp(),
                           time.time() - 3600)

    def test_a_late_catch_up_does_not_make_the_next_turn_skip(self):
        # maintenance_time 02:00, the Host asleep until 15:00: the window of 02:00 ran at 15:00;
        # the next turn, 11 hours later at 02:00, runs
        self.settings(maintenance_time=hhmm(0), rebuild_day=today(3))
        self.units()
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"timer_window": stamp(11)}))
        self.window()
        self.assertIn(("start", FEEDS), self.order())

    def test_a_catch_up_after_midnight_runs_and_so_does_the_next_turn(self):
        # maintenance_time 23:00, the Host off from 22:00 to 00:30: Monday's window runs at 00:30
        # on Tuesday, though Monday's 23:00 window of the turn before began 25.5 hours ago ...
        self.settings(maintenance_time=hhmm(1.5), rebuild_day=today(3))
        self.units()
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"timer_window": stamp(25.5)}))
        self.window()
        self.assertIn(("start", FEEDS), self.order())
        # ... and Tuesday's regular window at 23:00, 22.5 hours after the catch-up, runs too
        self.forget()
        self.settings(maintenance_time=hhmm(0), rebuild_day=today(3))
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"timer_window": stamp(22.5)}))
        self.window()
        self.assertIn(("start", FEEDS), self.order())

    def test_a_second_start_in_the_same_turn_is_skipped(self):
        self.settings(maintenance_time=hhmm(3), rebuild_day=today(3))
        self.units()
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"timer_window": stamp(2)}))
        out, _ = self.window()
        self.assertIn("ran already", out)
        self.assertEqual(self.order(), [])
        self.assertEqual(self.entries(), [("maintenance.skipped", "ok")])


class TransitionTest(MaintenanceTest):
    """A Host updated from before the Maintenance window: its old Reset timer (agentbox-weekly.timer,
    `agentbox --actor=timer weekly-check`) runs the whole window until regenerate writes the
    window's timer."""

    def setUp(self):
        super().setUp()
        for name in ("agentbox-weekly.timer", "agentbox-weekly.service", "agentbox-feeds.timer"):
            open(self.path("etc/systemd/system/" + name), "w").close()

    def weekly_check(self, *args, actor="timer"):
        code, out, err = self.run_agentbox(f"--actor={actor}", "weekly-check", *args)
        self.assertEqual(code, 0, out + err)
        return out

    def test_the_old_reset_timer_runs_the_whole_window_until_regenerate(self):
        self.units()
        out = self.weekly_check()
        self.assertIn("agentbox-maintenance.timer is missing", out)
        self.assertEqual(self.order(), [
            ("start", FEEDS), ("agentbox-feeds", "start"), ("agentbox-feeds", "end"),
            ("start", VERSIONS), ("agentbox-versions", "start"), ("agentbox-versions", "end")])
        self.assertEqual(self.entries(), [("maintenance.feeds", "ok"), ("maintenance.versions", "ok"),
                                          ("maintenance.reset-check", "ok")])
        self.assertEqual({e["AGENTBOX_ACTOR"] for e in self.journald.entries()}, {"timer"})

    def test_the_rebuild_runs_at_an_old_reset_time_before_the_migrated_maintenance_time(self):
        # reset_time 01:00 migrates to maintenance_time 23:00 (the evening before), but until
        # regenerate the window runs at 01:00: on the rebuild day it still rebuilds
        self.write("etc/agentbox/settings.json", json.dumps({"reset_time": "01:00", "rebuild_day": today()}))
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.1", "--target", "all"]})
        self.weekly_check()
        self.assertIn(("start", ALL), self.order())
        self.assertEqual(self.record()["rebuild"], time.strftime("%Y-%m-%d", time.gmtime()))

    def test_a_missed_rebuild_day_is_caught_up_once_the_day_after(self):
        self.write("etc/agentbox/settings.json", json.dumps({"reset_time": "01:00", "rebuild_day": today(-1)}))
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"rebuild": days_ago(8)}))
        self.units(**{ALL: ["--take-request", "all", "--hold", "0.1", "--target", "all"]})
        self.weekly_check()
        self.assertIn(("start", ALL), self.order())
        # the next window, not before the next rebuild day, does not rebuild again
        os.remove(os.path.join(self.dir, "calls.jsonl"))
        self.window(by_hand=True)
        self.assertIn(("start", VERSIONS), self.order())
        self.assertNotIn(("start", ALL), self.order())

    def test_with_json_it_is_the_window_s_result(self):
        self.units()
        self.assertEqual([r["step"] for r in json.loads(self.weekly_check("--json"))["steps"]],
                         ["feeds", "versions", "rebuild", "reset-check"])

    def test_never_twice_in_a_turn(self):
        self.units()
        self.weekly_check()
        os.remove(os.path.join(self.dir, "calls.jsonl"))
        self.assertIn("ran already", self.weekly_check())
        self.assertEqual(self.order(), [])
        # nor once regenerate wrote the window's timer, in the same turn
        open(self.path("etc/systemd/system/agentbox-maintenance.timer"), "w").close()
        self.window()
        self.assertEqual(self.order(), [])
        self.assertEqual([a for a, _ in self.entries()][3:], ["maintenance.skipped", "maintenance.skipped"])

    def test_the_window_at_the_old_reset_time_counts_for_the_turn_two_hours_before(self):
        # reset_time now: maintenance_time two hours ago. The old timer's window of yesterday
        # does not make today's skip, today's does make a second start skip
        reset = hhmm(0)
        self.write("etc/agentbox/settings.json", json.dumps({"reset_time": reset, "rebuild_day": today(3)}))
        self.write("var/lib/agentbox/maintenance.json", json.dumps({"timer_window": stamp(24)}))
        self.units()
        self.weekly_check()
        self.assertIn(("start", FEEDS), self.order())
        os.remove(os.path.join(self.dir, "calls.jsonl"))
        hours, minutes = map(int, reset.split(":"))
        at = f"{(hours - 2) % 24:02d}:{minutes:02d}"
        self.assertIn(f"the window of {at} ran already", self.weekly_check())
        self.assertEqual(self.order(), [])

    def test_after_regenerate_it_is_the_reset_check_alone(self):
        open(self.path("etc/systemd/system/agentbox-maintenance.timer"), "w").close()
        self.units()
        self.assertEqual(self.weekly_check(), "")
        self.assertEqual(self.order(), [])
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.record(), {})

    def test_by_hand_it_is_the_reset_check_alone(self):
        self.units()
        self.disk("work", days=200, backing=CURRENT_BASE)
        self.assertIn("work: ", self.weekly_check(actor="owner"))
        self.assertEqual(self.order(), [RESET])
        self.assertEqual(self.entries(), [("reset.automatic", "ok")])


class WarningTest(MaintenanceTest):
    """`agentbox status` says when the Maintenance window's timer is missing on a Host regenerate
    set up (after an update from before the window, until regenerate runs)."""

    def unit(self, *names):
        for name in names:
            open(self.path("etc/systemd/system/" + name), "w").close()

    def status(self, *args):
        self.scenario(vms={"work": {"state": "shut off"}})
        code, out, err = self.run_agentbox("status", *args)
        self.assertEqual(code, 0, err)
        return out

    def test_missing_after_an_update_from_before_the_window(self):
        self.unit("agentbox-weekly.timer", "agentbox-feeds.timer")
        out = self.status()
        self.assertTrue(out.endswith(
            "\nWarning: The timer of the Maintenance window (agentbox-maintenance.timer) is missing: sudo agentbox "
            "regenerate creates it; until then the old Reset check's timer runs the window at the old Reset time\n"),
            out)
        data = json.loads(self.status("--json"))
        self.assertEqual(list(data), ["work", "priv", "host_warnings"])
        [warning] = data["host_warnings"]
        self.assertEqual(warning["code"], "maintenance_timer_missing")
        self.assertIn("sudo agentbox regenerate", warning["message"])

    def test_missing_without_the_old_timer_either(self):
        self.unit("agentbox-feeds.timer")
        [warning] = json.loads(self.status("--json"))["host_warnings"]
        self.assertEqual(warning["message"], "The timer of the Maintenance window (agentbox-maintenance.timer) is "
                                             "missing: sudo agentbox regenerate creates it")

    def test_none_once_regenerate_wrote_it_or_before_setup(self):
        self.assertNotIn("Warning", self.status())
        self.assertEqual(list(json.loads(self.status("--json"))), ["work", "priv"])
        self.unit("agentbox-weekly.timer", "agentbox-maintenance.timer")
        self.assertNotIn("Warning", self.status())
        self.assertNotIn("host_warnings", json.loads(self.status("--json")))


class RebuildDueTest(unittest.TestCase):
    """maintenance.rebuild_due, in local time and by dates alone: a window catches up a missed
    rebuild day once, whatever the clock time it begins at."""

    def at(self, *args):
        return datetime.datetime(*args).timestamp()

    def test_due(self):
        sun, sat = datetime.date(2026, 11, 15), datetime.date(2026, 11, 14)     # 2026-11-15 is a Sunday
        prev = datetime.date(2026, 11, 8)
        for now, last, due in (
                ((2026, 11, 15, 2, 0), None, True),         # the window of the rebuild day
                ((2026, 11, 15, 0, 30), None, True),        # at any time of that day
                ((2026, 11, 15, 23, 0), None, True),
                ((2026, 11, 16, 8, 0), None, False),        # never a record: only the rebuild day
                ((2026, 11, 15, 2, 0), prev, True),
                ((2026, 11, 15, 9, 0), sun, False),         # once that day, also at another time
                ((2026, 11, 16, 8, 0), prev, True),         # Sunday's window missed: caught up on Monday
                ((2026, 11, 16, 8, 0), sun, False),
                ((2026, 11, 20, 8, 0), sun, False),         # not again until the next Sunday
                ((2026, 11, 22, 1, 0), sun, True),
                ((2026, 11, 14, 2, 0), prev, False),        # Saturday
                ((2026, 11, 14, 2, 0), sat, False),
                ((2026, 12, 2, 2, 0), prev, True),          # three rebuild days missed: one rebuild
                ((2026, 12, 3, 2, 0), datetime.date(2026, 12, 2), False),
                ((2026, 12, 6, 2, 0), datetime.date(2026, 12, 2), True)):
            with self.subTest(now=now, last=last):
                self.assertEqual(maintenance.rebuild_due(self.at(*now), "sun", last), due)

    def test_a_changed_rebuild_day(self):
        wed, mon = datetime.date(2026, 11, 11), datetime.date(2026, 11, 16)
        for now, changed, due in (
                # rebuilt on Wednesday, rebuild_day changed from wed to sun on Monday: the Sunday
                # before was no rebuild day, Tuesday does not rebuild; the next Sunday does
                ((2026, 11, 17, 2, 0), mon, False),
                ((2026, 11, 22, 2, 0), mon, True),
                # changed on Friday: that Sunday's window was missed, Monday catches it up
                ((2026, 11, 16, 2, 0), datetime.date(2026, 11, 13), True),
                # changed on the rebuild day itself: its window rebuilds
                ((2026, 11, 15, 2, 0), datetime.date(2026, 11, 15), True),
                ((2026, 11, 16, 2, 0), datetime.date(2026, 11, 15), False)):
            with self.subTest(now=now, changed=changed):
                self.assertEqual(maintenance.rebuild_due(self.at(*now), "sun", wed, changed), due)


class TurnTest(unittest.TestCase):
    """maintenance.turn_ran, in local time: whether the timer's window of the latest turn of
    maintenance_time began already."""

    def at(self, *args):
        return datetime.datetime(*args).timestamp()

    def test_turns(self):
        for at, now, last, ran in (
                ("02:00", (2026, 11, 16, 2, 0), None, False),                   # never a window
                ("02:00", (2026, 11, 16, 2, 0), (2026, 11, 15, 2, 0), False),   # yesterday's
                ("02:00", (2026, 11, 16, 2, 5), (2026, 11, 16, 2, 0), True),    # a second start
                ("02:00", (2026, 11, 16, 1, 59), (2026, 11, 15, 2, 0), True),   # before today's turn
                # a late catch-up: the Host asleep until 15:00, the next 02:00 runs
                ("02:00", (2026, 11, 16, 15, 0), (2026, 11, 15, 2, 0), False),
                ("02:00", (2026, 11, 17, 2, 0), (2026, 11, 16, 15, 0), False),
                ("02:00", (2026, 11, 16, 15, 5), (2026, 11, 16, 15, 0), True),
                # across midnight: Monday's 23:00 caught up at 00:30, Tuesday's 23:00 runs
                ("23:00", (2026, 11, 17, 0, 30), (2026, 11, 15, 23, 0), False),
                ("23:00", (2026, 11, 17, 0, 30), (2026, 11, 16, 23, 0), True),
                ("23:00", (2026, 11, 17, 23, 0), (2026, 11, 17, 0, 30), False),
                # until regenerate the old timer at reset_time = maintenance_time + 2 h
                ("02:00", (2026, 11, 17, 4, 0), (2026, 11, 16, 4, 0), False),
                ("02:00", (2026, 11, 17, 4, 1), (2026, 11, 17, 4, 0), True),
                ("02:00", (2026, 11, 17, 10, 0), (2026, 11, 17, 4, 0), True),   # regenerate's timer, same turn
                ("02:00", (2026, 11, 18, 2, 0), (2026, 11, 17, 4, 0), False),
                ("23:00", (2026, 11, 17, 1, 0), (2026, 11, 16, 1, 0), False),   # reset_time 01:00
                ("23:00", (2026, 11, 17, 23, 0), (2026, 11, 17, 1, 0), False),
                # a window "after now" only a clock set back shows: no reason to skip
                ("02:00", (2026, 11, 16, 3, 0), (2026, 11, 16, 9, 0), False)):
            with self.subTest(at=at, now=now, last=last):
                self.assertEqual(maintenance.turn_ran(self.at(*now), at, last and self.at(*last)), ran)


class TimerTest(VmsTest):
    """The units `agentbox regenerate` writes for the window, and the timers it replaced."""

    def unit(self, name):
        return self.read("etc/systemd/system/" + name)

    def test_one_daily_timer_that_catches_up_a_missed_window_once(self):
        self.ok("regenerate")
        timer = self.unit("agentbox-maintenance.timer")
        # one calendar time a day and Persistent: systemd runs a window missed while the Host was
        # off once after boot, however many days it missed; nothing else starts it, and waking a
        # sleeping Host is the Host's business
        self.assertEqual([line for line in timer.splitlines() if line.startswith("On")], ["OnCalendar=*-*-* 02:00"])
        self.assertIn("Persistent=true\n", timer)
        self.assertNotIn("WakeSystem", timer)
        self.assertNotIn("RandomizedDelaySec", timer)
        self.assertIn("WantedBy=timers.target\n", timer)
        service = self.unit("agentbox-maintenance.service")
        for line in ("Type=oneshot", "ExecStart=/usr/bin/agentbox --actor=timer maintenance",
                     "TimeoutStartSec=infinity", "After=network-online.target"):
            self.assertIn(line + "\n", service)
        self.assertNotIn("[Install]", service)
        self.assertIn(["enable", "--now", "agentbox-maintenance.timer"], self.systemctl())
        # the Feeds keep their interval timer, for a Host that is awake for long, and the boot
        self.assertIn("OnBootSec=10min\n", self.unit("agentbox-feeds.timer"))
        self.assertIn("OnUnitActiveSec=12h\n", self.unit("agentbox-feeds.timer"))

    def test_the_timers_it_replaced_are_taken_away(self):
        old = ("agentbox-weekly.timer", "agentbox-weekly.service", "agentbox-rebuild.timer",
               "agentbox-rebuild.service", "agentbox-versions.timer")
        os.makedirs(self.path("etc/systemd/system"), exist_ok=True)
        for name in old:
            self.write("etc/systemd/system/" + name, "[Unit]\n")
        self.ok("regenerate")
        for name in old:
            self.assertFalse(self.exists("etc/systemd/system/" + name), name)
        calls = self.systemctl()
        disable = calls.index(["disable", "--now", "agentbox-weekly.timer", "agentbox-rebuild.timer",
                               "agentbox-versions.timer"])
        self.assertEqual(calls[disable + 1], ["daemon-reload"])
        self.assertTrue(self.exists("etc/systemd/system/agentbox-versions.service"))
        self.forget_calls()
        self.ok("regenerate")
        self.assertNotIn("disable", [c[0] for c in self.systemctl()])

    def test_a_failed_disable_is_retried_by_the_next_regenerate(self):
        os.makedirs(self.path("etc/systemd/system"), exist_ok=True)
        self.write("etc/systemd/system/agentbox-weekly.timer", "[Unit]\n")
        self.scenario(systemctl_fails=["disable"])
        self.assertEqual(self.run_agentbox("regenerate")[0], 1)
        self.assertTrue(self.exists("etc/systemd/system/agentbox-weekly.timer"))
        self.scenario()
        self.ok("regenerate")
        self.assertFalse(self.exists("etc/systemd/system/agentbox-weekly.timer"))


if __name__ == "__main__":
    unittest.main()
