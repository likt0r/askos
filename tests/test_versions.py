"""Versions of the tools in the Images (#104): what every Build records (Orca, Claude Code,
codex, opencode, the Fedora release), what the daily check finds available (`agentbox
fetch-versions` as agentbox-feeds: the npm tools and Orca as a Build would install them now,
#113; then `agentbox check-versions` as root: that file read as untrusted input, the package
updates of the current Build), and how `agentbox images` shows both.

The harness of test_images.py (the real CLI under AGENTBOX_ROOT, fake podman building a
rootfs from the repository's vm/Containerfile), plus a fake curl that plays the npm registry
and GitHub (fake_registry_curl.py); nothing reaches the network.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import datetime
import fcntl
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from vm_records import record  # noqa: E402
from test_images import DERIVED, OLD, REPO, RUN_EFFECTS, ImagesTest  # noqa: E402
from test_orca_release import RELEASES, release  # noqa: E402
from test_registry import AGENTBOX, fake  # noqa: E402

VERSIONS = "var/lib/agentbox/versions.json"
AVAILABLE = "var/lib/agentbox/available/available.json"
DAY = 86400


def asar(package, unpacked=False, offset="0"):
    """An Electron archive (app.asar) holding package.json and one more file."""
    data = json.dumps(package).encode()
    entry = {"size": len(data), "offset": offset, **({"unpacked": True} if unpacked else {})}
    table = json.dumps({"files": {"LICENSE": {"size": 3, "offset": str(len(data))}, "package.json": entry,
                                  "out": {"files": {}}}}).encode()
    payload = struct.pack("<I", len(table)) + table + b"\0" * (-len(table) % 4)
    header = struct.pack("<I", len(payload)) + payload
    return struct.pack("<II", 4, len(header)) + header + data + b"MIT"


def orca_package(version):
    return {"name": "orca", "version": version, "homepage": "https://github.com/stablyai/orca"}


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def npm(latest, *versions):
    """The registry's metadata of a package: versions as (version, days ago[, deprecated])."""
    now = time.time()
    doc = {"dist-tags": {"latest": latest}, "versions": {}, "time": {"created": iso(now - 900 * DAY)}}
    for v, days, *deprecated in versions:
        doc["versions"][v] = {"name": "x", "version": v, **({"deprecated": "do not use"} if deprecated else {})}
        doc["time"][v] = iso(now - days * DAY)
    return doc


# The RUN line of vm/Containerfile that installs Orca from its release (vm/build/orca-release)
ORCA_RUN = "/tmp/orca-release rpm /tmp/orca"
NODE_SHA = "e32e2d16081cf644f88392934288086e7662529d0d43277c34f7aad2207cfafb"


def orca_effects(archive):
    """What that RUN line leaves in the Image: the RPM's files moved to /usr/lib/orca, its link
    on PATH and desktop file, and the Node runtime of Orca's server."""
    return {"/usr/lib/orca/resources/app.asar": {"base64": base64.b64encode(archive).decode()},
            "/usr/lib/orca/resources/bin/orca-ide": {"content": "#!/bin/bash\n", "mode": 0o755},
            "/usr/lib/orca/chrome-sandbox": {"content": "ELF", "mode": 0o4755},
            "/usr/bin/orca-ide": {"symlink": "/usr/lib/orca/resources/bin/orca-ide"},
            "/usr/share/applications/orca-ide.desktop": "[Desktop Entry]\nName=Orca\nExec=/opt/Orca/orca-ide %U\n",
            f"/usr/lib/agentbox/orca-runtimes/node-{NODE_SHA}/bin/node": {"content": "ELF", "mode": 0o755}}


# What the Build's RUN lines leave in the Image beyond test_images: Orca from its release and
# the release of the distribution image
IMAGE_EFFECTS = {
    **RUN_EFFECTS,
    ORCA_RUN: orca_effects(asar(orca_package("1.4.241"))),
    "gtk3 nss": {"/usr/lib/os-release": 'NAME="Fedora Linux"\nVERSION="44 (Container Image)"\nID=fedora\n'
                                       'VERSION_ID=44\n'},
}
# what the registry and GitHub know: claude-code's latest is one day old, so the Build
# (package_min_age_days 2) takes the one before; of Orca it takes 1.4.251 (RELEASES)
REGISTRY = {
    "npm": {
        "@anthropic-ai/claude-code": npm("2.1.9", ("2.1.5", 30), ("2.1.7", 5), ("2.1.8-beta.1", 4), ("2.1.9", 1)),
        "@openai/codex": npm("0.80.0", ("0.79.0", 40), ("0.80.0", 10), ("0.81.0-alpha.3", 3)),
        "opencode-ai": npm("1.2.3", ("1.2.3", 20)),
    },
    "github": {"stablyai/orca": RELEASES},
}
ORCA_API = "https://api.github.com/repos/stablyai/orca/releases"


class VersionsTest(ImagesTest):
    def setUp(self):
        super().setUp()
        fake(os.path.join(self.dir, "bin", "curl"), "fake_registry_curl.py")
        with open(os.path.join(self.dir, "run.json"), "w") as f:
            json.dump(IMAGE_EFFECTS, f)
        self.registry(REGISTRY)

    def image_orca(self, archive):
        """The app.asar the next Build's Orca has."""
        with open(os.path.join(self.dir, "run.json"), "w") as f:
            json.dump({**IMAGE_EFFECTS, ORCA_RUN: orca_effects(archive)}, f)

    def registry(self, known):
        with open(os.path.join(self.dir, "registry.json"), "w") as f:
            json.dump(known, f)

    def env(self, **extra):
        """The environment of run_agentbox (test_registry.py), plus these variables."""
        return {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
                "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC", **extra}

    def check(self):
        """What the timer does: the fetch (agentbox-feeds' unit), then the root part."""
        code, out, err = self.run_agentbox("fetch-versions")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_agentbox("check-versions")
        self.assertEqual(code, 0, err)
        return out, err

    def fetched(self):
        with open(self.path(AVAILABLE)) as f:
            return json.load(f)

    def record(self):
        with open(self.path(VERSIONS)) as f:
            return json.load(f)

    def curl(self):
        return [c["argv"][-1] for c in self.calls("curl")]


class BuildRecordsTest(VersionsTest):
    def test_a_build_records_the_versions_of_its_tools(self):
        self.built()
        new = self.newest()
        self.assertEqual(new["tools"], {"orca": "1.4.241", "claude-code": "2.1.5", "codex": "0.80.0",
                                        "opencode": "1.2.3", "fedora": "44"})
        with open(self.path(f"var/lib/agentbox/builds/base-{new['build']}.json")) as f:
            self.assertEqual(json.load(f)["tools"], new["tools"])
        # the Orca of its release passes the Safeguard check like everything else in the Image
        self.assertEqual((new["status"], new["problems"]), ("ok", []))
        self.assertEqual(new["orca_version"], None)

    def test_a_build_with_a_pinned_orca_records_the_pin(self):
        self.ok("set-settings", "orca_version=1.4.241")
        self.built()
        self.assertEqual((self.newest()["orca_version"], self.newest()["tools"]["orca"]), ("1.4.241", "1.4.241"))

    def test_a_derived_image_records_its_own(self):
        self.built()
        self.image("rust", DERIVED + "RUN npm install -g opencode-ai@1.3.0\n")
        with open(os.path.join(self.dir, "run.json"), "w") as f:
            json.dump({**IMAGE_EFFECTS, "opencode-ai@1.3.0": {
                "/usr/lib/node_modules/opencode-ai/package.json": '{"version": "1.3.0"}'}}, f)
        self.ok("set-settings", "orca_version=1.4.250")         # takes effect at the next Base Build
        self.built("rust")
        self.assertEqual(self.newest("rust")["tools"]["opencode"], "1.3.0")
        # Orca is the Base Build's, and so is the pin it was built with
        self.assertEqual((self.newest("rust")["tools"]["orca"], self.newest("rust")["orca_version"]), ("1.4.241", None))
        self.assertEqual([c["argv"] for c in self.podman("build")][-1].count("ORCA_VERSION=1.4.250"), 0)

    def test_what_cannot_be_read_is_unknown_and_the_build_goes_on(self):
        with open(os.path.join(self.dir, "run.json"), "w") as f:
            json.dump({**RUN_EFFECTS, ORCA_RUN: {"/usr/lib/orca/resources/app.asar": "not an archive"}}, f)
        self.built()
        new = self.newest()
        self.assertEqual((new["status"], new["current"]), ("ok", True))
        self.assertEqual((new["tools"]["orca"], new["tools"]["fedora"], new["tools"]["claude-code"]),
                         (None, None, "2.1.5"))

    def test_an_archive_with_an_odd_offset_is_unknown(self):
        self.image_orca(asar(orca_package("1.4.241"), offset=float("inf")))
        self.built()
        self.assertEqual((self.newest()["status"], self.newest()["tools"]["orca"]), ("ok", None))

    def test_an_unpacked_package_json_is_not_followed(self):
        self.image_orca(asar(orca_package("1.4.241"), unpacked=True))
        self.built()
        self.assertIsNone(self.newest()["tools"]["orca"])

    def test_builds_from_before_show_what_they_recorded_and_unknown_for_the_rest(self):
        os.makedirs(self.path("var/lib/agentbox/builds"), exist_ok=True)
        with open(self.path("var/lib/agentbox/builds/base-20261102-0300.json"), "w") as f:
            json.dump({"image": "base", "build": "20261102-0300", "status": "ok", "started": 1, "finished": 2,
                       "agents": {"claude-code": "2.0.1", "codex": None, "opencode": "1.0.0", "npm": "12.0.0"}}, f)
        self.write("var/lib/agentbox/images/base-20261102-0300.qcow2", "")
        builds = self.builds()
        self.assertEqual(builds["20261102-0300"]["tools"], {"orca": None, "claude-code": "2.0.1", "codex": None,
                                                            "opencode": "1.0.0", "fedora": None})
        self.assertEqual(builds[OLD]["tools"], dict.fromkeys(("orca", "claude-code", "codex", "opencode", "fedora")))


class CheckTest(VersionsTest):
    def test_npm_tools_as_a_build_would_install_them_now(self):
        self.check()
        npm = self.record()["npm"]
        # latest only a day old: the newest at least two days old, never a prerelease
        self.assertEqual(npm["claude-code"], {"version": "2.1.7", "error": None})
        self.assertEqual(npm["codex"], {"version": "0.80.0", "error": None})
        self.assertEqual(npm["opencode"], {"version": "1.2.3", "error": None})
        self.assertEqual(self.record()["package_min_age_days"], 2)
        self.assertIn("https://registry.npmjs.org/@anthropic-ai%2fclaude-code", self.curl())
        self.assertIn("https://registry.npmjs.org/@openai%2fcodex", self.curl())
        for argv in self.calls("curl"):
            self.assertEqual(argv["argv"][argv["argv"].index("--proto") + 1], "=https")

    def test_the_package_minimum_age_of_the_settings_counts(self):
        self.ok("set-settings", "package_min_age_days=7")
        self.check()
        self.assertEqual(self.record()["npm"]["claude-code"]["version"], "2.1.5")
        self.assertEqual(self.record()["package_min_age_days"], 7)

    def test_deprecated_versions_come_last(self):
        self.registry({**REGISTRY, "npm": {**REGISTRY["npm"], "opencode-ai": npm(
            "1.3.0", ("1.2.0", 30), ("1.2.3", 20, True), ("1.3.0", 1))}})
        self.check()
        self.assertEqual(self.record()["npm"]["opencode"]["version"], "1.2.0")

    def test_orca_as_a_build_would_install_it_now(self):
        out, _ = self.check()
        # the newest release at least two days old, never a draft or prerelease (RELEASES)
        self.assertEqual(self.record()["orca"], {"version": "1.4.251", "error": None, "pin": None})
        self.assertEqual(self.curl()[-1], f"{ORCA_API}?per_page=100&page=1")
        self.assertIn("Orca: 1.4.251, the newest release at least 2 days old", out)
        # nothing is downloaded
        self.assertFalse([u for u in self.curl() if "/releases/download/" in u])
        self.ok("set-settings", "package_min_age_days=4")
        self.check()
        self.assertEqual(self.record()["orca"]["version"], "1.4.250")

    def test_a_pinned_orca_version(self):
        self.ok("set-settings", "orca_version=1.4.253")
        out, _ = self.check()
        self.assertEqual(self.record()["orca"], {"version": "1.4.253", "error": None, "pin": "1.4.253"})
        self.assertEqual(self.curl()[-1], f"{ORCA_API}/tags/v1.4.253")
        self.assertIn("Orca: 1.4.253, pinned in orca_version (1.4.253)", out)

    def test_orca_a_build_could_not_install_is_unknown(self):
        for pin, releases, error in (
                ("1.4.999", RELEASES, "no release of the version in orca_version"),
                ("1.4.254", RELEASES, "no release of the version in orca_version"),          # a draft
                ("1.4.255", [release("1.4.255", 9, prerelease=True)], "no release of the version in orca_version"),
                ("", [release("1.4.253", 1)], "no matching version found"),
                ("", [release("1.4.251", 3, rpm=False)], "no x86_64 RPM with a SHA-256 in the release"),
                ("", [release("1.4.251", 3, digest=False)], "no x86_64 RPM with a SHA-256 in the release"),
                ("1.4.251", [release("1.4.251", 3, digest=False)], "no x86_64 RPM with a SHA-256 in the release"),
                ("", [release("1.4.251", 3, asset_days=0.5)], "no matching version found"),
                ("1.4.251", [release("1.4.251", 3, asset_days=0.5)],
                 "RPM of the version in orca_version replaced only recently"),
                ("", {"status": 403}, "GitHub answered 403")):
            with self.subTest(pin=pin, error=error):
                self.ok("set-settings", f"orca_version={pin}")
                self.registry({**REGISTRY, "github": {"stablyai/orca": releases}})
                self.check()
                self.assertEqual(self.record()["orca"], {"version": None, "error": error, "pin": pin or None})

    def test_the_list_is_paged_as_the_build_pages_it(self):
        young = [release(f"1.5.{n}", 0.5) for n in range(150, 50, -1)]
        self.registry({**REGISTRY, "github": {"stablyai/orca": [*young, *RELEASES]}})
        self.check()
        self.assertEqual(self.record()["orca"]["version"], "1.4.251")
        self.assertEqual([u for u in self.curl() if u.startswith(ORCA_API)],
                         [f"{ORCA_API}?per_page=100&page=1", f"{ORCA_API}?per_page=100&page=2"])

    def test_the_highest_version_counts_even_on_a_later_page(self):
        old = [release(f"1.3.{n}", 9) for n in range(200, 100, -1)]
        self.registry({**REGISTRY, "github": {"stablyai/orca": [*old, release("1.4.251", 3)]}})
        self.check()
        self.assertEqual(self.record()["orca"]["version"], "1.4.251")

    def test_package_updates_are_counted_in_a_container_from_the_current_build(self):
        self.built()
        build = self.newest()["build"]
        self.scenario(check_update={"localhost/agentbox/base:": {"exit": 100, "stdout": (
            "glibc.x86_64                 2.41-9.fc44        updates\n"
            "openssl-libs.x86_64          1:3.5.4-2.fc44     updates\n"
            "\n"
            "Obsoleting packages\n"
            "grub2-tools.x86_64           1:2.12-40.fc44     updates\n")}})
        out, _ = self.check()
        self.assertEqual(self.record()["packages"], {"base": {"build": build, "updates": 2, "error": None}})
        run = [c["argv"] for c in self.podman("run")]
        self.assertEqual(run[-1], ["run", "--rm", "--name=agentbox-check-base", "--pull=never", "--cap-drop=all",
                                   "--security-opt=no-new-privileges", "--read-only", "--tmpfs=/var/cache",
                                   "--tmpfs=/var/lib/dnf", "--tmpfs=/var/log", f"localhost/agentbox/base:{build}",
                                   "dnf", "-q", "check-update"])
        # a container of that name left by a check that was killed goes first
        self.assertEqual([c["argv"] for c in self.podman("rm")], [["rm", "--force", "--ignore",
                                                                   "agentbox-check-base"]])
        self.assertIn(f"Package updates for base-{build}: 2", out)

    def test_no_package_updates(self):
        self.built()
        self.check()
        self.assertEqual(self.record()["packages"]["base"]["updates"], 0)

    def test_images_without_their_container_image_are_not_counted(self):
        # the Builds from before and derived Images keep no container image
        self.check()
        self.assertEqual(self.record()["packages"], {})
        self.assertEqual(self.podman("run"), [])

    def test_a_build_never_waits_for_the_count_and_may_end_it(self):
        self.built()
        old = self.newest()["build"]
        # a count that would take 30 s, unless its container image goes
        self.scenario(check_update={"localhost/agentbox/base:": {"exit": 100, "stdout": "a.x86_64 1-1 updates\n",
                                                                 "sleep": 30}})
        check = subprocess.Popen([AGENTBOX, "check-versions"], env=self.env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: check.poll() is None and check.kill())
        deadline = time.monotonic() + 30
        while not any("dnf" in c["argv"] for c in self.podman("run")):
            self.assertLess(time.monotonic(), deadline, "the count did not start")
            time.sleep(0.05)
        started = time.monotonic()
        self.built()                # a new Base Build, which removes the old one's container image
        self.assertLess(time.monotonic() - started, 20)
        out, err = check.communicate(timeout=30)
        self.assertEqual(check.returncode, 0, err)
        self.assertEqual(self.record()["packages"]["base"], {"build": old, "updates": None,
                                                             "error": "dnf check-update failed (exit 137): Error: container was stopped: its image was removed"})
        self.assertIn(["rmi", "--force", "--ignore", f"localhost/agentbox/base:{old}"],
                      [c["argv"] for c in self.podman("rmi")])

    def test_a_count_that_takes_too_long_is_ended(self):
        self.built()
        self.scenario(check_update={"localhost/agentbox/base:": {"exit": 0, "sleep": 30}})
        r = subprocess.run([AGENTBOX, "check-versions"], env=self.env(AGENTBOX_CHECK_UPDATE_SECONDS="1"),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.record()["packages"]["base"]["error"], "dnf check-update: time limit exceeded")
        self.assertEqual([c["argv"] for c in self.podman("rm")][-1], ["rm", "--force", "--ignore",
                                                                     "agentbox-check-base"])

    def test_what_fails_is_unknown_and_the_check_still_ends_well(self):
        self.built()
        self.registry({"npm": {"@anthropic-ai/claude-code": {"status": 500}}, "github": {}})
        self.scenario(check_update={"localhost/agentbox/base:": {"exit": 1, "stderr": "Error: Failed to download\x1b[31m metadata\n"}})
        self.run_agentbox("fetch-versions")
        code, out, err = self.run_agentbox("check-versions")
        self.assertEqual(code, 0, err)
        rec = self.record()
        self.assertEqual(rec["npm"]["claude-code"], {"version": None, "error": "npm registry answered 500"})
        self.assertEqual(rec["npm"]["codex"]["version"], None)
        self.assertEqual(rec["orca"], {"version": None, "error": "GitHub answered 404", "pin": None})
        self.assertEqual(rec["packages"]["base"]["updates"], None)
        self.assertIn("dnf check-update failed (exit 1): Error: Failed to download", rec["packages"]["base"]["error"])
        self.assertNotIn("\x1b", rec["packages"]["base"]["error"])
        self.assertIn("not everything determined", err)
        self.assertIn("stays unknown", err)

    def test_no_network_at_all(self):
        self.registry({"exit": 6})
        self.check()
        rec = self.record()
        self.assertEqual(rec["npm"]["opencode"], {"version": None, "error": "npm registry not reachable"})
        self.assertEqual(rec["orca"]["error"], "GitHub not reachable")

    def test_an_endless_chunked_answer_is_cut_off(self):
        self.registry({"flood": True})
        self.check()
        rec = self.record()
        self.assertEqual(rec["npm"]["opencode"], {"version": None, "error": "npm registry not reachable"})
        self.assertEqual(rec["orca"]["error"], "GitHub not reachable")

    def test_orca_on_the_host_plays_no_part(self):
        # the Host's own Orca (for the Observer) is neither read nor named
        os.makedirs(self.path("opt/Orca/resources"))
        with open(self.path("opt/Orca/resources/app.asar"), "wb") as f:
            f.write(asar(orca_package("1.4.199")))
        self.write("opt/Orca/resources/app-update.yml", "owner: someone\nrepo: else\nprovider: github\n")
        self.check()
        self.assertEqual(self.record()["orca"], {"version": "1.4.251", "error": None, "pin": None})
        self.assertNotIn("1.4.199", self.ok("images"))

    def test_json_and_no_activity(self):
        rec = self.json("check-versions")
        self.assertEqual(rec, self.record())
        self.assertEqual(self.activity(""), [])


class FetchTest(VersionsTest):
    """The part that talks to the internet, as agentbox-feeds: it needs nothing of root's."""

    def test_it_writes_what_npm_and_github_offer_for_the_root_part(self):
        self.built()
        self.forget_calls()
        code, out, err = self.run_agentbox("fetch-versions")
        self.assertEqual(code, 0, err)
        found = self.fetched()
        self.assertEqual(set(found), {"fetched", "package_min_age_days", "npm", "orca"})
        self.assertEqual(found["npm"]["claude-code"], {"version": "2.1.7", "error": None})
        self.assertEqual(found["orca"], {"version": "1.4.251", "error": None, "pin": None})
        self.assertIn("Orca: 1.4.251, the newest release at least 2 days old", out)
        # no container, no root's record
        self.assertEqual(self.podman(), [])
        self.assertFalse(self.exists(VERSIONS))

    def test_the_minimum_age_comes_from_its_unit_without_reading_the_settings(self):
        self.write("etc/agentbox/settings.json", "{broken")
        r = subprocess.run([AGENTBOX, "fetch-versions"], env=self.env(AGENTBOX_VERSIONS_MIN_AGE_DAYS="7"),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.fetched()["package_min_age_days"], self.fetched()["npm"]["claude-code"]["version"],
                          self.fetched()["orca"]["version"]), (7, "2.1.5", "1.4.249"))

    def test_the_orca_version_comes_from_its_unit_without_reading_the_settings(self):
        self.write("etc/agentbox/settings.json", "{broken")
        for pin, version in (("1.4.250", "1.4.250"), ("", "1.4.251")):
            with self.subTest(pin=pin):
                r = subprocess.run([AGENTBOX, "fetch-versions"], env=self.env(AGENTBOX_VERSIONS_MIN_AGE_DAYS="2",
                                                                             AGENTBOX_VERSIONS_ORCA_VERSION=pin),
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.fetched()["orca"], {"version": version, "error": None, "pin": pin or None})

    def test_curl_reads_no_configuration_of_its_user(self):
        self.run_agentbox("fetch-versions")
        for c in self.calls("curl"):
            self.assertEqual(c["argv"][0], "-q")

    def test_only_the_fetch_may_run_without_root(self):
        # a checkout's CLI outside tests: fetch-versions passes the check for root (and then
        # refuses, as any changing command of a checkout does); check-versions does not
        env = {k: v for k, v in self.env().items() if k != "AGENTBOX_ROOT"}
        if os.geteuid() == 0:
            self.skipTest("runs as root")
        for cmd, message in (("fetch-versions", "this agentbox is not installed"),
                             ("check-versions", "check-versions needs root")):
            r = subprocess.run([AGENTBOX, cmd], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 1)
            self.assertIn(message, r.stderr)


class UntrustedFetchTest(VersionsTest):
    """The root part reads agentbox-feeds' file as untrusted input."""

    def hostile(self, content):
        os.makedirs(self.path(os.path.dirname(AVAILABLE)), exist_ok=True)
        with open(self.path(AVAILABLE), "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        code, out, err = self.run_agentbox("check-versions")
        self.assertEqual(code, 0, err)
        return self.record()

    def good(self, **changes):
        return {"fetched": int(time.time()), "package_min_age_days": 2,
                "npm": {"claude-code": {"version": "2.1.7", "error": None},
                        "codex": {"version": "0.80.0", "error": None},
                        "opencode": {"version": "1.2.3", "error": None}},
                "orca": {"version": "1.4.251", "error": None, "pin": None}, **changes}

    def unknown(self, rec):
        self.assertEqual([rec["npm"][t]["version"] for t in ("claude-code", "codex", "opencode")], [None] * 3)
        self.assertEqual(rec["orca"]["version"], None)

    def test_a_good_file_is_taken(self):
        rec = self.hostile(self.good())
        self.assertEqual((rec["npm"]["codex"]["version"], rec["orca"], rec["package_min_age_days"]),
                         ("0.80.0", {"version": "1.4.251", "error": None, "pin": None}, 2))
        rec = self.hostile(self.good(orca={"version": "1.4.250", "error": None, "pin": "1.4.250"}))
        self.assertEqual(rec["orca"], {"version": "1.4.250", "error": None, "pin": "1.4.250"})

    def test_every_string_is_checked_again(self):
        rec = self.hostile(self.good(
            package_min_age_days=9999,
            npm={"claude-code": {"version": "2.1.7; rm -rf /", "error": "<script>alert(1)</script>"},
                 "codex": {"version": "x" * 100, "error": None}, "opencode": {"version": ["1.2.3"]},
                 "evil": {"version": "1.0.0"}},
            orca={"version": {"v": 1}, "pin": "../../etc", "error": "npm registry answered 500", "evil": 1}))
        self.unknown(rec)
        self.assertEqual(set(rec["npm"]), {"claude-code", "codex", "opencode"})
        self.assertEqual(rec["npm"]["claude-code"]["error"], "unknown error")
        self.assertEqual(rec["orca"], {"version": None, "error": "npm registry answered 500", "pin": None})
        self.assertIsNone(rec["package_min_age_days"])
        for version in ("1.4", "v1.4.251", "1.4.251-rc.0", "1.4.251\n", "1.4.251 ", "01.4.251", 1.4):
            with self.subTest(version=version):
                rec = self.hostile(self.good(orca={"version": version, "error": None, "pin": version}))
                self.assertEqual(rec["orca"], {"version": None, "error": "unknown error", "pin": None})

    def test_a_fetch_from_before_names_no_orca(self):
        """Before #113 the fetch named the newest release of the repository Orca's
        app-update.yml names, for the Host's Orca: unknown now."""
        rec = self.hostile(self.good(orca={"repo": "stablyai/orca", "release": "1.4.250", "error": None}))
        self.assertEqual(rec["orca"], {"version": None, "error": "unknown error", "pin": None})
        self.assertEqual(rec["npm"]["codex"]["version"], "0.80.0")

    def test_odd_files_make_everything_unknown(self):
        for content in ("not json", "[]", "null", "[" * 100000, json.dumps(self.good()) + " " * (64 << 10),
                        self.good(fetched=int(time.time()) + 86400), self.good(fetched=int(time.time()) - 4 * DAY),
                        self.good(fetched="today"), self.good(fetched=True)):
            with self.subTest(content=str(content)[:60]):
                rec = self.hostile(content)
                self.unknown(rec)
                self.assertEqual(rec["npm"]["codex"]["error"], "fetch stale or unreadable")

    def test_a_link_is_not_followed(self):
        os.makedirs(self.path(os.path.dirname(AVAILABLE)), exist_ok=True)
        self.write("var/lib/agentbox/elsewhere.json", json.dumps(self.good()))
        os.symlink(self.path("var/lib/agentbox/elsewhere.json"), self.path(AVAILABLE))
        self.assertEqual(self.run_agentbox("check-versions")[0], 0)
        self.unknown(self.record())

    def test_a_fifo_does_not_hang_the_check(self):
        os.makedirs(self.path(os.path.dirname(AVAILABLE)), exist_ok=True)
        os.mkfifo(self.path(AVAILABLE))
        self.assertEqual(self.run_agentbox("check-versions")[0], 0)
        self.unknown(self.record())

    def test_without_a_fetch_everything_fetched_is_unknown(self):
        self.run_agentbox("check-versions")
        rec = self.record()
        self.unknown(rec)
        self.assertEqual(rec["npm"]["opencode"]["error"], "not fetched (agentbox-versions-fetch.service)")
        self.assertEqual(rec["orca"]["error"], "not fetched (agentbox-versions-fetch.service)")


class OverviewTest(VersionsTest):
    def test_before_the_first_check_everything_available_is_unknown(self):
        self.built()
        result = self.json("images")
        self.assertEqual(result["available"], {"checked": None, "package_min_age_days": None,
                                               "tools": dict.fromkeys(("orca", "claude-code", "codex", "opencode",
                                                                       "fedora")),
                                               "orca_version": None})
        base = result["images"][0]
        self.assertEqual((base["update"], base["updates"], base["package_updates"]), (None, [], None))
        self.assertIn("Update: unknown (checked daily; now: systemctl start agentbox-versions)",
                      self.ok("images"))
        self.assertIn("Available: not checked yet", self.ok("images"))

    def test_installed_and_available_side_by_side(self):
        self.built()
        self.scenario(check_update={"localhost/agentbox/base:": {"exit": 100, "stdout": "a.x86_64 1-1 updates\n"}})
        self.check()
        result = self.json("images")
        self.assertEqual(result["available"]["tools"], {"orca": "1.4.251", "claude-code": "2.1.7", "codex": "0.80.0",
                                                        "opencode": "1.2.3", "fedora": None})
        self.assertEqual((result["available"]["orca_version"], result["available"]["package_min_age_days"]), (None, 2))
        self.assertRegex(result["available"]["checked"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        base = result["images"][0]
        current = base["builds"][0]
        # a newer Orca release and a newer claude-code: a new Build brings both
        self.assertEqual(current["updates"], ["orca", "claude-code"])
        self.assertEqual((base["update"], base["updates"], base["package_updates"]),
                         (True, ["orca", "claude-code", "packages"], 1))
        # a Build from before, which a VM still uses, is compared too, where it is known
        self.assertEqual(self.builds()[OLD]["updates"], [])

    def test_nothing_new(self):
        self.registry({"npm": {**REGISTRY["npm"], "@anthropic-ai/claude-code": npm("2.1.5", ("2.1.5", 30))},
                       "github": {"stablyai/orca": [release("1.4.242", 1), release("1.4.241", 3)]}})
        self.built()
        self.check()
        base = self.json("images")["images"][0]
        self.assertEqual((base["update"], base["updates"], base["package_updates"]), (False, [], 0))
        self.assertIn("Update: a new Build would bring nothing new\n", self.ok("images"))

    def test_package_updates_counted_for_an_earlier_build_are_unknown_for_the_new_one(self):
        self.built()
        self.scenario(check_update={"localhost/agentbox/base:": {"exit": 100, "stdout": "a.x86_64 1-1 updates\n"}})
        self.check()
        time.sleep(1.1)
        self.built()
        base = self.json("images")["images"][0]
        self.assertEqual(base["package_updates"], None)
        self.assertEqual(base["updates"], ["orca", "claude-code"])

    def test_a_pinned_older_orca_is_an_update_too(self):
        self.built()
        self.ok("set-settings", "orca_version=1.4.240")
        self.registry({**REGISTRY, "github": {"stablyai/orca": [*RELEASES, release("1.4.240", 40)]}})
        self.check()
        result = self.json("images")
        self.assertEqual((result["available"]["tools"]["orca"], result["available"]["orca_version"]),
                         ("1.4.240", "1.4.240"))
        self.assertEqual(result["images"][0]["updates"], ["orca", "claude-code"])

    def test_orca_checked_with_another_pin_than_the_settings_have_now_is_unknown(self):
        """Until the check a changed orca_version starts has run, Orca's version is not
        known: the one checked is not what the next Build takes."""
        self.built()
        self.check()
        self.ok("set-settings", "orca_version=1.4.250")
        available = self.json("images")["available"]
        self.assertEqual((available["tools"]["orca"], available["tools"]["codex"], available["orca_version"]),
                         (None, "0.80.0", "1.4.250"))
        self.assertEqual(self.json("images")["images"][0]["updates"], ["claude-code"])
        self.check()
        self.assertEqual(self.json("images")["available"]["tools"]["orca"], "1.4.250")
        self.ok("set-settings", "orca_version=default")
        self.assertIsNone(self.json("images")["available"]["tools"]["orca"])

    def test_a_record_from_before_shows_orca_as_unknown(self):
        """versions.json of #104 named the Host's Orca and the newest release."""
        self.built()
        self.write(VERSIONS, json.dumps({
            "checked": int(time.time()), "fetched": int(time.time()), "package_min_age_days": 2,
            "npm": {"claude-code": {"version": "2.1.7", "error": None}, "codex": {"version": "0.80.0", "error": None},
                    "opencode": {"version": "1.2.3", "error": None}},
            "orca": {"host": "1.4.245", "repo": "stablyai/orca", "release": "1.4.250", "error": None},
            "packages": {}}))
        result = self.json("images")
        self.assertEqual(result["available"]["tools"], {"orca": None, "claude-code": "2.1.7", "codex": "0.80.0",
                                                        "opencode": "1.2.3", "fedora": None})
        self.assertEqual(result["images"][0]["updates"], ["claude-code"])
        self.assertRegex(self.ok("images"), r"Available \(checked .*\): Orca \?, Claude Code 2\.1\.7")

    def test_a_failed_check_never_blocks_a_build_or_a_reset(self):
        self.write(VERSIONS, "{broken")
        self.built()
        self.assertEqual(self.json("images")["available"]["checked"], None)
        self.ok("reset", "work", "--no-start")

    def test_text(self):
        self.built()
        self.check()
        out = self.ok("images")
        self.assertIn("  Update available: a new Build would bring Orca, Claude Code\n", out)
        self.assertIn("Orca 1.4.241, Claude Code 2.1.5, codex 0.80.0, opencode 1.2.3, Fedora 44\n", out)
        self.assertRegex(out, r"Available \(checked \d{4}-\d\d-\d\d \d\d:\d\d, at least 2 days old\): "
                              r"Orca 1\.4\.251, Claude Code 2\.1\.7, codex 0\.80\.0, opencode 1\.2\.3\n")
        self.ok("set-settings", "orca_version=1.4.250")
        self.check()
        self.assertRegex(self.ok("images"), r"Available \(checked [^)]*, at least 2 days old; Orca pinned to "
                                            r"1\.4\.250 \(orca_version\)\): Orca 1\.4\.250, Claude Code 2\.1\.7")


class ContainerfileTest(unittest.TestCase):
    """The Build installs the agents with the package minimum age the check compares with."""

    def setUp(self):
        with open(os.path.join(REPO, "vm", "Containerfile")) as f:
            self.lines = f.read().replace("\\\n", " ").splitlines()

    def test_the_minimum_age_is_declared_before_it_is_used(self):
        declared = next(i for i, line in enumerate(self.lines) if line.startswith("ARG PACKAGE_MIN_AGE_DAYS="))
        used = [i for i, line in enumerate(self.lines) if "PACKAGE_MIN_AGE_DAYS}" in line
                or "$PACKAGE_MIN_AGE_DAYS" in line]
        self.assertTrue(used)
        self.assertLess(declared, min(used))

    def test_orca_comes_from_its_release_by_the_minimum_age_or_the_pin(self):
        """#113: the Build container picks, downloads and checks Orca (vm/build/orca-release);
        the RPM goes to /usr/lib/orca, where the unit starts it and the version is read."""
        declared = next(i for i, line in enumerate(self.lines) if line.startswith("ARG ORCA_VERSION="))
        [copy] = [i for i, line in enumerate(self.lines) if line.startswith("COPY vm/build/orca-release ")]
        [run] = [i for i, line in enumerate(self.lines) if line.startswith("RUN") and ORCA_RUN in line]
        self.assertLess(declared, run)
        self.assertLess(copy, run)
        line = self.lines[run]
        commands = [c.strip() for c in line[len("RUN"):].split("&&")]
        self.assertEqual(commands[0], 'python3 -I /tmp/orca-release rpm /tmp/orca --min-age-days "$PACKAGE_MIN_AGE_DAYS" '
                                      '--version "$ORCA_VERSION"')
        self.assertIn("dnf -y install --setopt=install_weak_deps=False /tmp/orca/orca-ide-*.x86_64.rpm", commands)
        self.assertIn("mv /opt/Orca /usr/lib/orca", commands)
        self.assertIn("python3 -I /tmp/orca-release runtime /usr/lib/orca /usr/lib/agentbox/orca-runtimes", commands)
        self.assertEqual(commands[-1], "rm -rf /tmp/orca /tmp/orca-release")
        # one chain: any step that fails fails the Build
        self.assertNotIn(";", line)
        self.assertNotIn("||", line)
        self.assertFalse([l for l in self.lines if "/mnt/orca" in l])
        with open(os.path.join(REPO, "vm/files/usr/lib/systemd/system/agentbox-orca.service")) as f:
            self.assertIn("ExecStart=/usr/lib/orca/resources/bin/orca-ide serve", f.read())
        sys.path.insert(0, REPO)
        from agentbox import versions
        self.assertEqual(versions.IMAGE_ORCA, "/usr/lib/orca")

    def test_every_npm_install_takes_versions_from_before_that_age(self):
        installs = [line for line in self.lines if line.startswith("RUN") and "npm install" in line]
        self.assertTrue(installs)
        for line in installs:
            for command in re.findall(r"npm install[^&]*", line):
                self.assertIn('--before "$before"', command)
        computed = re.search(r'before="(\$\(date [^)]*\))"', installs[0]).group(1)
        r = subprocess.run(["sh", "-c", f'PACKAGE_MIN_AGE_DAYS=3; echo "{computed}"'], capture_output=True,
                           text=True, check=True)
        stamp = r.stdout.strip()
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        self.assertLess(abs(time.time() - 3 * DAY - when), 120)


NOTHING_NEW = {"npm": {**REGISTRY["npm"], "@anthropic-ai/claude-code": npm("2.1.5", ("2.1.5", 30))},
               "github": {"stablyai/orca": [release("1.4.242", 1), release("1.4.241", 3)]}}
UPDATE_UNIT = "agentbox-build@update.service"


class UpdateHarness(VersionsTest):
    """A Host with the units and a Build of the Base image: Orca 1.4.241 and Claude Code 2.1.5,
    where the registry and GitHub offer 1.4.251 and 2.1.7."""
    ACTOR = "Firefox/Linux (192.0.2.23)"
    REQUEST = "var/lib/agentbox/builds/request-update.json"

    def setUp(self):
        super().setUp()
        self.ok("regenerate")
        self.built()
        self.forget_calls()

    def timer_check(self, *settings):
        """What the daily timer runs: the fetch, then the root part with the timer as actor."""
        code, out, err = self.run_agentbox("fetch-versions")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_agentbox("--actor=timer", "check-versions")
        self.assertEqual(code, 0, err)
        return out

    def started(self):
        return [c for c in self.systemctl() if c[:2] == ["start", "--no-block"]]

    def request(self):
        with open(self.path(self.REQUEST)) as f:
            return json.load(f)

    def disk(self, vm):
        with open(self.path(f"var/lib/agentbox/vms/{vm}-system.qcow2")) as f:
            return os.path.basename(json.load(f)["backing-filename"])

    def resets(self):
        return [(e["AGENTBOX_VM"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"])
                for e in self.journald.entries() if e.get("AGENTBOX_ACTION") == "reset.update"]

    def run_lock(self):
        """Hold the run lock of the Builds (as a running Build does) until the file is closed."""
        lock = self.path("var/lib/agentbox/locks/build-run.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        f = open(lock, "a")
        self.addCleanup(f.close)
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def effects(self, orca, claude):
        """What the next Build installs: these versions of Orca and Claude Code."""
        claude_files = {**RUN_EFFECTS["@anthropic-ai/claude-code"],
                        "/usr/lib/node_modules/@anthropic-ai/claude-code/package.json": json.dumps({"version": claude})}
        with open(os.path.join(self.dir, "run.json"), "w") as f:
            json.dump({**IMAGE_EFFECTS, ORCA_RUN: orca_effects(asar(orca_package(orca))),
                       "@anthropic-ai/claude-code": claude_files}, f)


class AutoUpdateTest(UpdateHarness):
    """auto_update (#114, on by default): the daily check starts an update of the Images a new
    Build would update at once (agentbox-build@update), never while a Build runs; its Builds
    have the reason "update". The minimum age and the Orca pin still decide what is built."""

    def test_an_update_starts_a_build_at_once(self):
        out = self.timer_check()
        self.assertEqual(self.started(), [["start", "--no-block", UPDATE_UNIT]])
        self.assertEqual(self.request(), {"actor": "timer", "reset": [], "check": False})
        self.assertIn(f"Automatic update started ({UPDATE_UNIT}): Build of base", out)
        [entry] = self.activity("build.automatic")
        self.assertEqual((entry["AGENTBOX_ACTOR"], entry["AGENTBOX_OUTCOME"], entry["AGENTBOX_PARAM_REASON"],
                          entry["AGENTBOX_PARAM_IMAGES"]), ("timer", "ok", "update", "base"))
        self.assertEqual(entry["AGENTBOX_DESCRIPTION"], "Automatic update started: Build of base "
                                                        "(base: Orca 1.4.241 -> 1.4.251, Claude Code 2.1.5 -> 2.1.7)")
        # the unit builds the Base image: Activity says it was for the update, who started it
        old = self.newest()["build"]
        out = self.built("update")
        self.assertIn("== Update: Build of base", out)
        new = self.newest()["build"]
        self.assertNotEqual(new, old)
        [done] = [e for e in self.activity("build.ok") if e["AGENTBOX_PARAM_BUILD"] == new]
        self.assertEqual((done["AGENTBOX_ACTOR"], done["AGENTBOX_PARAM_REASON"]), ("timer", "update"))
        self.assertTrue(done["AGENTBOX_DESCRIPTION"].startswith(f"Update: Build base-{new} done (Release "),
                        done["AGENTBOX_DESCRIPTION"])
        self.assertEqual(self.newest()["actor"], "timer")
        # the Build's log is its unit's journal, as for every Build
        with open(self.path(f"var/lib/agentbox/builds/base-{new}.json")) as f:
            self.assertEqual(json.load(f)["unit"], UPDATE_UNIT)

    def test_package_updates_alone_are_an_update(self):
        self.registry(NOTHING_NEW)
        self.scenario(check_update={"localhost/agentbox/base:": {"exit": 100, "stdout": "a.x86_64 1-1 updates\n"}})
        self.timer_check()
        self.assertEqual(self.started(), [["start", "--no-block", UPDATE_UNIT]])
        [entry] = self.activity("build.automatic")
        self.assertEqual(entry["AGENTBOX_DESCRIPTION"], "Automatic update started: Build of base "
                                                        "(base: 1 package updates)")

    def test_nothing_new_starts_nothing(self):
        self.registry(NOTHING_NEW)
        out = self.timer_check()
        self.assertIn("Automatic update: no Image needs a new Build", out)
        self.assertEqual(self.started(), [])
        self.assertEqual(self.activity("build.automatic"), [])

    def test_off_the_check_only_checks(self):
        self.ok("set-settings", "auto_update=false")
        self.forget_calls()
        out = self.timer_check()
        self.assertNotIn("Automatic update", out)
        self.assertEqual(self.started(), [])
        self.assertEqual(self.activity("build.automatic"), [])
        self.assertTrue(self.json("images")["images"][0]["update"])

    def test_never_while_a_build_or_an_update_runs(self):
        lock = self.run_lock()
        out = self.timer_check()
        self.assertIn("Automatic update of base not started: a Build is running; the next check "
                      "tries again", out)
        lock.close()
        self.scenario(systemctl_active=[UPDATE_UNIT])
        self.timer_check()
        self.assertEqual(self.started(), [])
        self.assertEqual(self.activity("build.automatic"), [])
        self.assertFalse(self.exists(self.REQUEST))
        skipped = self.activity("build.skipped")
        self.assertEqual(len(skipped), 2)
        self.assertEqual(skipped[0]["AGENTBOX_PARAM_REASON"], "update")
        self.assertIn("a Build is running", skipped[0]["AGENTBOX_DESCRIPTION"])

    def test_never_while_the_weekly_rebuild_is_starting(self):
        """agentbox-build@all is active but holds no run lock yet."""
        self.scenario(systemctl_active=["agentbox-build@all.service"])
        out = self.timer_check()
        self.assertIn("not started: a Build is running", out)
        self.assertEqual(self.started(), [])
        self.assertEqual(len(self.activity("build.skipped")), 1)

    def test_a_second_request_is_refused_not_overwritten(self):
        self.timer_check()
        first = self.request()
        self.assertEqual(first["actor"], "timer")
        self.forget_calls()
        r = subprocess.run([AGENTBOX, f"--actor={self.ACTOR}", "update-now"], env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("an update is running already", r.stderr + r.stdout)
        self.assertEqual(self.started(), [])
        self.assertEqual(self.request(), first)

    def test_a_unit_that_cannot_start_is_said_in_activity_and_the_check_ends_well(self):
        os.remove(self.path("etc/systemd/system/agentbox-build@.service"))
        self.timer_check()
        [entry] = self.activity("build.automatic")
        self.assertEqual((entry["AGENTBOX_OUTCOME"], entry["AGENTBOX_DESCRIPTION"], entry["AGENTBOX_ERROR"]),
                         ("failed", "Automatic update of base not started: agentbox-build@.service is missing, "
                                    "run agentbox regenerate first", "not_set_up"))

    def test_a_build_that_brought_it_meanwhile_leaves_nothing_to_build(self):
        """The update decides under the run lock: what a Build that ran while it waited
        brought is not built again."""
        self.timer_check()
        self.ok("set-settings", "auto_update=false")
        self.registry(NOTHING_NEW)
        self.timer_check()
        builds = set(self.builds())
        out = self.built("update")
        self.assertIn("== Update: no Image needs a new Build", out)
        self.assertEqual(set(self.builds()), builds)
        [entry] = self.activity("build.unneeded")
        self.assertEqual((entry["AGENTBOX_ACTOR"], entry["AGENTBOX_PARAM_REASON"], entry["AGENTBOX_DESCRIPTION"]),
                         ("timer", "update", "Update: no Image needs a new Build"))

    def test_with_the_base_image_every_image_is_built_on_it(self):
        self.image("rust", DERIVED)
        self.timer_check()
        out = self.built("update")
        self.assertIn("== Update: Build of base, rust", out)
        self.assertEqual(self.newest("rust")["base_build"], self.newest()["build"])
        self.assertEqual([e["AGENTBOX_PARAM_IMAGE"] for e in self.activity("build.ok")][-2:], ["base", "rust"])

    def test_only_an_image_behind_is_built(self):
        """A derived Image built on an earlier Base Build has the tools of that one: only it is
        built when the Base image has nothing new."""
        self.image("rust", DERIVED)
        self.built("all")
        self.effects("1.4.251", "2.1.7")
        self.built()                      # the Base image has what is available now
        base, rust = self.newest()["build"], self.newest("rust")["build"]
        self.ok("set-settings", "auto_update=false")
        self.timer_check()
        overview = self.overview()
        self.assertEqual((overview["base"]["update"], overview["rust"]["update"]), (False, True))
        out = self.built("update")
        self.assertIn("== Update: Build of rust\n", out)
        self.assertEqual(self.newest()["build"], base)
        self.assertNotEqual(self.newest("rust")["build"], rust)

    def test_the_minimum_age_and_the_orca_pin_still_decide(self):
        self.ok("set-settings", "orca_version=1.4.241")
        self.registry({"npm": NOTHING_NEW["npm"], "github": {"stablyai/orca": [*RELEASES, release("1.4.241", 40)]}})
        self.forget_calls()
        self.timer_check()
        # Orca pinned to what the Build has, and Claude Code has nothing old enough that is newer
        self.assertEqual(self.record()["orca"], {"version": "1.4.241", "error": None, "pin": "1.4.241"})
        self.assertEqual(self.started(), [])
        self.assertEqual(self.activity("build.automatic"), [])


class UpdateNowTest(UpdateHarness):
    """Update now (#114): `update-now --reset <vm>...` starts agentbox-build@update, which checks
    the versions at once (the fetch as agentbox-feeds through its unit, then the root part),
    builds every Image with an update and then resets the VMs picked; a failed Build resets
    nothing."""

    def update_now(self, *vms):
        return self.ok(f"--actor={self.ACTOR}", "update-now", *[a for vm in vms for a in ("--reset", vm)])

    def test_check_build_then_reset(self):
        self.run_agentbox("fetch-versions")        # what the fetch's unit leaves (it is not run here)
        out = self.update_now("work")
        self.assertIn(f"Update started ({UPDATE_UNIT}): checking versions, then building every Image with an update; "
                      "if every Build succeeds, then a Reset of work", out)
        self.assertEqual(self.request(), {"actor": self.ACTOR, "reset": ["work"], "images": {"work": "base"},
                                          "check": True})
        self.assertEqual(self.started(), [["start", "--no-block", UPDATE_UNIT]])
        [entry] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "update-now"]
        self.assertEqual((entry["AGENTBOX_KIND"], entry["AGENTBOX_DESCRIPTION"]),
                         ("build", "Update now: check the versions, build every Image with an update, then a Reset "
                                   "of work"))
        self.assertEqual(entry["AGENTBOX_PARAM_RESET"], "work")
        self.assertFalse(self.exists(VERSIONS))
        out = self.built("update")
        # the check first: the fetch through its unit, then the root part
        calls = [c for c in self.calls() if c["cmd"] in ("systemctl", "podman")]
        fetch = calls.index({"cmd": "systemctl", "argv": ["start", "agentbox-versions-fetch.service"]})
        self.assertLess(fetch, min(i for i, c in enumerate(calls) if c["argv"][0] == "build"))
        self.assertEqual(self.record()["npm"]["claude-code"]["version"], "2.1.7")
        self.assertIn("== Update: Build of base", out)
        new = self.newest()["build"]
        self.assertEqual(self.disk("work"), f"base-{new}.qcow2")
        self.assertEqual(self.resets(), [("work", "ok", f"Reset after the Update onto Build base-{new}, started again")])
        [done] = [e for e in self.activity("build.ok") if e["AGENTBOX_PARAM_BUILD"] == new]
        self.assertEqual((done["AGENTBOX_ACTOR"], done["AGENTBOX_PARAM_REASON"]), (self.ACTOR, "update"))
        # its own check starts no automatic update besides
        self.assertEqual(self.activity("build.automatic"), [])
        self.assertEqual(self.started(), [["start", "--no-block", UPDATE_UNIT]])

    def test_nothing_new_builds_nothing_and_resets_onto_the_current_build(self):
        self.registry(NOTHING_NEW)
        self.run_agentbox("fetch-versions")
        self.system_disk("work", OLD)
        self.update_now("work")
        current = self.newest()["build"]
        out = self.built("update")
        self.assertIn("== Update: no Image needs a new Build", out)
        self.assertEqual(self.newest()["build"], current)
        self.assertEqual(self.disk("work"), f"base-{current}.qcow2")
        self.assertEqual([e["AGENTBOX_ACTOR"] for e in self.activity("build.unneeded")], [self.ACTOR])

    def test_a_failed_build_resets_nothing(self):
        self.run_agentbox("fetch-versions")
        self.system_disk("work", OLD)
        self.update_now("work", "priv")
        self.scenario(podman_fails={"build": ["localhost/agentbox/base:"]})
        code, out = self.build("update")
        self.assertEqual(code, 1, out)
        self.assertIn("== No Reset of work, priv: the Build of base failed", out)
        self.assertEqual(self.disk("work"), f"base-{OLD}.qcow2")
        self.assertEqual(self.resets(), [
            ("work", "failed", "No Reset after the Update: the Build of base failed"),
            ("priv", "failed", "No Reset after the Update: the Build of base failed")])
        [failed] = self.activity("build.failed")
        self.assertEqual(failed["AGENTBOX_PARAM_REASON"], "update")
        self.assertTrue(failed["AGENTBOX_DESCRIPTION"].startswith("Update: Build base-"), failed["AGENTBOX_DESCRIPTION"])

    def test_a_vm_on_another_image_is_reset_onto_its_own(self):
        self.image("rust", DERIVED)
        self.built("rust")
        self.ok("set-image", "priv", "rust")
        self.registry(NOTHING_NEW)
        self.run_agentbox("fetch-versions")
        self.update_now("priv")
        self.assertEqual(self.request()["images"], {"priv": "rust"})
        self.built("update")
        self.assertEqual(self.disk("priv"), f"rust-{self.newest('rust')['build']}.qcow2")

    def test_what_cannot_be_updated_is_refused(self):
        vm_records.write(self.root, record("priv", 2, build=OLD))
        for args, message in ((["--reset", "nope"], "No such VM"),
                              (["--reset", "priv"], f"priv is pinned to Build {OLD}, a Reset would not "
                                                    "bring the new Build"),
                              (["base"], "update-now [--reset <vm>]... [--json]"),
                              (["--all"], "unknown option")):
            with self.subTest(args=args):
                code, out, err = self.run_agentbox("update-now", *args)
                self.assertNotEqual(code, 0)
                self.assertIn(message, err)
        self.scenario(systemctl_active=[UPDATE_UNIT])
        code, _, err = self.run_agentbox("update-now")
        self.assertEqual(code, 1)
        self.assertIn(f"an update is running already (journalctl -fu {UPDATE_UNIT})", err)
        self.assertEqual(self.started(), [])
        self.assertFalse(self.exists(self.REQUEST))
        # images says that one runs
        self.assertTrue(self.json("images")["updating"])
        self.assertIn(f"Update running ({UPDATE_UNIT})", self.ok("images"))
        self.scenario()
        self.assertFalse(self.json("images")["updating"])

    def test_json(self):
        result = self.json("update-now", "--reset=work", "--reset", "work")
        self.assertEqual((result["unit"], result["started"], result["reset"]), (UPDATE_UNIT, True, ["work"]))

    def test_an_image_cannot_be_named_like_a_target(self):
        self.image("update", DERIVED)
        self.assertNotIn("update", [i["name"] for i in self.json("images")["images"]])
        code, _, err = self.run_agentbox("build", "update")
        self.assertEqual(code, 1)
        self.assertIn("invalid Image: update", err)


class TimerTest(VersionsTest):
    def unit(self, name):
        return self.read(f"etc/systemd/system/{name}")

    def test_regenerate_writes_the_daily_check_in_two_parts(self):
        self.ok("regenerate")
        fetch = self.unit("agentbox-versions-fetch.service")
        for line in ("User=agentbox-feeds", "ExecStart=/usr/bin/agentbox fetch-versions",
                     "Environment=AGENTBOX_VERSIONS_MIN_AGE_DAYS=2", "Environment=AGENTBOX_VERSIONS_ORCA_VERSION=",
                     "StateDirectory=agentbox/available",
                     "NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes",
                     "PrivateTmp=yes", "PrivateDevices=yes", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK"):
            self.assertIn(line + "\n", fetch)
        # only available/ of /var/lib/agentbox is visible; no denylist entry for the rest
        self.assertIn("TemporaryFileSystem=/var/lib/agentbox:ro\n", fetch)
        self.assertIn("BindPaths=/var/lib/agentbox/available\n", fetch)
        hidden = re.search(r"(?m)^InaccessiblePaths=(.*)$", fetch).group(1).split()
        for path in ("/etc/agentbox", "/var/lib/libvirt", "/var/lib/containers"):
            self.assertIn("-" + path, hidden)
        self.assertFalse([h for h in hidden if h.startswith("-/var/lib/agentbox")])
        denied = re.search(r"(?m)^IPAddressDeny=(.*)$", fetch).group(1).split()
        for net in ("169.254.0.0/16", "224.0.0.0/4", "10.77.0.0/16", "fe80::/10", "ff00::/8"):
            self.assertIn(net, denied)
        service = self.unit("agentbox-versions.service")
        self.assertIn("ExecStart=/usr/bin/agentbox --actor=timer check-versions\n", service)
        self.assertIn("Environment=HOME=/root\n", service)
        self.assertNotRegex(service, r"(?m)^User=")
        # the fetch first; when it fails, the root part still runs
        self.assertIn("Wants=network-online.target agentbox-versions-fetch.service\n", service)
        self.assertIn("After=network-online.target agentbox-versions-fetch.service\n", service)
        # no timer of its own: the Maintenance window starts it daily, after the Feeds, and waits
        # for the Builds of an automatic update before its Reset check (test_maintenance)
        self.assertFalse(self.exists("etc/systemd/system/agentbox-versions.timer"))
        self.assertNotRegex(service, r"(?m)^\[Install\]")
        # new units: the check runs once at once
        calls = self.systemctl()
        self.assertLess(calls.index(["daemon-reload"]), calls.index(["start", "--no-block", "agentbox-versions.service"]))

    def test_a_new_package_minimum_age_reaches_the_fetch_and_checks_at_once(self):
        self.ok("regenerate")
        self.forget_calls()
        self.ok("set-settings", "package_min_age_days=5")
        self.assertIn("Environment=AGENTBOX_VERSIONS_MIN_AGE_DAYS=5\n", self.unit("agentbox-versions-fetch.service"))
        calls = self.systemctl()
        self.assertLess(calls.index(["daemon-reload"]), calls.index(["start", "--no-block", "agentbox-versions.service"]))

    def test_an_unchanged_regenerate_leaves_it_alone(self):
        self.ok("regenerate")
        self.forget_calls()
        self.ok("regenerate")
        self.assertNotIn(["start", "--no-block", "agentbox-versions.service"], self.systemctl())
        self.assertNotIn(["daemon-reload"], self.systemctl())


if __name__ == "__main__":
    unittest.main()
