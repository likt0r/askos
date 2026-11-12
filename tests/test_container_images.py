"""#62: the hourly Container image cleanup in the running VMs (ADR 0017).

The real CLI runs with AGENTBOX_ROOT and fake virsh (fake_virsh.py) as in test_import.py. For
guest-exec of agentbox-container-images the fake runs the real wrapper of vm/files in the VM's
own root (FAKE_VIRSH_DIR/guest-<vm>), with a fake rootless Podman there (fake_guest_podman.py:
its Container images, containers and volumes in podman.json) and the usage file in the agent's
Podman storage: what is left there is what the VM would keep. The usage recorder (`record`) is
run directly, fed recorded `podman events` output (RecorderTest).

Run from the repo root: python3 -m unittest discover -s tests
"""
import datetime
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_activity import AGENTBOX, ActivityTest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER_PATH = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-container-images"
WRAPPER = os.path.join(REPO, "vm", "files", WRAPPER_PATH.lstrip("/"))
FAKE_PODMAN = os.path.join(REPO, "tests", "fake_guest_podman.py")
USAGE = "var/home/agent/.local/share/containers/agentbox-container-image-usage.json"
UTC = datetime.timezone.utc


def iid(n):
    """An image id (64 hex digits)."""
    return f"{n:064x}"


def cid(n):
    """A container id."""
    return f"c{n:063x}"


def at(days_ago, hour=12, minute=0, second=0):
    """Epoch seconds on the calendar day `days_ago` days before today (UTC, the VM's zone here)."""
    day = datetime.datetime.now(UTC).date() - datetime.timedelta(days=days_ago)
    return int(datetime.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC).timestamp())


def image(n, size=1000, names=None, created=None):
    return {"Id": iid(n), "Names": [f"localhost/img{n}:latest"] if names is None else names, "Size": size,
            "Created": created if created is not None else at(400) + n, "ReadOnly": False}


def container(n, image_n, state="exited"):
    return {"Id": cid(n), "Names": [f"box{n}"], "ImageID": iid(image_n), "State": state}


class GuestRoot:
    """The VM's own root the fake guest agent runs the wrapper in: its Podman and usage file."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def podman(self, **state):
        state.setdefault("volumes", ["pgdata"])
        with open(os.path.join(self.root, "podman.json"), "w") as f:
            json.dump(state, f)

    def state(self):
        with open(os.path.join(self.root, "podman.json")) as f:
            return json.load(f)

    def images(self):
        return sorted(i["Id"] for i in self.state().get("images", []))

    def usage(self, images=None, raw=None):
        path = os.path.join(self.root, USAGE)
        if images is None and raw is None:
            try:
                with open(path) as f:
                    return json.load(f)["images"]
            except FileNotFoundError:
                return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(raw if raw is not None else json.dumps({"images": images}))
        return None

    def calls(self):
        try:
            with open(os.path.join(self.root, "podman-calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


class CleanupTest(ActivityTest):
    def setUp(self):
        super().setUp()
        self.work = GuestRoot(os.path.join(self.dir, "guest-work"))
        self.priv = GuestRoot(os.path.join(self.dir, "guest-priv"))
        self.work.podman()
        self.priv.podman()

    def records(self, **fields):
        """work and priv, work with these record fields."""
        vm_records.write(self.root, vm_records.record("work", 1, **fields), vm_records.record("priv", 2))

    def cleanup(self, *args):
        code, out, err = self.run_agentbox("--actor=timer", "cleanup-container-images", "--json", *args)
        self.assertEqual(code, 0, err)
        return {r["vm"]: r for r in json.loads(out)["vms"]}, err

    def cleaned(self):
        return [e for e in self.entries() if e.get("AGENTBOX_ACTION", "").startswith("container-image-cleanup")]


class WhatIsRemovedTest(CleanupTest):
    def test_only_container_images_no_container_uses_and_not_used_for_more_than_the_days_go(self):
        self.work.podman(
            images=[image(1, 100), image(2, 200), image(3, 300), image(4, 400), image(5, 500),
                    image(6, 600, names=["localhost/a:1", "localhost/a:latest"]), image(7, 700, names=[])],
            containers=[container(1, 2, "exited"), container(2, 3, "running")],
            external=[container(3, 4, "running")], volumes=["pgdata", "cache"])
        old = at(30)
        self.work.usage({iid(n): old for n in range(1, 8)} | {iid(5): at(2)})
        results, _ = self.cleanup()
        # 1, 6 (two names) and 7 (no name) go; 2 and 3 have a container, stopped or running, 4 a
        # build's container, 5 was used two days ago
        self.assertEqual(results["work"], {"vm": "work", "outcome": "removed", "reason": None, "removed": 3,
                                           "freed_bytes": 100 + 600 + 700, "error": None})
        self.assertEqual(self.work.images(), [iid(2), iid(3), iid(4), iid(5)])
        state = self.work.state()
        self.assertEqual(sorted(c["Id"] for c in state["containers"] + state["external"]), [cid(1), cid(2), cid(3)])
        self.assertEqual(state["volumes"], ["pgdata", "cache"])
        # removed only with rmi, never by force, never anything about containers or volumes
        argvs = [c["argv"] for c in self.work.calls()]
        self.assertEqual(sorted(a for a in argvs if a[0] == "rmi"),
                         sorted([["rmi", iid(1)], ["rmi", "localhost/a:1", "localhost/a:latest"], ["rmi", iid(7)]]))
        self.assertEqual({a[0] for a in argvs}, {"images", "ps", "rmi"})
        self.assertFalse(any("--force" in a or "-f" in a for a in argvs))
        # the usage file forgets what is gone
        self.assertEqual(sorted(self.work.usage()), [iid(2), iid(3), iid(4), iid(5)])

    def test_days_are_calendar_days(self):
        self.work.podman(images=[image(1), image(2), image(3)])
        self.work.usage({iid(1): at(7, 23, 59, 59), iid(2): at(8, 0, 0, 1), iid(3): at(8, 23, 59, 59)})
        results, _ = self.cleanup()
        self.assertEqual(self.work.images(), [iid(1)])
        self.assertEqual(results["work"]["removed"], 2)

    def test_container_images_without_a_recorded_use_count_as_used_when_first_seen(self):
        before = int(time.time())
        self.work.podman(images=[image(1, created=at(300)), image(2, created=at(300))])
        self.work.usage({iid(2): at(30)})
        results, _ = self.cleanup()
        self.assertEqual(results["work"]["outcome"], "removed")
        self.assertEqual(self.work.images(), [iid(1)])
        seen = self.work.usage()[iid(1)]
        self.assertTrue(before <= seen <= time.time(), seen)
        # and it stays for the days from then
        results, _ = self.cleanup()
        self.assertEqual(results["work"]["outcome"], "nothing")
        self.assertEqual(self.work.images(), [iid(1)])

    def test_a_usage_file_that_cannot_be_read_removes_nothing(self):
        for raw in ("not json", '{"images": []}', json.dumps({"images": {iid(1): "old"}})):
            with self.subTest(raw=raw):
                self.work.podman(images=[image(1)])
                self.work.usage(raw=raw)
                results, _ = self.cleanup()
                self.assertEqual(results["work"]["outcome"], "nothing")
                self.assertEqual(self.work.images(), [iid(1)])

    def test_the_days_of_the_vm_apply(self):
        for days, removed in ((30, []), (9, [iid(1)])):
            with self.subTest(days=days):
                self.records(container_image_cleanup_days=days)
                self.work.podman(images=[image(1)])
                self.work.usage({iid(1): at(10)})
                self.cleanup()
                self.assertEqual(self.work.images(), sorted(set([iid(1)]) - set(removed)))
        [entry] = self.cleaned()
        self.assertEqual(entry["AGENTBOX_PARAM_DAYS"], "9")

    def test_a_record_without_the_fields_means_on_and_seven_days(self):
        self.work.podman(images=[image(1), image(2)])
        self.work.usage({iid(1): at(7, 1), iid(2): at(8, 23)})
        self.cleanup()
        self.assertEqual(self.work.images(), [iid(1)])
        self.assertEqual([c["arg"] for c in self.calls("guest-exec")], [["prune", "7"], ["prune", "7"]])

    def test_an_image_podman_refuses_stays_and_the_others_go(self):
        self.work.podman(images=[image(1), image(2)], rmi_refuses=[iid(1)])
        self.work.usage({iid(1): at(30), iid(2): at(30)})
        results, _ = self.cleanup()
        self.assertEqual((results["work"]["removed"], self.work.images()), (1, [iid(1)]))

    def test_an_image_with_several_names_a_container_took_meanwhile_keeps_all_its_names(self):
        names = ["localhost/a:1", "localhost/a:2", "localhost/a:3"]
        self.work.podman(images=[image(1, names=names)], containers_at_rmi=[container(1, 1)])
        self.work.usage({iid(1): at(30)})
        results, _ = self.cleanup()
        self.assertEqual(results["work"]["removed"], 0)
        [kept] = self.work.state()["images"]
        self.assertEqual(sorted(kept["Names"]), names)
        calls = [c["argv"] for c in self.work.calls()]
        self.assertIn(["rmi", *names], calls)
        self.assertEqual(sorted(c[2] for c in calls if c[:1] == ["tag"]), names)
        self.assertFalse(any("--force" in c or "-f" in c for c in calls))

    def test_podman_failing_removes_nothing(self):
        for failing in ("images", "ps"):
            with self.subTest(failing=failing):
                self.work.podman(images=[image(1)], fails=[failing])
                self.work.usage({iid(1): at(30)})
                results, _ = self.cleanup()
                self.assertEqual((results["work"]["outcome"], results["work"]["error"]),
                                 ("failed", "container_image_cleanup_failed"))
                self.assertEqual(self.work.images(), [iid(1)])

    def test_text_output(self):
        self.work.podman(images=[image(1, 3 * 1024 * 1024)])
        self.work.usage({iid(1): at(30)})
        self.records(container_image_cleanup=True)
        self.scenario(vms={"priv": {"state": "shut off"}})
        code, out, err = self.run_agentbox("--actor=timer", "cleanup-container-images")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "work: removed 1 Container image (3.0 MiB) not used for more than 7 days\n"
                              "priv: skipped, the VM is not running\n")


class WhichVmsTest(CleanupTest):
    """Only running VMs with the cleanup on; never one that is off, paused, missing or locked,
    and nothing in Activity for them."""

    def test_vms_that_are_off_paused_missing_or_have_the_cleanup_off_are_skipped(self):
        for vms, reason in (({"priv": {"state": "shut off"}}, "not_running"),
                            ({"priv": {"state": "shut off", "managed_save": True}}, "not_running"),
                            ({"priv": {"state": "paused"}}, "not_running"),
                            ({"priv": {"defined": False}}, "not_running")):
            with self.subTest(vms=vms):
                self.scenario(vms=vms)
                self.records(container_image_cleanup=False, container_image_cleanup_days=3)
                if os.path.exists(os.path.join(self.dir, "calls.jsonl")):
                    os.remove(os.path.join(self.dir, "calls.jsonl"))
                results, _ = self.cleanup()
                self.assertEqual(results, {
                    "work": {"vm": "work", "outcome": "skipped", "reason": "off", "removed": None, "freed_bytes": None,
                             "error": None},
                    "priv": {"vm": "priv", "outcome": "skipped", "reason": reason, "removed": None,
                             "freed_bytes": None, "error": None}})
                # nothing ran in them, nothing started or woke them
                self.assertEqual([c for c in self.calls() if c["cmd"] != "guest-ping"], [])
        self.assertEqual(self.entries(), [])

    def test_a_vm_whose_lifecycle_lock_is_held_is_skipped_this_hour(self):
        locks = self.path("var/lib/agentbox/locks")
        os.makedirs(locks, exist_ok=True)
        with open(os.path.join(locks, "lifecycle-work.lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            results, _ = self.cleanup()
        self.assertEqual((results["work"]["outcome"], results["work"]["reason"]), ("skipped", "locked"))
        self.assertEqual(results["priv"]["outcome"], "nothing")
        self.assertEqual((len(self.calls("guest-exec")), self.work.calls()), (1, []))
        self.assertEqual(self.entries(), [])

    def test_the_run_holds_the_lifecycle_lock_of_the_vm(self):
        self.scenario(still_running=True, vms={"priv": {"state": "shut off"}})
        p = subprocess.Popen([AGENTBOX, "cleanup-container-images", "--json"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             env={"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
                                  "FAKE_VIRSH_DIR": self.dir, "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root,
                                  "LANG": "C.UTF-8", "TZ": "UTC", "AGENTBOX_CONTAINER_IMAGE_CLEANUP_SECONDS": "4"})
        self.addCleanup(lambda: p.poll() is None and p.kill())
        deadline = time.monotonic() + 20
        while not self.calls("guest-exec"):
            self.assertLess(time.monotonic(), deadline)
            self.assertIsNone(p.poll())
            time.sleep(0.05)
        with open(self.path("var/lib/agentbox/locks/lifecycle-work.lock")) as f:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p.communicate(timeout=60)
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(p.returncode, 0)


class FailureTest(CleanupTest):
    def failed(self, error, vms=("work", "priv")):
        """Run once: these VMs failed with this code, each with its Activity entry; the entry of
        work and the error output."""
        results, err = self.cleanup()
        entries = {}
        for vm in vms:
            self.assertEqual((results[vm]["outcome"], results[vm]["error"], results[vm]["removed"]),
                             ("failed", error, None))
            [entries[vm]] = [e for e in self.cleaned() if e["AGENTBOX_VM"] == vm]
            self.assertEqual((entries[vm]["AGENTBOX_KIND"], entries[vm]["AGENTBOX_ACTION"],
                              entries[vm]["AGENTBOX_OUTCOME"], entries[vm]["AGENTBOX_ERROR"],
                              entries[vm]["AGENTBOX_PARAM_DAYS"], entries[vm]["AGENTBOX_ACTOR"]),
                             ("action", "container-image-cleanup.failed", "failed", error, "7", "timer"))
        return entries[vms[0]], err

    def test_a_build_without_the_wrapper_is_reported_and_the_other_vms_go_on(self):
        self.scenario(missing_wrappers=[WRAPPER_PATH])
        entry, err = self.failed("container_image_cleanup_missing")
        self.assertIn("build its Image and reset the VM first", entry["AGENTBOX_DESCRIPTION"])
        self.assertIn("work: the VM's Build has no Container image cleanup yet", err)

    def test_one_vm_failing_does_not_stop_the_others(self):
        self.work.podman(images=[image(1)], fails=["images"])
        self.priv.podman(images=[image(1)])
        self.priv.usage({iid(1): at(30)})
        results, _ = self.cleanup()
        self.assertEqual((results["work"]["outcome"], results["priv"]["outcome"]), ("failed", "removed"))
        self.assertEqual(self.priv.images(), [])

    def test_a_vm_being_deleted_when_the_run_fails_is_skipped_without_an_entry(self):
        self.scenario(agent_down=True)
        pending = self.path("var/lib/agentbox/pending")
        os.makedirs(pending)
        with open(os.path.join(pending, "delete-work.json"), "w") as f:
            json.dump({"record": vm_records.record("work", 1)}, f)
        results, err = self.cleanup()
        self.assertEqual((results["work"]["outcome"], results["work"]["reason"], results["work"]["error"]),
                         ("skipped", "not_running", None))
        self.assertEqual(results["priv"]["outcome"], "failed")
        self.assertEqual([e["AGENTBOX_VM"] for e in self.cleaned()], ["priv"])

    def test_a_failure_outside_the_wrapper_run_does_not_stop_the_others(self):
        self.priv.podman(images=[image(1)])
        self.priv.usage({iid(1): at(30)})
        locks = self.path("var/lib/agentbox/locks")
        os.makedirs(os.path.join(locks, "lifecycle-work.lock"))     # a lock file that cannot be opened
        code, out, err = self.run_agentbox("--actor=timer", "cleanup-container-images", "--json")
        self.assertEqual(code, 0, err)
        results = {r["vm"]: r for r in json.loads(out)["vms"]}
        self.assertEqual((results["work"]["outcome"], results["work"]["error"], results["work"]["removed"]),
                         ("failed", "action_failed", None))
        self.assertIn("lifecycle-work.lock", err)
        self.assertEqual((results["priv"]["outcome"], self.priv.images()), ("removed", []))

    def test_a_guest_agent_not_answering(self):
        self.scenario(agent_down=True)
        self.failed("guest_agent_unavailable")

    def test_the_time_limit(self):
        """5 minutes in the VM; the wrapper stops itself a little before."""
        sys.path.insert(0, REPO)
        from agentbox import container_images
        self.assertEqual(container_images.LIMIT, 300)
        with open(WRAPPER) as f:
            self.assertIn("\nLIMIT = 280 ", f.read())
        self.scenario(still_running=True)
        self.env["AGENTBOX_CONTAINER_IMAGE_CLEANUP_SECONDS"] = "1"
        entry, _ = self.failed("vm_query_timeout")
        self.assertIn("takes longer than 1 s", entry["AGENTBOX_DESCRIPTION"])

    def test_the_wrapper_stopping_at_its_own_limit(self):
        for response in ({"exitcode": 124}, {"signal": 9}):
            with self.subTest(response=response):
                self.journald.datagrams.clear()
                self.scenario(responses=[response])
                self.failed("vm_query_timeout")

    def test_the_answer_of_the_vm_is_untrusted(self):
        for answer in ('{"removed": true, "freed_bytes": 1}', '{"removed": -1, "freed_bytes": 1}',
                       '{"removed": 1}', "[1, 2]", '{"removed": 1.5, "freed_bytes": 2}', "not json",
                       '{"removed": 1, "freed_bytes": 99999999999999999999}',
                       '{"removed": 1, "freed_bytes": 2, "x": "' + "y" * 2000 + '"}'):
            with self.subTest(answer=answer[:60]):
                self.journald.datagrams.clear()
                self.scenario(responses=[{"stdout": answer}])
                self.failed("container_image_cleanup_failed")

    def test_nothing_the_vm_says_reaches_activity(self):
        self.scenario(responses=[{"exitcode": 1, "stdout": "evil out", "stderr": "agentbox-container-images: evil\x1b[2J"}])
        entry, err = self.failed("container_image_cleanup_failed")
        self.assertEqual(entry["AGENTBOX_DESCRIPTION"],
                         "Container image cleanup failed: work: the Container image cleanup in the VM failed (exit 1)")
        self.assertNotIn("evil", json.dumps(self.entries()) + err)


class ActivityEntryTest(CleanupTest):
    def test_a_run_that_removed_something_is_recorded_one_that_removed_nothing_is_not(self):
        self.work.podman(images=[image(1, 1500), image(2, 2500), image(3)])
        self.work.usage({iid(1): at(30), iid(2): at(30), iid(3): at(1)})
        self.priv.podman(images=[image(1)])
        self.priv.usage({iid(1): at(1)})
        results, _ = self.cleanup()
        self.assertEqual(results["priv"], {"vm": "priv", "outcome": "nothing", "reason": None, "removed": 0,
                                           "freed_bytes": 0, "error": None})
        [entry] = self.entries()            # none of the dispatcher's for the command, none for priv
        self.assertEqual((entry["AGENTBOX_KIND"], entry["AGENTBOX_ACTION"], entry["AGENTBOX_OUTCOME"],
                          entry["AGENTBOX_VM"], entry["AGENTBOX_ACTOR"]),
                         ("action", "container-image-cleanup.removed", "ok", "work", "timer"))
        self.assertEqual((entry["AGENTBOX_PARAM_REMOVED"], entry["AGENTBOX_PARAM_FREED_BYTES"],
                          entry["AGENTBOX_PARAM_DAYS"]), ("2", "4000", "7"))
        self.assertEqual(entry["AGENTBOX_DESCRIPTION"],
                         "Container image cleanup removed 2 Container images (3.9 KiB) not used for more than 7 days")

    def test_a_registry_that_cannot_be_read_fails_the_command(self):
        os.chmod(self.path("etc/agentbox/vms"), 0)
        self.addCleanup(os.chmod, self.path("etc/agentbox/vms"), 0o755)
        if os.access(self.path("etc/agentbox/vms"), os.R_OK):
            self.skipTest("root reads it anyway")
        code, out, err = self.run_agentbox("--actor=timer", "cleanup-container-images", "--json")
        self.assertEqual((code, out, json.loads(err)["error"]), (1, "", "action_failed"))
        self.assertEqual(self.entries(), [])

    def test_unknown_options_are_refused(self):
        code, _, err = self.run_agentbox("cleanup-container-images", "--all", "--json")
        self.assertEqual((code, json.loads(err)["error"]), (2, "invalid_argument"))
        self.assertEqual(self.calls("guest-exec"), [])


class TimerTest(unittest.TestCase):
    def test_the_package_runs_it_every_hour_as_timer(self):
        """rpm/units; agentbox setup enables and starts the timer (test_setup.py)."""
        with open(os.path.join(REPO, "rpm", "units", "agentbox-container-image-cleanup.service")) as f:
            self.assertIn("ExecStart=/usr/bin/agentbox --actor=timer cleanup-container-images\n", f.read())
        with open(os.path.join(REPO, "rpm", "units", "agentbox-container-image-cleanup.timer")) as f:
            timer = f.read()
        self.assertIn("\nOnCalendar=hourly\n", timer)
        self.assertIn("\nPersistent=true\n", timer)

    def test_the_image_runs_the_usage_recorder_as_the_agent(self):
        with open(os.path.join(REPO, "vm", "files", "usr/lib/systemd/user/agentbox-container-image-usage.service")) as f:
            unit = f.read()
        self.assertIn(f"\nExecStart=/usr/bin/python3 -I {WRAPPER_PATH} record\n", unit)
        with open(os.path.join(REPO, "vm", "Containerfile")) as f:
            self.assertRegex(f.read(), r"systemctl --global enable [^\n]*agentbox-container-image-usage\.service")


class GuestTest(unittest.TestCase):
    """agentbox-container-images, run directly in a test root (AGENTBOX_CONTAINER_IMAGES_ROOT)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.guest = GuestRoot(os.path.realpath(tmp.name))
        podman = os.path.join(self.guest.root, "usr", "bin", "podman")
        os.makedirs(os.path.dirname(podman))
        with open(podman, "w") as f:
            f.write(f'#!/bin/sh\nFAKE_GUEST_ROOT="{self.guest.root}" exec python3 "{FAKE_PODMAN}" "$@"\n')
        os.chmod(podman, 0o755)
        self.guest.podman()

    def run_wrapper(self, *args):
        r = subprocess.run([sys.executable, "-I", WRAPPER, *args], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "TZ": "UTC", "AGENTBOX_CONTAINER_IMAGES_ROOT": self.guest.root})
        return r.returncode, r.stdout, r.stderr


@unittest.skipIf(os.geteuid() == 0, "without root the wrapper stays in its test root")
class WrapperTest(GuestTest):
    def test_invalid_arguments(self):
        for args in ((), ("prune",), ("prune", "0"), ("prune", "366"), ("prune", "7d"), ("prune", "-1"),
                     ("prune", "7", "8"), ("remove", "7"), ("record", "x")):
            with self.subTest(args=args):
                self.assertEqual(self.run_wrapper(*args)[0], 2)
        self.assertEqual(self.guest.calls(), [])

    def test_freeze_and_thaw_do_nothing(self):
        for hook in ("freeze", "thaw"):
            self.assertEqual(self.run_wrapper(hook), (0, "", ""))
        self.assertEqual(self.guest.calls(), [])

    def test_prints_only_the_two_numbers(self):
        self.guest.podman(images=[image(1, 10), image(2, 20)])
        self.guest.usage({iid(1): at(2), iid(2): at(3)})
        self.assertEqual(self.run_wrapper("prune", "1"), (0, '{"removed": 2, "freed_bytes": 30}\n', ""))


@unittest.skipIf(os.geteuid() == 0, "without root the wrapper stays in its test root")
class RecorderTest(GuestTest):
    """The usage recorder, fed recorded `podman events --format json` output."""

    def events(self, *events, **state):
        self.guest.podman(events=[e if isinstance(e, str) else json.dumps(e) for e in events], **state)
        before = int(time.time())
        code, out, err = self.run_wrapper("record")
        self.assertEqual((code, out), (0, ""), err)
        return before

    def test_pulls_builds_and_containers_created_or_started_count_as_a_use(self):
        old = at(20)
        self.guest.usage({iid(9): old, iid(1): old})
        before = self.events(
            {"ID": iid(1), "Name": "docker.io/library/postgres:16", "Status": "pull", "Time": "2026-11-12T10:00:00Z",
             "Type": "image"},
            # a build event that names only its tag
            {"Name": "localhost/app:latest", "Status": "build", "Type": "image"},
            {"ID": cid(1), "Image": "docker.io/library/redis:7", "Name": "box1", "Status": "create", "Type": "container"},
            # a container gone again (--rm): its image by name
            {"ID": cid(9), "Image": "localhost/img4:latest", "Name": "gone", "Status": "start", "Type": "container"},
            images=[image(1), image(2, names=["localhost/app:latest"]), image(3, names=["docker.io/library/redis:7"]),
                    image(4), image(9)],
            containers=[container(1, 3)])
        usage = self.guest.usage()
        self.assertEqual(sorted(usage), [iid(1), iid(2), iid(3), iid(4), iid(9)])
        for n in (1, 2, 3, 4):
            self.assertTrue(before <= usage[iid(n)] <= time.time(), n)
        self.assertEqual(usage[iid(9)], old)

    def test_other_events_and_lines_count_for_nothing(self):
        self.events(
            {"ID": cid(1), "Image": "localhost/img1:latest", "Status": "died", "Type": "container"},
            {"ID": cid(1), "Image": "localhost/img1:latest", "Status": "remove", "Type": "container"},
            {"ID": iid(1), "Name": "localhost/img1:latest", "Status": "tag", "Type": "image"},
            {"Name": "pgdata", "Status": "create", "Type": "volume"},
            {"ID": "../../etc", "Name": "-rf", "Status": "pull", "Type": "image"},
            "not json", "[1, 2]", '{"Type": "image", "Status": "pull"}',
            images=[image(1)], containers=[container(1, 1)])
        self.assertIn(self.guest.usage(), (None, {}))
        # a name that would be an option is never passed to podman
        self.assertFalse(any("-rf" in c["argv"] for c in self.guest.calls()))

    def test_a_removed_image_is_forgotten(self):
        self.guest.usage({iid(1): at(3), iid(2): at(3)})
        self.events({"ID": iid(1), "Name": "localhost/img1:latest", "Status": "remove", "Type": "image"},
                    images=[image(2)])
        self.assertEqual(self.guest.usage(), {iid(2): at(3)})

    def test_what_the_recorder_saw_keeps_an_image_from_the_cleanup(self):
        self.guest.usage({iid(1): at(30)})
        self.events({"ID": iid(1), "Name": "localhost/img1:latest", "Status": "pull", "Type": "image"},
                    images=[image(1)])
        self.guest.podman(images=[image(1)])
        self.assertEqual(self.run_wrapper("prune", "7")[1], '{"removed": 0, "freed_bytes": 0}\n')
        self.assertEqual(self.guest.images(), [iid(1)])


if __name__ == "__main__":
    unittest.main()
