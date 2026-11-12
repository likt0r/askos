"""Images and Builds (#17, #96): `agentbox build`, `build-run` (what the unit runs), `images`,
`build-log` and `set-image`, the safeguard check, keeping and pruning Builds, the weekly
rebuild timer and the Builds made before (base-<stamp>.qcow2 and base.qcow2).

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root and fakes on PATH:
podman (fake_podman.py: builds a rootfs from the Containerfile's FROM, COPY and a few RUN
lines, so the repository's own vm/Containerfile and vm/files make the Base image),
systemctl, qemu-img, virsh, virt-install and journalctl. Builds come from the installed
Release (ADR 0010): its Containerfile and guest files lie under the root where a Release
installs them (paths.VM_SOURCE), a copy of this repository's vm/ and the guest tools of
github-app/, plus derived Images a test adds. Activity goes to a stand-in for journald's
socket under the root, never to the Host's journal.

Run from the repo root: python3 -m unittest discover -s tests
"""
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from fake_journald import Journald  # noqa: E402
from test_registry import AGENTBOX, fake  # noqa: E402
from test_vms import VmsTest  # noqa: E402
from vm_records import record  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from agentbox import safeguards  # noqa: E402
with open(os.path.join(REPO, "VERSION")) as f:
    RELEASE = f.read().strip()         # the Release of the CLI under test (a checkout's VERSION)
COMMIT = "abcdef0123456789abcdef0123456789abcdef01"     # of main, named by Builds from before ADR 0010
OLD = "20261101"                       # the Build of the Base image the harness starts with
IMAGES = "var/lib/agentbox/images"
VM_SOURCE = "usr/share/agentbox/vm"    # where the Release a checkout's CLI names installs them
GUEST_TOOLS = ("agentbox-gh-token", "git-credential-agentbox", "gh")    # what vm/Containerfile copies
# what the RUN lines of vm/Containerfile that podman cannot run here leave in the image
RUN_EFFECTS = {
    # npm 12 replaces Fedora's npm, its builtin configuration (globalconfig=/etc/npmrc) included
    "npm@12": {"/usr/lib/node_modules/npm/package.json": '{"name": "npm", "version": "12.1.0"}'},
    "@anthropic-ai/claude-code": {
        "/usr/lib/node_modules/@anthropic-ai/claude-code/package.json": '{"version": "2.1.5"}',
        "/usr/lib/node_modules/@openai/codex/package.json": '{"version": "0.80.0"}',
        "/usr/lib/node_modules/opencode-ai/package.json": '{"version": "1.2.3"}',
        "/usr/lib/node_modules/pnpm/package.json": '{"version": "10.20.0"}',
        "/usr/lib/node_modules/bun/package.json": '{"version": "1.3.1"}'},
}
DERIVED = "ARG BASE\nFROM ${BASE}\n"


def install_release(src, folder):
    """The Containerfile and guest files of the checkout src as a Release installs them
    (paths.VM_SOURCE): the Base image's build context, vm/ and the guest tools of github-app/."""
    shutil.copytree(os.path.join(src, "vm"), os.path.join(folder, "vm"), symlinks=True,
                    ignore=shutil.ignore_patterns("images"))
    os.makedirs(os.path.join(folder, "github-app"))
    for name in GUEST_TOOLS:
        shutil.copy2(os.path.join(src, "github-app", name), os.path.join(folder, "github-app", name))


class ImagesTest(VmsTest):
    def setUp(self):
        super().setUp()
        fake(os.path.join(self.dir, "bin", "podman"), "fake_podman.py")
        fake(os.path.join(self.dir, "bin", "journalctl"), "fake_journalctl.py")
        self.journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.journald.close)
        # no GitHub App, no Orca on the Host: a Build needs neither
        self.source = self.path(VM_SOURCE)
        install_release(REPO, self.source)
        with open(os.path.join(self.dir, "run.json"), "w") as f:
            json.dump(RUN_EFFECTS, f)

    def write(self, rel, content, mode=0o644):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        super().write(rel, content, mode)

    def image(self, name, containerfile, **files):
        """A derived Image in the installed Release: vm/images/<name>/ with its Containerfile and files."""
        folder = os.path.join(self.source, "vm", "images", name)
        os.makedirs(folder, exist_ok=True)
        for rel, content in {"Containerfile": containerfile, **files}.items():
            with open(os.path.join(folder, rel), "w") as f:
                f.write(content)

    def build(self, target="base"):
        """Run a Build as its unit does; (exit code, output) with stderr appended."""
        code, out, err = self.run_agentbox("build-run", target)
        return code, out + err

    def built(self, target="base"):
        code, out = self.build(target)
        self.assertEqual(code, 0, out)
        return out

    def overview(self):
        return {i["name"]: i for i in self.json("images")["images"]}

    def builds(self, image="base"):
        return {b["build"]: b for b in self.overview()[image]["builds"]}

    def newest(self, image="base"):
        return self.overview()[image]["builds"][0]

    def current(self, image="base"):
        return os.readlink(self.path(f"{IMAGES}/{image}.qcow2"))

    def podman(self, verb=None):
        return [c for c in self.calls("podman") if verb is None or c["argv"][0] == verb]

    def activity(self, prefix="build."):
        return [e for e in self.journald.entries() if e.get("AGENTBOX_ACTION", "").startswith(prefix)]

    def rootfs(self, image="base", build=None):
        return os.path.join(self.dir, "podman", f"localhost_agentbox_{image}_{build or self.newest(image)['build']}",
                            "rootfs")

    def system_disk(self, vm, build):
        """A System disk of the VM backed by that Build of the Base image."""
        with open(self.path(f"var/lib/agentbox/vms/{vm}-system.qcow2"), "w") as f:
            json.dump({"backing-filename": self.path(f"{IMAGES}/base-{build}.qcow2")}, f)


class BaseBuildTest(ImagesTest):
    def test_a_build_becomes_the_current_build_of_the_base_image(self):
        out = self.built()
        new = self.newest()
        self.assertRegex(new["build"], r"^\d{8}-\d{6}$")
        self.assertEqual((new["status"], new["current"], new["release"], new["commit"]), ("ok", True, RELEASE, None))
        self.assertEqual(self.current(), f"base-{new['build']}.qcow2")
        disk = self.path(f"{IMAGES}/base-{new['build']}.qcow2")
        self.assertEqual(stat.S_IMODE(os.stat(disk).st_mode), 0o644)
        with open(disk) as f:
            self.assertEqual(json.load(f), {"built-from": f"localhost/agentbox/base:{new['build']}"})
        self.assertIn("all Safeguards present", out)
        self.assertEqual(new["agents"], {"claude-code": "2.1.5", "codex": "0.80.0", "opencode": "1.2.3",
                                         "npm": "12.1.0", "pnpm": "10.20.0", "bun": "1.3.1"})
        # the earlier Build stays (builds_kept 3) but is no longer the current one
        self.assertEqual(self.builds()[OLD]["status"], "migrated")
        self.assertFalse(self.builds()[OLD]["current"])

    def test_podman_builds_the_containerfile_of_the_release_with_the_package_age_of_the_settings(self):
        self.ok("set-settings", "package_min_age_days=5")
        self.built()
        build = self.podman("build")[0]["argv"]
        new = self.newest()["build"]
        self.assertEqual(build[build.index("-t") + 1], f"localhost/agentbox/base:{new}")
        self.assertEqual([build[i + 1] for i, a in enumerate(build) if a == "--build-arg"],
                         ["PACKAGE_MIN_AGE_DAYS=5", "ORCA_VERSION="])
        self.assertIn("--no-cache", build)
        src = build[-1]
        self.assertEqual(build[build.index("-f") + 1], os.path.join(src, "vm", "Containerfile"))
        self.assertEqual(self.newest()["package_min_age_days"], 5)
        rootfs = os.path.join(self.dir, "podman", f"localhost_agentbox_base_{new}", "rootfs")
        with open(os.path.join(rootfs, "etc", "npmrc")) as f:
            self.assertIn("min-release-age=5\n", f.read())

    def test_orca_comes_from_its_release_by_the_settings_not_from_the_host(self):
        """#113: the Containerfile downloads Orca (orca_version, else the newest release at
        least package_min_age_days old); nothing of Orca is mounted from the Host, and a
        build.conf an earlier setup wrote is not read."""
        self.write("etc/agentbox/build.conf", "# Erzeugt von agentbox setup: Orcas Node-Laufzeit fuer die Builds\n"
                   f"ORCA_RUNTIMES={self.path('home/bewr/nothing-here')}\n")
        self.ok("set-settings", "orca_version=1.4.250")
        out = self.built()
        build = self.podman("build")[0]["argv"]
        self.assertEqual([build[i + 1] for i, a in enumerate(build) if a == "--build-arg"],
                         ["PACKAGE_MIN_AGE_DAYS=2", "ORCA_VERSION=1.4.250"])
        self.assertNotIn("-v", build)
        self.assertNotIn("--volume", build)
        self.assertIn("Orca: 1.4.250, pinned in orca_version", out)
        self.assertEqual(self.newest()["orca_version"], "1.4.250")
        # back to automatic
        self.ok("set-settings", "orca_version=")
        time.sleep(1.1)
        self.built()
        build = self.podman("build")[-1]["argv"]
        self.assertIn("ORCA_VERSION=", build)
        self.assertEqual(self.newest()["orca_version"], None)

    def test_bootc_image_builder_makes_the_disk_from_the_checked_image(self):
        self.built()
        new = self.newest()["build"]
        run = self.podman("run")[0]["argv"]
        self.assertIn("quay.io/centos-bootc/bootc-image-builder:latest", run)
        self.assertEqual(run[-1], f"localhost/agentbox/base:{new}")
        self.assertEqual(run[run.index("--type") + 1], "qcow2")
        verbs = [c["argv"][:2] for c in self.podman()]
        self.assertLess(verbs.index(["image", "mount"]), verbs.index(["run", "--rm"]))
        # the work directory is gone
        self.assertEqual([n for n in os.listdir(self.path(IMAGES)) if n.startswith(".")], [])

    def test_a_good_build_is_recorded_in_activity(self):
        self.write("var/lib/agentbox/builds/request-base.json", '{"actor": "Firefox/Linux (192.0.2.23)"}')
        self.built()
        [entry] = self.activity()
        new = self.newest()["build"]
        self.assertEqual((entry["AGENTBOX_KIND"], entry["AGENTBOX_ACTION"], entry["AGENTBOX_OUTCOME"]),
                         ("build", "build.ok", "ok"))
        self.assertEqual(entry["AGENTBOX_ACTOR"], "Firefox/Linux (192.0.2.23)")
        self.assertEqual((entry["AGENTBOX_PARAM_IMAGE"], entry["AGENTBOX_PARAM_BUILD"],
                          entry["AGENTBOX_PARAM_RELEASE"]), ("base", new, RELEASE))
        self.assertIn(f"Build base-{new} done (Release {RELEASE})", entry["AGENTBOX_DESCRIPTION"])
        self.assertFalse(self.exists("var/lib/agentbox/builds/request-base.json"))

    def test_only_the_current_base_build_keeps_its_container_image(self):
        """Derived Images build on it; older ones would only fill podman's storage."""
        self.built()
        time.sleep(1.1)
        self.built()
        new = self.newest()["build"]
        self.assertEqual(os.listdir(os.path.join(self.dir, "podman")), [f"localhost_agentbox_base_{new}"])

    def test_the_base_image_of_this_repository_passes_the_check(self):
        """vm/Containerfile, vm/files and package-safeguards together carry every safeguard."""
        self.built()
        self.assertEqual(self.newest()["problems"], [])
        # bootc container lint refuses /usr/etc in an image
        rootfs = os.path.join(self.dir, "podman", f"localhost_agentbox_base_{self.newest()['build']}", "rootfs")
        self.assertFalse(os.path.lexists(os.path.join(rootfs, "usr", "etc")))


class ReleaseSourceTest(ImagesTest):
    """ADR 0010: a Build takes the Containerfile and guest files of the installed Release."""

    def test_a_build_takes_the_files_of_the_installed_release_and_asks_github_nothing(self):
        fake(os.path.join(self.dir, "bin", "curl"), "fake_github_curl.py")
        with open(os.path.join(self.source, "vm/files/etc/agentbox-marker"), "w") as f:
            f.write("from the installed Release\n")
        self.built()
        with open(os.path.join(self.rootfs(), "etc", "agentbox-marker")) as f:
            self.assertEqual(f.read(), "from the installed Release\n")
        self.assertEqual(self.calls("curl"), [])
        # podman builds a copy, which goes with the Build; the Release's files stay as they are
        context = self.podman("build")[0]["argv"][-1]
        self.assertFalse(context.startswith(self.source + "/"), context)
        self.assertFalse(os.path.exists(context))
        self.assertTrue(os.path.isfile(os.path.join(self.source, "vm/files/etc/agentbox-marker")))

    def test_a_build_names_the_release_it_came_from(self):
        out = self.built()
        self.assertIn(f"== Sources: Release {RELEASE} ({self.source})", out)
        new = self.newest()
        self.assertEqual((new["release"], new["commit"]), (RELEASE, None))
        with open(self.path(f"var/lib/agentbox/builds/base-{new['build']}.json")) as f:
            self.assertNotIn("repository", json.load(f))
        self.assertEqual(self.json("images")["release"], RELEASE)
        self.assertRegex(self.ok("images"), rf"(?m)^  {new['build']} +ok .*  Release {RELEASE}  \(current\)$")

    def test_a_release_without_a_safeguard_is_discarded(self):
        """The checks of ADR 0008 hold for the Release's own files too."""
        os.remove(os.path.join(self.source, "vm/files/usr/lib/agentbox/hooks/pre-push"))
        code, out = self.build()
        self.assertEqual(code, 1, out)
        new = self.newest()
        self.assertEqual((new["status"], new["release"]), ("rejected", RELEASE))
        self.assertTrue(any(p.startswith("Git: /usr/lib/agentbox/hooks/pre-push is missing") for p in new["problems"]),
                        new["problems"])
        self.assertEqual(self.current(), f"base-{OLD}.qcow2")
        self.assertEqual(self.podman("run"), [])          # no disk made

    def test_a_release_without_the_inbox_wrapper_or_with_a_weaker_one_is_discarded(self):
        """#47: agentbox-inbox unpacks `agentbox import` as the agent, with the data filter."""
        path = os.path.join(self.source, "vm/files", safeguards.INBOX_WRAPPER.lstrip("/"))
        with open(path) as f:
            wrapper = f.read()
        problem = (f"Guest agent: {safeguards.INBOX_WRAPPER} is missing or does not unpack as the agent with "
                   "the data filter")
        for what, changed in (("missing", None),
                              ("no data filter", wrapper.replace('filter="data"', 'filter="fully_trusted"')),
                              ("stays root", wrapper.replace("        become(USER)\n", ""))):
            with self.subTest(what):
                if os.path.exists(path):
                    os.remove(path)
                if changed is not None:
                    self.assertNotEqual(changed, wrapper)
                    with open(path, "w") as f:
                        f.write(changed)
                    os.chmod(path, 0o755)
                code, out = self.build()
                self.assertEqual(code, 1, out)
                new = self.newest()
                self.assertEqual(new["status"], "rejected")
                self.assertIn(problem, new["problems"])
                self.assertEqual(self.current(), f"base-{OLD}.qcow2")

    def test_a_release_letting_claude_code_fetch_claudeai_connectors_is_discarded(self):
        """#8: a Base image without ENABLE_CLAUDEAI_MCP_SERVERS=false for services and shells."""
        for rel, line in (("usr/lib/systemd/system.conf.d/agentbox.conf", '  "ENABLE_CLAUDEAI_MCP_SERVERS=false" \\\n'),
                          ("etc/profile.d/agentbox.sh", "export ENABLE_CLAUDEAI_MCP_SERVERS=false\n")):
            path = os.path.join(self.source, "vm/files", rel)
            with open(path) as f:
                content = f.read()
            self.assertIn(line, content)
            with open(path, "w") as f:
                f.write(content.replace(line, ""))
        code, out = self.build()
        self.assertEqual(code, 1, out)
        new = self.newest()
        self.assertEqual(new["status"], "rejected")
        self.assertIn("Claude Code: ENABLE_CLAUDEAI_MCP_SERVERS is not set instead of false", new["problems"])
        self.assertIn("Claude Code: /etc/profile.d/agentbox.sh does not export ENABLE_CLAUDEAI_MCP_SERVERS=false "
                      "for shells", new["problems"])
        self.assertEqual(self.current(), f"base-{OLD}.qcow2")
        self.assertEqual(self.podman("run"), [])          # no disk made

    def test_a_release_with_another_resolver_or_a_proxy_is_discarded(self):
        """ADR 0014: a Base image whose VM resolves elsewhere than through the Host, or whose
        services get proxy variables, is no Build."""
        setup = os.path.join(self.source, "vm/files/usr/lib/agentbox/guest-setup")
        with open(setup) as f:
            content = f.read()
        self.assertIn(safeguards.RESOLVER + "\n", content)
        with open(setup, "w") as f:
            f.write(content.replace(safeguards.RESOLVER, 'echo "nameserver 127.0.0.1" > /etc/resolv.conf'))
        conf = os.path.join(self.source, "vm/files/usr/lib/systemd/system.conf.d/agentbox.conf")
        with open(conf, "a") as f:
            f.write('DefaultEnvironment="https_proxy=http://10.0.0.1:8080" "NODE_USE_ENV_PROXY=1" '
                    '"NODE_OPTIONS=--use-env-proxy"\n')
        resolved = os.path.join(self.source, "vm/files/usr/lib/systemd/resolved.conf.d")
        os.makedirs(resolved)
        with open(os.path.join(resolved, "50-dns.conf"), "w") as f:
            f.write("[Resolve]\nFallbackDNS=9.9.9.9\n")
        code, out = self.build()
        self.assertEqual(code, 1, out)
        new = self.newest()
        self.assertEqual(new["status"], "rejected")
        self.assertIn(f"DNS: guest-setup does not make the Host the only resolver ({safeguards.RESOLVER})",
                      new["problems"])
        self.assertIn("DNS: /usr/lib/systemd/resolved.conf.d/50-dns.conf sets FallbackDNS= (the Host is the only "
                      "resolver)", new["problems"])
        for name in ("NODE_OPTIONS", "NODE_USE_ENV_PROXY", "https_proxy"):
            self.assertIn(f"Proxy: {name} is set; the VM reaches the outside without proxy variables", new["problems"])
        self.assertEqual(self.current(), f"base-{OLD}.qcow2")
        self.assertEqual(self.podman("run"), [])          # no disk made

    def test_builds_from_before_name_the_commit_of_main(self):
        """Records written before ADR 0010 hold the repository and the commit, no Release."""
        build = "20261102-030000"
        self.write(f"{IMAGES}/base-{build}.qcow2", "")
        self.write(f"var/lib/agentbox/builds/base-{build}.json", json.dumps(
            {"image": "base", "build": build, "status": "ok", "step": "done", "started": 1793595600,
             "finished": 1793596800, "repository": "likt0r/askos", "commit": COMMIT,
             "base_build": None, "package_min_age_days": 2, "unit": "agentbox-build@base.service", "actor": "timer",
             "agents": None, "problems": [], "error": None, "safeguards": None, "container": False}))
        old = self.builds()[build]
        self.assertEqual((old["release"], old["commit"]), (None, COMMIT))
        self.assertRegex(self.ok("images"), rf"(?m)^  {build} +ok .*  Commit abcdef012345$")


class SafeguardTest(ImagesTest):
    def setUp(self):
        super().setUp()
        self.built()
        self.base = self.newest()["build"]
        self.journald.raw()
        self.journald.datagrams.clear()
        self.forget_calls()

    def rejected(self, name, *problems):
        code, out = self.build(name)
        self.assertEqual(code, 1, out)
        new = self.newest(name)
        self.assertEqual(new["status"], "rejected", new)
        for p in problems:
            self.assertTrue(any(p in q for q in new["problems"]), (p, new["problems"]))
        self.assertIn("rejected", out)
        self.assertFalse(self.exists(f"{IMAGES}/{name}.qcow2"))
        self.assertFalse(self.exists(f"{IMAGES}/{name}-{new['build']}.qcow2"))
        self.assertIn(["rmi", "--force", "--ignore", f"localhost/agentbox/{name}:{new['build']}"],
                      [c["argv"] for c in self.podman("rmi")])
        self.assertEqual(self.podman("run"), [])          # no disk made
        [entry] = self.activity()
        self.assertEqual((entry["AGENTBOX_ACTION"], entry["AGENTBOX_OUTCOME"]), ("build.rejected", "failed"))
        return new

    def test_a_derived_image_builds_on_the_current_base_build(self):
        self.image("rust", DERIVED + "RUN dnf -y install clang\nCOPY tool /usr/bin/tool\n", tool="#!/bin/sh\n")
        self.built("rust")
        new = self.newest("rust")
        self.assertEqual((new["status"], new["base_build"], new["current"]), ("ok", self.base, True))
        build = self.podman("build")[0]["argv"]
        self.assertEqual(build[build.index("--build-arg") + 1], f"BASE=localhost/agentbox/base:{self.base}")
        self.assertEqual(build[-1], os.path.join(build[-1].rsplit("/vm/", 1)[0], "vm", "images", "rust"))
        self.assertEqual(self.current("rust"), f"rust-{new['build']}.qcow2")
        # its container image goes, the Base Build's stays for the next derived Image
        self.assertIn(["rmi", "--force", "--ignore", f"localhost/agentbox/rust:{new['build']}"],
                      [c["argv"] for c in self.podman("rmi")])
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "podman", f"localhost_agentbox_base_{self.base}")))

    def test_harmless_additions_where_the_environment_is_read_are_fine(self):
        self.image("cuda", DERIVED + "COPY cuda.sh /etc/profile.d/cuda.sh\n"
                   "COPY managed.json /etc/claude-code/managed-settings.json\n",
                   **{"cuda.sh": "export PATH=/usr/local/cuda/bin:$PATH\n",
                      "managed.json": '{"permissions": {"deny": ["Read(./.env)"]}}\n'})
        self.built("cuda")

    def test_a_weakened_npm_configuration_is_rejected(self):
        self.image("weak", DERIVED + "COPY npmrc /etc/npmrc\n", npmrc="ignore-scripts=false\nmin-release-age=0\n")
        self.rejected("weak", "npm: ignore-scripts is not true", "npm: min-release-age is 0 instead of 2",
                      "/etc/npmrc differs from the Base image")

    def test_npm_reading_another_global_configuration_is_rejected(self):
        # what `npm install -g npm@latest` does: npm's builtin configuration goes
        self.image("weak", DERIVED + "RUN rm -f /usr/lib/node_modules/npm/npmrc\n")
        self.rejected("weak", "npm: global configuration /usr/etc/npmrc is missing",
                      "/usr/lib/node_modules/npm/npmrc differs from the Base image")

    def test_an_older_npm_is_rejected(self):
        self.image("weak", DERIVED + "COPY package.json /usr/lib/node_modules/npm/package.json\n",
                   **{"package.json": '{"version": "10.9.0"}'})
        self.rejected("weak", "npm: version 10.9.0")

    def test_a_shorter_package_age_for_uv_pnpm_and_bun_is_rejected(self):
        self.image("weak", DERIVED + "COPY uv.toml /etc/uv/uv.toml\nCOPY bunfig.toml /usr/lib/agentbox/bunfig.toml\n"
                   "COPY zz.conf /etc/systemd/system.conf.d/zz.conf\n",
                   **{"uv.toml": 'exclude-newer = "1 days"\n', "bunfig.toml": "[install]\nminimumReleaseAge = 0\n",
                      "zz.conf": '[Manager]\nDefaultEnvironment="UV_EXCLUDE_NEWER=1 days" '
                                 '"pnpm_config_minimum_release_age=0"\n'})
        self.rejected("weak", "uv: exclude-newer in /etc/uv/uv.toml is 1 days instead of 2 days",
                      "uv: UV_EXCLUDE_NEWER is 1 days instead of 2 days",
                      "pnpm: pnpm_config_minimum_release_age is 0 instead of 2880",
                      "Bun: minimumReleaseAge", "/etc/systemd/system.conf.d/zz.conf is new or changed")

    def test_switching_a_safeguard_off_through_the_environment_is_rejected(self):
        self.image("weak", DERIVED + "COPY zz.conf /usr/lib/systemd/system.conf.d/zz.conf\n",
                   **{"zz.conf": "[Manager]\nDefaultEnvironment=npm_config_ignore_scripts=false\n"})
        self.rejected("weak", "Environment: npm_config_ignore_scripts overrides a Safeguard")

    def test_a_shell_profile_mentioning_a_safeguard_is_rejected(self):
        self.image("weak", DERIVED + "COPY npm.sh /etc/profile.d/npm.sh\n",
                   **{"npm.sh": "export npm_config_ignore_scripts=false\n"})
        self.rejected("weak", "/etc/profile.d/npm.sh is new or changed and concerns a Safeguard")

    def test_the_base_image_keeps_claude_code_from_fetching_claudeai_connectors(self):
        """#8: every service gets ENABLE_CLAUDEAI_MCP_SERVERS=false from systemd (orca serve, the
        agents and terminals it starts), shells from the profile, after the token it sources."""
        self.assertEqual(self.builds()[self.base]["problems"], [])
        rootfs = self.rootfs("base", self.base)
        with open(os.path.join(rootfs, "usr/lib/systemd/system.conf.d/agentbox.conf")) as f:
            self.assertIn('"ENABLE_CLAUDEAI_MCP_SERVERS=false"', f.read())
        with open(os.path.join(rootfs, "etc/profile.d/agentbox.sh")) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[-1], "export ENABLE_CLAUDEAI_MCP_SERVERS=false")
        self.assertLess(next(i for i, line in enumerate(lines) if "/run/agentbox/agent-env" in line), len(lines) - 1)

    def test_claude_code_switched_back_to_claudeai_connectors_is_rejected(self):
        self.image("weak", DERIVED + "COPY zz.conf /etc/systemd/system.conf.d/zz.conf\n",
                   **{"zz.conf": "[Manager]\nDefaultEnvironment=ENABLE_CLAUDEAI_MCP_SERVERS=true\n"})
        self.rejected("weak", "Claude Code: ENABLE_CLAUDEAI_MCP_SERVERS is true instead of false",
                      "/etc/systemd/system.conf.d/zz.conf is new or changed and concerns a Safeguard")

    def test_a_shell_profile_or_claude_code_setting_naming_claudeai_connectors_is_rejected(self):
        self.image("weak", DERIVED + "COPY claude.sh /etc/profile.d/claude.sh\n"
                   "COPY managed.json /etc/claude-code/managed-settings.json\n"
                   "COPY team.json /etc/claude-code/managed-settings.d/10-team.json\n",
                   **{"claude.sh": "unset ENABLE_CLAUDEAI_MCP_SERVERS\n",
                      "managed.json": '{"env": {"ENABLE_CLAUDEAI_MCP_SERVERS": "true"}}\n',
                      "team.json": '{"disableClaudeAiConnectors": false}\n'})
        self.rejected("weak", "/etc/profile.d/claude.sh is new or changed and concerns a Safeguard",
                      "/etc/claude-code/managed-settings.json is new or changed and concerns a Safeguard",
                      "/etc/claude-code/managed-settings.d/10-team.json is new or changed and concerns a Safeguard")

    def test_claude_code_settings_hiding_claudeai_connectors_behind_json_escapes_are_rejected(self):
        """Claude Code decodes JSON escapes: the check reads its settings the same way."""
        managed = '{"env": {"ENABLE_CL\\u0041UDEAI_MCP_SERVERS": "true"}}\n'
        team = '{"disable\\u0043laude\\u0041iConnectors": false}\n'
        self.assertNotRegex(managed + team, "(?i)claudeai")
        self.image("weak", DERIVED + "COPY managed.json /etc/claude-code/managed-settings.json\n"
                   "COPY team.json /etc/claude-code/managed-settings.d/10-team.json\n"
                   "COPY broken.json /etc/claude-code/managed-settings.d/20-broken.json\n",
                   **{"managed.json": managed, "team.json": team, "broken.json": '{"env": {"A": "1"\n'})
        self.rejected("weak", "/etc/claude-code/managed-settings.json is new or changed and concerns a Safeguard",
                      "/etc/claude-code/managed-settings.d/10-team.json is new or changed and concerns a Safeguard",
                      "/etc/claude-code/managed-settings.d/20-broken.json is new or changed and is not valid JSON")

    def test_a_changed_profile_of_the_agent_box_is_rejected(self):
        with open(os.path.join(REPO, "vm/files/etc/profile.d/agentbox.sh")) as f:
            changed = f.read() + "unset ENABLE_CLAUDEAI_MCP_SERVERS\n"
        self.image("weak", DERIVED + "COPY agentbox.sh /etc/profile.d/agentbox.sh\n", **{"agentbox.sh": changed})
        self.rejected("weak", "Claude Code: /etc/profile.d/agentbox.sh does not export ENABLE_CLAUDEAI_MCP_SERVERS=false",
                      "/etc/profile.d/agentbox.sh differs from the Base image")

    def test_git_hooks_from_repositories_are_rejected(self):
        self.image("weak", DERIVED + "COPY gitconfig /etc/gitconfig\n",
                   gitconfig="[core]\n\thooksPath = .githooks\n")
        self.rejected("weak", "Git: core.hooksPath in /etc/gitconfig is .githooks")

    def test_a_removed_pre_push_hook_is_rejected(self):
        self.image("weak", DERIVED + "RUN rm -f /usr/lib/agentbox/hooks/pre-push\n")
        self.rejected("weak", "Git: /usr/lib/agentbox/hooks/pre-push is missing",
                      "/usr/lib/agentbox/hooks/pre-push is missing (compared with the Base image)")

    def test_the_base_image_resolves_through_the_host_and_sets_no_proxy(self):
        """ADR 0014: guest-setup writes the Host's address on the VM's network as the only
        nameserver, NetworkManager leaves it alone, and no service or shell has a proxy."""
        self.assertEqual(self.builds()[self.base]["problems"], [])
        rootfs = self.rootfs("base", self.base)
        with open(os.path.join(rootfs, safeguards.GUEST_SETUP.lstrip("/"))) as f:
            setup = f.read()
        self.assertIn('gw="${ip%.*}.1"\n', setup)
        self.assertLess(setup.index('gw="${ip%.*}.1"'), setup.index(safeguards.RESOLVER))
        self.assertIn("rm -f /etc/resolv.conf\n" + safeguards.RESOLVER + "\n", setup)
        self.assertNotIn("127.0.0.1", setup)
        env = safeguards.systemd_environment(rootfs)
        self.assertEqual([n for n in env if re.search(r"(?i)proxy", n + "=" + env[n])], [])
        with open(os.path.join(rootfs, "etc/profile.d/agentbox.sh")) as f:
            self.assertNotRegex(f.read(), r"(?i)proxy")

    def test_the_agents_podman_storage_lives_on_the_data_disk(self):
        """ADR 0016: guest-setup binds the agent's whole Podman storage from the Data disk
        at every start, created with the agent as owner on the first; nothing else of
        Podman's user configuration is kept."""
        rootfs = self.rootfs("base", self.base)
        with open(os.path.join(rootfs, safeguards.GUEST_SETUP.lstrip("/"))) as f:
            setup = f.read()
        persist = setup.split("PERSIST=(", 1)[1].split(")", 1)[0].split()
        self.assertIn(".local/share/containers", persist)
        self.assertNotIn(".config/containers", persist)
        # every lasting part is created with the agent as owner and bound from the Data disk
        self.assertIn('install -d -o agent -g agent -m 700 "$DATA/home/$p" "$HOME_DIR/$p"', setup)
        self.assertIn('mountpoint -q "$HOME_DIR/$p" || mount --bind "$DATA/home/$p" "$HOME_DIR/$p"',
                      setup)

    def test_a_proxy_variable_or_option_is_rejected(self):
        self.image("weak", DERIVED + "COPY zz.conf /etc/systemd/system.conf.d/zz.conf\n"
                   "COPY proxy.sh /etc/profile.d/proxy.sh\nCOPY node.sh /etc/profile.d/node.sh\n"
                   "COPY java.sh /etc/profile.d/java.sh\n",
                   **{"zz.conf": '[Manager]\nDefaultEnvironment="HTTPS_PROXY=http://10.0.0.1:8080" '
                                 '"_JAVA_OPTIONS=-Dhttps.proxyHost=10.0.0.1" "NODE_OPTIONS=--use-env-proxy" '
                                 '"JAVA_TOOL_OPTIONS=-Djava.net.useSystemProxies=true"\n',
                      "proxy.sh": "export all_proxy=socks5://10.0.0.1:1080\n",
                      "node.sh": "export NODE_OPTIONS=--use-env-proxy\n",
                      "java.sh": "export JDK_JAVA_OPTIONS=-Djava.net.useSystemProxies=true\n"})
        self.rejected("weak", "Proxy: HTTPS_PROXY is set; the VM reaches the outside without proxy variables",
                      "Proxy: _JAVA_OPTIONS is set", "Proxy: NODE_OPTIONS is set", "Proxy: JAVA_TOOL_OPTIONS is set",
                      "/etc/systemd/system.conf.d/zz.conf is new or changed and concerns a Safeguard",
                      "/etc/profile.d/proxy.sh is new or changed and concerns a Safeguard",
                      "/etc/profile.d/node.sh is new or changed and concerns a Safeguard",
                      "/etc/profile.d/java.sh is new or changed and concerns a Safeguard")

    def test_another_resolver_is_rejected(self):
        with open(os.path.join(REPO, "vm/files/usr/lib/agentbox/guest-setup")) as f:
            changed = f.read().replace(safeguards.RESOLVER, 'echo "nameserver 9.9.9.9" > /etc/resolv.conf')
        self.image("weak", DERIVED + "COPY guest-setup /usr/lib/agentbox/guest-setup\n", **{"guest-setup": changed})
        self.rejected("weak", "DNS: guest-setup does not make the Host the only resolver",
                      "/usr/lib/agentbox/guest-setup differs from the Base image")

    def test_dns_servers_for_systemd_resolved_are_rejected(self):
        self.image("weak", DERIVED + "COPY resolved.conf /etc/systemd/resolved.conf\n"
                   "COPY dns.conf /etc/systemd/resolved.conf.d/dns.conf\n",
                   **{"resolved.conf": "[Resolve]\nFallbackDNS=1.1.1.1\n",
                      "dns.conf": "[Resolve]\nDNS=9.9.9.9\n"})
        self.rejected("weak", "DNS: /etc/systemd/resolved.conf sets FallbackDNS=",
                      "DNS: /etc/systemd/resolved.conf.d/dns.conf sets DNS=",
                      "/etc/systemd/resolved.conf is new or changed and concerns a Safeguard",
                      "/etc/systemd/resolved.conf.d/dns.conf is new or changed and concerns a Safeguard")

    def test_dns_switched_on_is_rejected(self):
        self.image("weak", DERIVED + "COPY nm.conf /etc/NetworkManager/conf.d/agentbox.conf\n",
                   **{"nm.conf": "[main]\ndns=default\n"})
        self.rejected("weak", "DNS: dns=none is missing")

    def test_a_changed_guest_agent_wrapper_is_rejected(self):
        self.image("weak", DERIVED + "COPY journal /usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-journal\n",
                   journal="#!/bin/sh\nexec /bin/sh -c \"$@\"\n")
        self.rejected("weak", "Guest agent: /usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-journal",
                      "agentbox-journal differs (compared with the Base image)")

    def test_a_changed_inbox_wrapper_is_rejected(self):
        with open(os.path.join(REPO, "vm/files", safeguards.INBOX_WRAPPER.lstrip("/"))) as f:
            changed = f.read().replace('CODE = "/var/home/agent/code"', 'CODE = "/var/home/agent"')
        self.image("weak", DERIVED + f"COPY inbox {safeguards.INBOX_WRAPPER}\n", inbox=changed)
        self.rejected("weak", "agentbox-inbox differs (compared with the Base image)")

    def test_an_image_not_built_on_the_base_build_is_rejected(self):
        self.image("weak", "FROM quay.io/fedora/fedora-bootc:44\nRUN dnf -y install clang\n")
        self.rejected("weak", f"not built on the Base Build {self.base}", "npm: global configuration")

    def test_a_link_leading_out_of_the_image_counts_as_missing(self):
        """An absolute link is resolved inside the Image, never on the Host."""
        self.write("etc/npmrc-of-the-host", "ignore-scripts=true\nmin-release-age=2\n")
        self.image("weak", DERIVED + "RUN rm -f /etc/npmrc\n")
        with open(os.path.join(self.dir, "run.json")) as f:
            effects = json.load(f)
        effects["rm -f /etc/npmrc"] = {"/etc/npmrc": {"symlink": self.path("etc/npmrc-of-the-host")}}
        with open(os.path.join(self.dir, "run.json"), "w") as f:
            json.dump(effects, f)
        self.rejected("weak", "npm: global configuration /etc/npmrc is missing")

    def test_a_derived_image_needs_a_checked_base_build(self):
        os.remove(self.path(f"{IMAGES}/base.qcow2"))
        os.symlink(f"base-{OLD}.qcow2", self.path(f"{IMAGES}/base.qcow2"))
        self.image("rust", DERIVED)
        code, out = self.build("rust")
        self.assertEqual(code, 1)
        self.assertIn("the Base image has no checked Build, run agentbox build base first", out)
        self.assertEqual(self.newest("rust")["status"], "failed")

    def test_an_image_missing_in_the_release_fails(self):
        code, out = self.build("nope")
        self.assertEqual(code, 1)
        self.assertIn(f"Image nope does not exist in Release {RELEASE}", out)


class AllTest(ImagesTest):
    def test_all_builds_the_base_image_then_every_other_image_on_it(self):
        self.image("rust", DERIVED)
        self.image("weak", DERIVED + "COPY npmrc /etc/npmrc\n", npmrc="ignore-scripts=false\n")
        code, out = self.build("all")
        self.assertEqual(code, 1, out)               # one of them was rejected
        images = self.overview()
        base = images["base"]["builds"][0]
        self.assertEqual(base["status"], "ok")
        self.assertEqual(images["rust"]["builds"][0]["base_build"], base["build"])
        self.assertEqual(images["rust"]["builds"][0]["status"], "ok")
        self.assertEqual(images["weak"]["builds"][0]["status"], "rejected")
        # one copy of the Release's files for all of them
        contexts = [c["argv"][-1] for c in self.podman("build")]
        self.assertEqual([c.rsplit("/", 1)[-1] for c in contexts], ["src", "rust", "weak"])
        self.assertEqual({c.rsplit("/src", 1)[0] for c in contexts}, {contexts[0].rsplit("/src", 1)[0]})
        self.assertEqual(sorted(e["AGENTBOX_ACTION"] for e in self.activity()), ["build.ok", "build.ok",
                                                                                 "build.rejected"])
        self.assertEqual({b["release"] for i in images.values() for b in i["builds"][:1]}, {RELEASE})


class FailedBuildTest(ImagesTest):
    def failed(self, target="base"):
        code, out = self.build(target)
        self.assertEqual(code, 1, out)
        self.assertIn(f"The previous Build stays in use ({OLD})", out)
        self.assertEqual(self.current(), f"base-{OLD}.qcow2")
        new = self.newest()
        self.assertEqual(new["status"], "failed")
        [entry] = self.activity()
        self.assertEqual((entry["AGENTBOX_ACTION"], entry["AGENTBOX_OUTCOME"]), ("build.failed", "failed"))
        # a Reset still gets the previous Build
        self.assertIn(f"work: fresh System disk (base-{OLD}.qcow2)", self.ok("reset", "work", "--no-start"))
        return new, entry

    def test_a_failing_podman_build_leaves_the_previous_build_in_use(self):
        self.scenario(podman_fails={"build": ["localhost/agentbox/base:"]})
        new, entry = self.failed()
        self.assertEqual(new["error"], "podman build failed (exit 1)")
        self.assertIn(f"Build base-{new['build']} failed: podman build failed", entry["AGENTBOX_DESCRIPTION"])
        # a failed step of the Build has no error code: the action says what failed, the
        # message how
        self.assertEqual((entry["AGENTBOX_PARAM_IMAGE"], entry["AGENTBOX_PARAM_BUILD"]), ("base", new["build"]))
        self.assertTrue(entry["AGENTBOX_PARAM_MESSAGE"].startswith("podman build failed"), entry)
        self.assertNotIn("AGENTBOX_ERROR", entry)
        self.assertFalse(self.exists(f"{IMAGES}/base-{new['build']}.qcow2"))

    def test_a_failing_disk_build_leaves_the_previous_build_in_use(self):
        self.scenario(podman_fails={"run": ["localhost/agentbox/base:"]})
        new, _ = self.failed()
        self.assertEqual(new["error"], "bootc-image-builder failed (exit 1)")

    def test_without_the_files_of_the_release_the_previous_build_stays_in_use(self):
        shutil.rmtree(self.source)
        new, entry = self.failed()
        self.assertEqual((new["step"], new["release"], new["error"]),
                         ("source", None, f"{self.source}/vm/Containerfile is missing: the Release is not fully "
                                          "installed"))
        self.assertIn("failed: " + new["error"], entry["AGENTBOX_DESCRIPTION"])
        self.assertEqual(self.podman(), [])

    def test_a_failed_build_shows_in_the_overview(self):
        self.scenario(podman_fails={"build": ["localhost/agentbox/base:"]})
        self.build()
        out = self.ok("images")
        self.assertRegex(out, r"(?m)^  \d{8}-\d{6} +failed .* podman build failed \(exit 1\)$")
        self.assertEqual(self.json("images")["images"][0]["current"], OLD)

    def test_an_interrupted_build_is_marked_failed_by_the_next_one(self):
        self.write("var/lib/agentbox/builds/base-20261102-100000.json", json.dumps(
            {"image": "base", "build": "20261102-100000", "status": "running", "step": "disk",
             "started": 1793600000, "finished": None}))
        os.makedirs(self.path(f"{IMAGES}/.build-base-20261102-100000/qcow2"))
        self.built()
        self.assertEqual(self.builds()["20261102-100000"]["status"], "failed")
        self.assertEqual(self.builds()["20261102-100000"]["error"], "interrupted")
        self.assertFalse(self.exists(f"{IMAGES}/.build-base-20261102-100000"))


class KeepTest(ImagesTest):
    def several(self, n):
        out = []
        for _ in range(n):
            self.built()
            out.append(self.newest()["build"])
            time.sleep(1.1)       # Build ids are per second
        return out

    def files(self):
        return sorted(n for n in os.listdir(self.path(IMAGES)) if n.startswith("base-"))

    def test_only_the_newest_builds_kept_are_kept(self):
        self.ok("set-settings", "builds_kept=2")
        a, b, c = self.several(3)
        self.assertEqual(self.files(), [f"base-{b}.qcow2", f"base-{c}.qcow2"])
        self.assertEqual(sorted(self.builds()), [b, c])
        self.assertIn("1 old Build removed", self.activity()[-1]["AGENTBOX_DESCRIPTION"])
        self.assertEqual(self.activity()[-1]["AGENTBOX_PARAM_REMOVED"], "1")

    def test_builds_in_use_are_never_deleted(self):
        self.ok("set-settings", "builds_kept=1")
        self.system_disk("work", OLD)
        a, = self.several(1)
        self.system_disk("priv", a)
        b, c = self.several(2)
        self.assertEqual(self.files(), [f"base-{OLD}.qcow2", f"base-{a}.qcow2", f"base-{c}.qcow2"])
        builds = self.builds()
        self.assertEqual(builds[OLD]["used_by"], ["work"])
        self.assertEqual(builds[a]["used_by"], ["priv"])

    def test_builds_a_vm_is_pinned_to_are_never_deleted(self):
        self.ok("set-settings", "builds_kept=1")
        a, = self.several(1)
        self.ok("set-image", "priv", "base", "--build", a)
        self.several(2)
        self.assertIn(f"base-{a}.qcow2", self.files())
        self.assertEqual(self.builds()[a]["pinned_by"], ["priv"])

    def test_nothing_is_deleted_while_a_system_disk_cannot_be_read(self):
        self.ok("set-settings", "builds_kept=1")
        self.write("var/lib/agentbox/vms/work-system.qcow2", "", mode=0)
        out = self.built()
        self.assertIn("Old Builds stay: not every System disk is readable", out)
        self.assertEqual(len(self.files()), 2)
        self.assertIsNone(self.builds()[OLD]["used_by"])

    def test_failed_builds_beyond_builds_kept_are_forgotten(self):
        self.ok("set-settings", "builds_kept=1")
        self.scenario(podman_fails={"build": ["localhost/agentbox/base:"]})
        for _ in range(3):
            self.build()
            time.sleep(1.1)
        self.scenario()
        self.built()
        self.assertEqual(sorted(b["status"] for b in self.builds().values()), ["failed", "ok"])


class PinTest(ImagesTest):
    def reset(self, vm="priv"):
        return self.ok("reset", vm, "--no-start")

    def test_rollback_of_a_vm_to_an_earlier_build(self):
        self.built()
        new = self.newest()["build"]
        self.assertIn(f"priv: fresh System disk (base-{new}.qcow2)", self.reset())
        out = self.ok("set-image", "priv", "base", "--build", OLD)
        self.assertIn("applies from the next Reset", out)
        self.assertEqual(vm_records.read(self.root, "priv")["build"], OLD)
        self.assertIn(f"priv: fresh System disk (base-{OLD}.qcow2)", self.reset())
        self.assertIn(f"work: fresh System disk (base-{new}.qcow2)", self.reset("work"))
        # back to the current Build
        self.ok("set-image", "priv", "base")
        self.assertIsNone(vm_records.read(self.root, "priv")["build"])
        self.assertIn(f"priv: fresh System disk (base-{new}.qcow2)", self.reset())

    def test_a_change_applies_at_the_next_reset_only(self):
        self.built()
        self.forget_calls()
        self.ok("set-image", "priv", "base", "--build", OLD)
        # nothing touches the VM or its disks until it is reset
        self.assertEqual([c for c in self.calls() if "dom" in c], [])
        self.assertFalse([c for c in self.calls("qemu-img") if c["argv"][0] == "create"])
        self.assertEqual(self.builds()[OLD]["pinned_by"], ["priv"])

    def test_a_vm_moves_to_another_image(self):
        self.built()
        self.image("rust", DERIVED)
        self.built("rust")
        rust = self.newest("rust")["build"]
        self.ok("set-image", "priv", "rust")
        self.assertIn(f"priv: fresh System disk (rust-{rust}.qcow2)", self.reset())
        self.assertEqual(self.overview()["rust"]["vms"], ["priv"])

    def test_only_builds_that_exist_and_passed_can_be_chosen(self):
        self.scenario(podman_fails={"build": ["localhost/agentbox/base:"]})
        self.build()
        failed = self.newest()["build"]
        for args, message in ((["priv", "base", "--build", failed], f"Build {failed} of Image base does not exist"),
                              (["priv", "base", "--build", "20250101-0000"], "does not exist"),
                              (["priv", "rust"], "Image rust has no Build, run agentbox build rust first"),
                              (["priv", "Base"], "invalid Image"), (["priv", "base", "--build", "x"],
                                                                        "invalid Build"),
                              (["nope", "base"], "No such VM"), (["priv"], "set-image <vm> <image>")):
            with self.subTest(args=args):
                code, out, err = self.run_agentbox("set-image", *args)
                self.assertNotEqual(code, 0)
                self.assertIn(message, err)
        self.assertEqual(vm_records.read(self.root, "priv")["build"], None)

    def test_a_pinned_build_that_is_gone_stops_the_reset(self):
        vm_records.write(self.root, record("priv", 2, build="20250101-0000"))
        code, out, err = self.run_agentbox("reset", "priv", "--no-start")
        self.assertEqual(code, 1)
        self.assertIn("priv: Build 20250101-0000 of Image base does not exist", err)

    def test_setting_the_image_is_recorded_in_activity(self):
        self.ok("--actor=Firefox/Linux (192.0.2.23)", "set-image", "priv", "base", "--build", OLD)
        [entry] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "set-image"]
        self.assertEqual((entry["AGENTBOX_KIND"], entry["AGENTBOX_VM"]), ("build", "priv"))
        self.assertIn(f"Image base, pinned to Build {OLD}, from the next Reset", entry["AGENTBOX_DESCRIPTION"])
        self.assertEqual((entry["AGENTBOX_PARAM_IMAGE"], entry["AGENTBOX_PARAM_BUILD"]), ("base", OLD))
        self.ok("set-image", "priv", "base", "--build", OLD)     # unchanged: no entry
        self.assertEqual(len([e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "set-image"]), 1)


class MigrationTest(ImagesTest):
    """The Builds made before (vm/build-disk.sh): base-<YYYYMMDD-HHMM>.qcow2 and the link base.qcow2."""

    def setUp(self):
        super().setUp()
        for n in ("base.qcow2", f"base-{OLD}.qcow2"):
            os.remove(self.path(f"{IMAGES}/{n}"))
        for stamp in ("20261018-0312", "20261025-0310"):
            self.write(f"{IMAGES}/base-{stamp}.qcow2", "")
        os.symlink("base-20261025-0310.qcow2", self.path(f"{IMAGES}/base.qcow2"))
        self.system_disk("work", "20261018-0312")
        self.system_disk("priv", "20261025-0310")

    def test_they_are_builds_of_the_base_image(self):
        builds = self.builds()
        self.assertEqual(sorted(builds), ["20261018-0312", "20261025-0310"])
        self.assertEqual({b["status"] for b in builds.values()}, {"migrated"})
        self.assertTrue(builds["20261025-0310"]["current"])
        self.assertEqual(builds["20261018-0312"]["used_by"], ["work"])
        self.assertEqual(builds["20261025-0310"]["used_by"], ["priv"])
        self.assertEqual(self.overview()["base"]["vms"], ["priv", "work"])

    def test_existing_system_disks_keep_working(self):
        self.assertIn("work: fresh System disk (base-20261025-0310.qcow2)", self.ok("reset", "work", "--no-start"))
        self.ok("set-image", "priv", "base", "--build", "20261018-0312")
        self.assertIn("priv: fresh System disk (base-20261018-0312.qcow2)", self.ok("reset", "priv", "--no-start"))

    def test_a_new_build_replaces_them_and_unused_ones_go(self):
        self.ok("set-settings", "builds_kept=1")
        self.built()
        new = self.newest()["build"]
        self.assertEqual(self.current(), f"base-{new}.qcow2")
        # both are still System disks' backing files
        self.assertEqual(sorted(self.builds()), ["20261018-0312", "20261025-0310", new])
        self.ok("reset", "work", "--no-start")
        self.ok("reset", "priv", "--no-start")
        time.sleep(1.1)
        self.built()
        # now unused, beyond builds_kept: gone; the first new one stays as long as work and priv use it
        self.assertEqual(sorted(self.builds()), [new, self.newest()["build"]])
        self.assertFalse(self.exists(f"{IMAGES}/base-20261018-0312.qcow2"))


class StartTest(ImagesTest):
    def setUp(self):
        super().setUp()
        self.ok("regenerate")
        self.forget_calls()

    def test_build_starts_the_unit_of_the_image(self):
        out = self.ok("--actor=Firefox/Linux (192.0.2.23)", "build", "base")
        self.assertIn("agentbox-build@base.service", out)
        self.assertEqual(self.systemctl(), [["is-active", "agentbox-build@base.service"],
                                            ["start", "--no-block", "agentbox-build@base.service"]])
        with open(self.path("var/lib/agentbox/builds/request-base.json")) as f:
            self.assertEqual(json.load(f), {"actor": "Firefox/Linux (192.0.2.23)", "reset": []})
        [entry] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "build"]
        self.assertEqual((entry["AGENTBOX_KIND"], entry["AGENTBOX_DESCRIPTION"]), ("build", "Start a Build of base"))
        # the unit then runs the Build with that actor
        self.built()
        self.assertEqual(self.activity()[0]["AGENTBOX_ACTOR"], "Firefox/Linux (192.0.2.23)")

    def test_build_all_and_json(self):
        result = self.json("build", "--all")
        self.assertEqual((result["image"], result["unit"], result["started"]), ("all", "agentbox-build@all.service",
                                                                                True))
        self.assertIn(["start", "--no-block", "agentbox-build@all.service"], self.systemctl())

    def test_a_running_build_is_not_started_again(self):
        self.scenario(systemctl_active=["agentbox-build@base.service"])
        code, out, err = self.run_agentbox("build", "base")
        self.assertEqual(code, 1)
        self.assertIn("a Build of base is running already", err)
        self.assertNotIn("start", [c[0] for c in self.systemctl()])

    def test_invalid_calls_are_refused(self):
        for args in (["build"], ["build", "base", "--all"], ["build", "Base"], ["build", "all"], ["build", "-x"],
                     ["build", "base", "rust"], ["build", "../x"]):
            with self.subTest(args=args):
                code, _, _ = self.run_agentbox(*args)
                self.assertNotEqual(code, 0)
        self.assertNotIn("start", [c[0] for c in self.systemctl()])

    def test_without_the_unit_nothing_starts(self):
        os.remove(self.path("etc/systemd/system/agentbox-build@.service"))
        code, out, err = self.run_agentbox("build", "base")
        self.assertEqual(code, 1)
        self.assertIn("run agentbox regenerate first", err)


class UpdateTest(ImagesTest):
    """An Update (#104): `build <image> --reset <vm>...` starts the Build of the Image; when it
    becomes the current one, its unit resets those VMs onto it. A failed Build resets nothing."""
    ACTOR = "Firefox/Linux (192.0.2.23)"
    REQUEST = "var/lib/agentbox/builds/request-base.json"

    def setUp(self):
        super().setUp()
        self.ok("regenerate")
        self.forget_calls()

    def update(self, *vms):
        args = [a for vm in vms for a in ("--reset", vm)]
        return self.ok(f"--actor={self.ACTOR}", "build", "base", *args)

    def resets(self):
        return [(e["AGENTBOX_VM"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"])
                for e in self.journald.entries() if e.get("AGENTBOX_ACTION") == "reset.update"]

    def disk(self, vm):
        with open(self.path(f"var/lib/agentbox/vms/{vm}-system.qcow2")) as f:
            return os.path.basename(json.load(f)["backing-filename"])

    def state(self, vm):
        with open(os.path.join(self.dir, "domains.json")) as f:
            return json.load(f).get(f"agentbox-{vm}", {}).get("state", "running")

    def holding(self, vm):
        """Hold the VM's lifecycle lock (as a Restart would) until the returned file is closed."""
        lock = self.path(f"var/lib/agentbox/locks/lifecycle-{vm}.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        f = open(lock, "a")
        self.addCleanup(f.close)
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def build_in_background(self):
        """The unit's build-run, while the test changes things under it."""
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        p = subprocess.Popen([AGENTBOX, "build-run", "base"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def wait_for(self, done, what):
        deadline = time.monotonic() + 30
        while not done():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.05)

    def build_ended(self):
        """The Build of the run is recorded as done (then its unit goes on to the Resets)."""
        recs = []
        for n in os.listdir(self.path("var/lib/agentbox/builds")):
            if n.startswith("base-") and n.endswith(".json"):
                with open(self.path(f"var/lib/agentbox/builds/{n}")) as f:
                    recs.append(json.load(f))
        return any(r.get("status") == "ok" and r.get("ended") and r["build"] != OLD for r in recs)

    def test_the_build_then_a_reset_of_the_vms_picked_onto_it(self):
        self.ok("pause", "priv")
        self.forget_calls()
        out = self.update("work", "priv")
        self.assertIn("if it succeeds, then a Reset of work, priv", out)
        with open(self.path(self.REQUEST)) as f:
            self.assertEqual(json.load(f), {"actor": self.ACTOR, "reset": ["work", "priv"]})
        [started] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "build"]
        self.assertEqual(started["AGENTBOX_DESCRIPTION"], "Start a Build of base, then a Reset of work, priv")
        self.assertEqual((started["AGENTBOX_PARAM_IMAGE"], started["AGENTBOX_PARAM_RESET"]), ("base", "work, priv"))
        # nothing is reset before the Build is done
        self.assertEqual([c for c in self.calls() if c["cmd"] in ("shutdown", "destroy", "start")], [])
        out = self.built()
        new = self.newest()["build"]
        self.assertEqual((self.disk("work"), self.disk("priv")), (f"base-{new}.qcow2", f"base-{new}.qcow2"))
        self.assertIn(f"== work: Reset after the Update of base (Build {new})", out)
        # work ran and runs again; priv was paused: its Pause is discarded and it stays off
        self.assertEqual([(c["cmd"], c["dom"]) for c in self.calls() if c["cmd"] in ("shutdown", "start")],
                         [("shutdown", "agentbox-work"), ("start", "agentbox-work")])
        self.assertEqual(self.resets(), [
            ("work", "ok", f"Reset after the Update onto Build base-{new}, started again"),
            ("priv", "ok", f"Reset after the Update onto Build base-{new}")])
        self.assertEqual([{k: v for k, v in e.items() if k.startswith("AGENTBOX_PARAM_")} for e in self.journald.entries()
                          if e["AGENTBOX_ACTION"] == "reset.update"], [
            {"AGENTBOX_PARAM_IMAGE": "base", "AGENTBOX_PARAM_BUILD": new, "AGENTBOX_PARAM_STARTED": "true"},
            {"AGENTBOX_PARAM_IMAGE": "base", "AGENTBOX_PARAM_BUILD": new}])
        entries = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] in ("build.ok", "reset.update")]
        self.assertEqual([e["AGENTBOX_ACTION"] for e in entries], ["build.ok", "reset.update", "reset.update"])
        self.assertEqual({e["AGENTBOX_ACTOR"] for e in entries}, {self.ACTOR})
        self.assertEqual(self.json("images")["images"][0]["builds"][0]["used_by"], ["priv", "work"])

    def test_a_failed_build_resets_nothing(self):
        self.system_disk("work", OLD)
        self.update("work")
        self.scenario(podman_fails={"build": ["localhost/agentbox/base:"]})
        code, out = self.build()
        self.assertEqual(code, 1, out)
        self.assertIn("== No Reset of work: the Build of base failed", out)
        self.assertEqual([c for c in self.calls() if c["cmd"] in ("shutdown", "destroy", "start")], [])
        self.assertFalse([c for c in self.calls("qemu-img") if c["argv"][0] == "create"])
        self.assertEqual(self.disk("work"), f"base-{OLD}.qcow2")
        self.assertEqual(self.resets(), [("work", "failed", "No Reset after the Update: the Build of base "
                                                            "failed")])
        [entry] = [e for e in self.journald.entries() if e["AGENTBOX_ACTION"] == "reset.update"]
        self.assertEqual((entry["AGENTBOX_PARAM_SKIPPED"], entry["AGENTBOX_PARAM_FAILED_BUILDS"]), ("true", "base"))

    def test_a_build_the_check_discards_resets_nothing(self):
        self.system_disk("work", OLD)
        os.remove(os.path.join(self.source, "vm/files/usr/lib/agentbox/hooks/pre-push"))
        self.update("work")
        code, out = self.build()
        self.assertEqual(code, 1, out)
        self.assertEqual(self.newest()["status"], "rejected")
        self.assertEqual(self.disk("work"), f"base-{OLD}.qcow2")
        self.assertEqual([c for c in self.calls() if c["cmd"] in ("shutdown", "destroy", "start")], [])
        self.assertEqual([vm for vm, outcome, _ in self.resets() if outcome == "failed"], ["work"])

    def test_without_the_files_of_the_release_nothing_is_reset_either(self):
        shutil.rmtree(self.source)
        self.update("priv")
        code, out = self.build()
        self.assertEqual(code, 1, out)
        self.assertEqual([vm for vm, _, _ in self.resets()], ["priv"])
        self.assertFalse([c for c in self.calls("qemu-img") if c["argv"][0] == "create"])

    def test_a_vm_that_changed_meanwhile_is_left_alone_and_the_others_reset(self):
        self.update("work", "priv")
        self.ok("set-image", "priv", "base", "--build", OLD)
        code, out = self.build()
        self.assertEqual(code, 1, out)          # the unit says something did not work
        new = self.newest()["build"]
        self.assertEqual(self.newest()["status"], "ok")
        self.assertEqual(self.disk("work"), f"base-{new}.qcow2")
        self.assertEqual(self.resets(), [
            ("work", "ok", f"Reset after the Update onto Build base-{new}, started again"),
            ("priv", "failed", f"Reset after the Update of base failed: priv now uses Build {OLD} of base, "
                               "no Reset; the VM is running")])
        failed = self.journald.entries()[-1]
        self.assertEqual((failed["AGENTBOX_ERROR"], failed["AGENTBOX_ERROR_PARAM_VM"], failed["AGENTBOX_PARAM_RUNNING"]),
                         ("vm_pinned", "priv", "true"))

    def test_a_newer_build_current_by_the_reset_is_the_one_used_and_named(self):
        self.update("work")
        lock = self.holding("work")
        unit = self.build_in_background()
        self.wait_for(self.build_ended, "the Build did not end")
        # meanwhile another Build became the current one
        newer = "29991231-235959"
        self.write(f"{IMAGES}/base-{newer}.qcow2", "{}")
        self.write(f"var/lib/agentbox/builds/base-{newer}.json", json.dumps(
            {"image": "base", "build": newer, "status": "ok", "started": 1, "finished": 2}))
        os.symlink(f"base-{newer}.qcow2", self.path(f"{IMAGES}/.base.qcow2.new"))
        os.replace(self.path(f"{IMAGES}/.base.qcow2.new"), self.path(f"{IMAGES}/base.qcow2"))
        lock.close()
        out, _ = unit.communicate(timeout=60)
        self.assertEqual(unit.returncode, 0, out)
        self.assertEqual(self.disk("work"), f"base-{newer}.qcow2")
        [entry] = [e for e in self.journald.entries() if e.get("AGENTBOX_ACTION") == "reset.update"]
        self.assertEqual((entry["AGENTBOX_DESCRIPTION"], entry["AGENTBOX_PARAM_BUILD"]),
                         (f"Reset after the Update onto Build base-{newer}, started again", newer))

    def test_a_reset_failing_after_the_stop_starts_the_vm_again_on_its_old_disk(self):
        self.system_disk("work", OLD)
        self.update("work")
        lock = self.holding("work")
        unit = self.build_in_background()
        self.wait_for(self.build_ended, "the Build did not end")
        os.remove(self.path(f"{IMAGES}/base.qcow2"))       # no current Build any more
        lock.close()
        out, _ = unit.communicate(timeout=60)
        self.assertEqual(unit.returncode, 1, out)
        self.assertIn("work started again (System disk unchanged)", out)
        self.assertEqual([(c["cmd"], c["dom"]) for c in self.calls() if c["cmd"] in ("shutdown", "start")],
                         [("shutdown", "agentbox-work"), ("start", "agentbox-work")])
        self.assertEqual((self.state("work"), self.disk("work")), ("running", f"base-{OLD}.qcow2"))
        self.assertEqual(self.resets(), [("work", "failed", "Reset after the Update of base failed: no Build "
                                                            "of Image base, run agentbox build base first; "
                                                            "the VM is running")])

    def test_a_vm_whose_start_fails_after_the_new_disk_stays_off_and_says_so(self):
        self.scenario(vms={"work": {"start_fails": True}})
        self.update("work")
        code, out = self.build()
        self.assertEqual(code, 1, out)
        new = self.newest()["build"]
        self.assertEqual(self.disk("work"), f"base-{new}.qcow2")
        [(vm, outcome, description)] = self.resets()
        self.assertEqual((vm, outcome), ("work", "failed"))
        self.assertIn("fresh System disk, but the start fails", description)
        self.assertTrue(description.endswith("; the VM stays off"), description)

    def test_stopped_during_the_resets_the_rest_are_recorded_as_not_done(self):
        self.update("work", "priv")
        lock = self.holding("priv")
        unit = self.build_in_background()
        self.wait_for(lambda: any(vm == "work" for vm, _, _ in self.resets()), "work was not reset")
        time.sleep(0.3)             # the unit waits for priv's lock now
        unit.send_signal(signal.SIGTERM)            # systemctl stop
        out, _ = unit.communicate(timeout=60)
        lock.close()
        self.assertEqual(unit.returncode, 130, out)
        new = self.newest()["build"]
        self.assertEqual(self.resets(), [
            ("work", "ok", f"Reset after the Update onto Build base-{new}, started again"),
            ("priv", "failed", "Reset after the Update of base interrupted, the VM is running")])
        self.assertEqual(self.journald.entries()[-1]["AGENTBOX_ERROR"], "interrupted")
        self.assertEqual(self.state("priv"), "running")

    def test_only_vms_on_this_image_without_a_pin_can_be_picked(self):
        self.image("rust", DERIVED)
        vm_records.write(self.root, record("priv", 2, build=OLD))
        for args, message in ((["base", "--reset", "nope"], "No such VM"),
                              (["base", "--reset", "priv"], f"priv is pinned to Build {OLD}, a Reset would "
                                                            "not bring the new Build"),
                              (["rust", "--reset", "work"], "work uses Image base, not rust"),
                              (["--all", "--reset", "work"], "--reset only with one Image, not with --all"),
                              (["base", "--reset"], "--reset needs a value")):
            with self.subTest(args=args):
                code, out, err = self.run_agentbox("build", *args)
                self.assertNotEqual(code, 0)
                self.assertIn(message, err)
        self.assertNotIn("start", [c[0] for c in self.systemctl()])
        self.assertFalse(self.exists(self.REQUEST))

    def test_json_names_the_vms(self):
        result = self.json("build", "base", "--reset=work", "--reset", "work")
        self.assertEqual((result["image"], result["reset"]), ("base", ["work"]))
        self.assertEqual(self.json("build", "--all")["reset"], [])

    def test_a_request_naming_odd_vms_resets_none_of_them(self):
        os.makedirs(self.path("var/lib/agentbox/builds"), exist_ok=True)
        self.write(self.REQUEST, json.dumps({"actor": "x", "reset": ["../work", "Work", 3]}))
        self.built()
        self.assertEqual(self.resets(), [])


class TimerTest(ImagesTest):
    STAMP = "var/lib/systemd/timers/stamp-agentbox-rebuild.timer"

    def unit(self, name):
        return self.read("etc/systemd/system/" + name)

    def test_regenerate_writes_the_build_unit_and_the_weekly_rebuild(self):
        self.ok("regenerate")
        service = self.unit("agentbox-build@.service")
        self.assertIn("ExecStart=/usr/bin/agentbox build-run %i\n", service)
        self.assertIn("Type=exec\n", service)
        self.assertIn("OnCalendar=Sun *-*-* 03:00\n", self.unit("agentbox-rebuild.timer"))
        self.assertIn("Persistent=true\n", self.unit("agentbox-rebuild.timer"))
        self.assertIn("ExecStart=/usr/bin/agentbox --actor=timer build --all\n",
                      self.unit("agentbox-rebuild.service"))
        self.assertIn(["enable", "--now", "agentbox-rebuild.timer"], self.systemctl())

    def test_a_new_rebuild_day_and_time_apply_at_once_without_catching_up(self):
        self.ok("regenerate")
        os.utime(self.path(self.STAMP), (0, 0))
        self.forget_calls()
        out = self.ok("set-settings", "rebuild_day=fri", "rebuild_time=02:30")
        self.assertNotIn("gilt", out)
        self.assertIn("OnCalendar=Fri *-*-* 02:30\n", self.unit("agentbox-rebuild.timer"))
        calls = self.systemctl()
        stop, reload, start = (calls.index(["stop", "agentbox-rebuild.timer"]), calls.index(["daemon-reload"]),
                               calls.index(["start", "agentbox-rebuild.timer"]))
        self.assertLess(stop, reload)
        self.assertLess(reload, start)
        self.assertNotIn(["restart", "agentbox-rebuild.timer"], calls)
        self.assertGreater(os.path.getmtime(self.path(self.STAMP)), time.time() - 60)
        # the other timers stay as they are
        self.assertNotIn(["stop", "agentbox-weekly.timer"], calls)
        self.assertNotIn(["restart", "agentbox-feeds.timer"], calls)

    def test_an_unchanged_regenerate_leaves_it_alone(self):
        self.ok("regenerate")
        self.forget_calls()
        self.ok("regenerate")
        self.assertEqual([c for c in self.systemctl() if c[0] != "enable"], [])


class LogTest(ImagesTest):
    def journal(self, *entries):
        with open(os.path.join(self.dir, "journal.json"), "w") as f:
            json.dump(list(entries), f)

    def line(self, n, message, at, unit="agentbox-build@base.service"):
        return {"__REALTIME_TIMESTAMP": str(int(at * 10**6)), "__CURSOR": f"c{n}", "_SYSTEMD_UNIT": unit,
                "SYSLOG_IDENTIFIER": "agentbox-build", "PRIORITY": "6", "MESSAGE": message, "_BOOT_ID": "b1"}

    def test_the_log_of_a_build_is_its_units_journal_while_it_ran(self):
        self.built()
        new = self.newest()
        with open(self.path(f"var/lib/agentbox/builds/base-{new['build']}.json")) as f:
            started = json.load(f)["started"]
        self.journal(self.line(1, "before", started - 100), self.line(2, "== base: building the container image", started + .1),
                     self.line(3, "STEP 1/12: FROM quay.io/fedora/fedora-bootc:44", started + .2),
                     self.line(4, "other unit", started + .2, unit="agentbox-build@rust.service"),
                     self.line(5, "token ghs_abcdefghijklmnopqrstuvwxyz0123456789 leaked", started + .3),
                     self.line(6, "the next Build", started + 1000))
        result = self.json("build-log", "base")
        self.assertEqual((result["build"], result["status"]), (new["build"], "ok"))
        self.assertEqual([e["message"] for e in result["entries"]],
                         ["== base: building the container image", "STEP 1/12: FROM quay.io/fedora/fedora-bootc:44",
                          "token [hidden] leaked"])
        self.assertEqual(result["cursor"], "c5")
        argv = self.calls("journalctl")[-1]["argv"]
        self.assertIn("_SYSTEMD_UNIT=agentbox-build@base.service", argv)
        # what came after a cursor, for a live view
        result = self.json("build-log", "base", "--after-cursor", "c2")
        self.assertEqual([e["cursor"] for e in result["entries"]], ["c3", "c5"])

    def test_text_output_and_errors(self):
        self.built()
        self.journal()
        self.assertTrue(self.ok("build-log", "base").startswith(f"Build base-{self.newest()['build']}: ok"))
        for args, message in ((["build-log"], "build-log <image>"), (["build-log", "rust"], "Image rust has no Builds"),
                              (["build-log", "base", "20250101-0000"], "does not exist"),
                              (["build-log", "base", "--lines", "0"], "invalid value for --lines"),
                              (["build-log", "base", "--after-cursor", "a b"], "invalid value")):
            with self.subTest(args=args):
                code, _, err = self.run_agentbox(*args)
                self.assertNotEqual(code, 0)
                self.assertIn(message, err)

    def test_a_build_from_before_has_no_log(self):
        result = self.json("build-log", "base", OLD)
        self.assertEqual((result["status"], result["entries"]), ("migrated", []))
        self.assertEqual(self.calls("journalctl"), [])


class OverviewTest(ImagesTest):
    def test_images_lists_every_image_with_its_builds(self):
        self.built()
        self.image("rust", DERIVED)
        result = self.json("images")
        self.assertEqual((result["builds_kept"], result["release"]), (3, RELEASE))
        base, rust = result["images"]
        self.assertEqual((base["name"], base["base"], base["in_release"], base["running"]), ("base", True, True, None))
        self.assertEqual((rust["name"], rust["in_release"], rust["builds"]), ("rust", True, []))
        self.assertEqual(base["vms"], ["priv", "work"])
        self.assertEqual([b["build"] for b in base["builds"]], [self.newest()["build"], OLD])

    def test_text(self):
        self.built()
        out = self.ok("images")
        self.assertIn("Image base (Base image), current: ", out)
        self.assertIn("from before", out)
        self.assertIn("current", out)

    def test_an_image_the_release_no_longer_defines_keeps_its_builds(self):
        self.built()
        self.image("rust", DERIVED)
        self.built("rust")
        shutil.rmtree(os.path.join(self.source, "vm", "images", "rust"))
        rust = self.overview()["rust"]
        self.assertEqual((rust["in_release"], rust["current"]), (False, rust["builds"][0]["build"]))
        self.assertIn("Image rust, current: ", self.ok("images"))
        self.assertIn("(not in the installed Release)", self.ok("images"))

    def test_without_the_files_of_the_release_it_is_not_known_which_images_it_defines(self):
        shutil.rmtree(self.source)
        self.assertEqual([(i["name"], i["in_release"]) for i in self.json("images")["images"]], [("base", None)])

    def test_reading_is_not_recorded(self):
        self.ok("images")
        self.json("build-log", "base", OLD)
        self.ok("containerfile", "base")
        self.assertEqual(self.journald.entries(), [])


REPOSITORY_URL = "https://github.com/likt0r/askos"


class DefinitionTest(ImagesTest):
    """#67: each Image's Containerfile, linked in the repository at the Release tag or commit its
    current Build came from, and its text from the installed Release, which the next Build takes.
    Links only: nothing is fetched, no token is involved."""

    def current_build(self, image, build, **fields):
        """A Build of the Image, made its current one, with these fields in its record."""
        self.write(f"{IMAGES}/{image}-{build}.qcow2", "")
        self.write(f"var/lib/agentbox/builds/{image}-{build}.json", json.dumps(
            {"image": image, "build": build, "status": "ok", "step": "done", "started": 1793595600,
             "finished": 1793596800, "base_build": None, "unit": f"agentbox-build@{image}.service",
             "actor": "timer", "agents": None, "problems": [], "error": None, "safeguards": None,
             "container": False, **fields}))
        link = self.path(f"{IMAGES}/{image}.qcow2")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(f"{image}-{build}.qcow2", link)

    def installed(self, rel):
        with open(os.path.join(self.source, rel), "rb") as f:
            return f.read()

    def test_the_base_image_links_its_containerfile_at_the_tag_of_the_release_it_came_from(self):
        self.current_build("base", "20261102-030000", release="1.2.3")
        result = self.json("images")
        self.assertEqual((result["repository"], result["repository_url"]), ("likt0r/askos", REPOSITORY_URL))
        base = result["images"][0]
        self.assertEqual((base["containerfile"], base["containerfile_url"]),
                         ("vm/Containerfile", f"{REPOSITORY_URL}/blob/v1.2.3/vm/Containerfile"))
        self.assertIn(f"  Containerfile: vm/Containerfile, as in the current Build: {REPOSITORY_URL}/blob/v1.2.3/"
                      "vm/Containerfile\n", self.ok("images"))

    def test_a_derived_image_links_its_own_containerfile(self):
        self.image("rust", DERIVED)
        self.image("cuda", DERIVED)
        self.current_build("rust", "20261102-040000", release="1.3.0-rc.1", base_build=OLD)
        images = self.overview()
        self.assertEqual((images["rust"]["containerfile"], images["rust"]["containerfile_url"]),
                         ("vm/images/rust/Containerfile",
                          f"{REPOSITORY_URL}/blob/v1.3.0-rc.1/vm/images/rust/Containerfile"))
        # without a Build there is nothing to link it at
        self.assertEqual((images["cuda"]["containerfile"], images["cuda"]["containerfile_url"]),
                         ("vm/images/cuda/Containerfile", None))
        self.assertIn("Image cuda, current: -, VMs: -\n  Containerfile: vm/images/cuda/Containerfile\n",
                      self.ok("images"))

    def test_a_build_from_before_links_the_commit_of_main_in_the_repository_it_names(self):
        self.current_build("base", "20261102-030000", repository="likt0r/askos", commit=COMMIT)
        self.assertEqual(self.overview()["base"]["containerfile_url"],
                         f"{REPOSITORY_URL}/blob/{COMMIT}/vm/Containerfile")
        self.current_build("base", "20261102-040000", repository="someone/askos.fork", commit=COMMIT[:12])
        self.assertEqual(self.overview()["base"]["containerfile_url"],
                         f"https://github.com/someone/askos.fork/blob/{COMMIT[:12]}/vm/Containerfile")

    def test_without_a_known_release_or_commit_there_is_no_link_and_no_error(self):
        # the harness's Build from before Builds were recorded
        self.assertEqual(self.overview()["base"]["containerfile_url"], None)
        self.assertIn("  Containerfile: vm/Containerfile\n", self.ok("images"))
        for n, fields in enumerate((
                {"release": "0.0.0"},                                     # a checkout: no tag
                {"release": "v1.2.3"}, {"release": "1.2"}, {"release": None}, {"release": 123},
                {"release": None, "repository": "likt0r/askos", "commit": "main"},
                {"release": None, "repository": "likt0r/askos", "commit": COMMIT.upper()},
                {"release": None, "commit": COMMIT},                       # no repository named
                {"release": None, "repository": "likt0r/..", "commit": COMMIT},
                {"release": None, "repository": "likt0r askos", "commit": COMMIT},
                {"release": None, "repository": "https://evil.example/x", "commit": COMMIT})):
            with self.subTest(fields=fields):
                self.current_build("base", f"20261102-03{n:02d}00", **fields)
                result = self.json("images")
                self.assertEqual(result["images"][0]["containerfile_url"], None)
                self.assertEqual(result["repository_url"], REPOSITORY_URL)

    def test_the_text_of_the_installed_release_for_the_base_image_and_a_derived_one(self):
        result = self.json("containerfile", "base")
        content = self.installed("vm/Containerfile")
        self.assertEqual(result, {
            "image": "base", "path": "vm/Containerfile", "release": RELEASE,
            "url": f"{REPOSITORY_URL}/blob/v{RELEASE}/vm/Containerfile" if RELEASE != "0.0.0" else None,
            "text": content.decode(), "size": len(content), "truncated": False})
        self.image("rust", DERIVED + "RUN dnf -y install clang\n")
        rust = self.json("containerfile", "rust")
        self.assertEqual((rust["path"], rust["text"], rust["truncated"]),
                         ("vm/images/rust/Containerfile", DERIVED + "RUN dnf -y install clang\n", False))
        self.assertEqual(self.ok("containerfile", "rust"), DERIVED + "RUN dnf -y install clang\n")
        # what the installed Release has now, not what a Build copied before
        self.image("rust", DERIVED + "RUN dnf -y install lld\n")
        self.assertEqual(self.json("containerfile", "rust")["text"], DERIVED + "RUN dnf -y install lld\n")

    def test_the_text_is_capped_at_whole_lines(self):
        line = "RUN echo " + "x" * 90 + "\n"                   # 100 bytes
        self.image("big", DERIVED + line * 1000)
        result = self.json("containerfile", "big")
        size = len(DERIVED) + 100 * 1000
        self.assertEqual((result["size"], result["truncated"]), (size, True))
        self.assertLessEqual(len(result["text"].encode()), 64 * 1024)
        self.assertGreater(len(result["text"].encode()), 64 * 1024 - 100)
        self.assertTrue(result["text"].endswith(line))
        self.assertTrue((DERIVED + line * 1000).startswith(result["text"]))
        code, out, err = self.run_agentbox("containerfile", "big")
        self.assertEqual((code, out), (0, result["text"]))
        self.assertIn(f"truncated, the file has {size} bytes", err)

    def test_the_text_has_no_control_characters_tokens_or_keys(self):
        folder = os.path.join(self.source, "vm", "images", "odd")
        os.makedirs(folder)
        with open(os.path.join(folder, "Containerfile"), "wb") as f:
            f.write(b"FROM x\x1b[31m\r\nENV TOKEN=ghs_abcdefghijklmnopqrstuvwxyz0123456789 KEY=sk-ant-oat01-abcdef\n"
                    b"# -----BEGIN RSA PRIVATE KEY-----\n# \xff\xfe and \xe2\x80\xae reversed\n")
        text = self.json("containerfile", "odd")["text"]
        self.assertEqual(text, "FROM x[31m\nENV TOKEN=[hidden] KEY=[hidden]\n# [hidden]\n"
                               "# �� and  reversed\n")

    def test_odd_or_missing_containerfiles_are_refused(self):
        os.makedirs(os.path.join(self.source, "vm", "images", "linked"))
        os.symlink("/etc/passwd", os.path.join(self.source, "vm", "images", "linked", "Containerfile"))
        os.makedirs(os.path.join(self.source, "vm", "images", "folder", "Containerfile"))
        for args, message in (
                (["containerfile"], "containerfile <image> [--json]"),
                (["containerfile", "base", "rust"], "containerfile <image> [--json]"),
                (["containerfile", "base", "--all"], "unknown option: --all"),
                (["containerfile", "Base"], "invalid Image: Base"),
                (["containerfile", "../vm"], "invalid Image"),
                (["containerfile", "all"], "invalid Image: all"),
                (["containerfile", "cuda"], "no Containerfile for Image cuda in the installed Release "
                                            "(vm/images/cuda/Containerfile is missing)"),
                (["containerfile", "linked", "--json"], "Containerfile not readable"),
                (["containerfile", "folder", "--json"], "Containerfile is not a file")):
            with self.subTest(args=args):
                code, out, err = self.run_agentbox(*args)
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
                self.assertIn(message, err)
        shutil.rmtree(self.source)
        code, out, err = self.run_agentbox("containerfile", "base", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no Containerfile for Image base in the installed Release (vm/Containerfile is missing)", err)


class ContainersTest(ImagesTest):
    """#27, #34, ADR 0014: docker (podman-docker) and docker compose (Docker Compose) reach the
    agent's rootless Podman, and containers and builds reach the outside as the rest of the VM
    does, through its resolver and the Host's proxy, with no settings. Only a real VM can run
    them; this checks what the Base image carries."""

    SOCKET = "unix:///run/user/1000/podman/podman.sock"

    def setUp(self):
        super().setUp()
        self.built()
        self.root = self.rootfs("base")
        self.env = safeguards.systemd_environment(self.root)

    def read(self, rel):
        with open(os.path.join(self.root, rel.lstrip("/"))) as f:
            return f.read()

    def test_docker_reaches_the_agents_podman_from_services_and_shells(self):
        self.assertEqual(self.env["DOCKER_HOST"], self.SOCKET)
        # shells: the agent gets its socket, root and other users podman-docker's (unset here)
        for uid, before, after in (("1000", "", self.SOCKET), ("1000", "unix:///elsewhere", self.SOCKET),
                                   ("0", self.SOCKET, "unset"), ("0", "", "unset"),
                                   ("1001", "unix:///mine", "unix:///mine")):
            with self.subTest(uid=uid, before=before):
                self.assertEqual(self.shell_docker_host(uid, before), after)
        self.assertNotIn("CONTAINER_HOST", self.read("/etc/profile.d/agentbox.sh"))    # podman stays local
        self.assertNotIn("CONTAINER_HOST", self.env)
        containerfile = self.read_repo("vm/Containerfile")
        self.assertIn("systemctl --global enable podman.socket", containerfile)
        # docker is podman (podman-docker)
        self.assertIn("podman-docker", re.search(r"dnf -y install [^&]*", containerfile).group(0).split())
        self.assertTrue(os.path.isfile(os.path.join(self.root, "etc/containers/nodocker")))   # no notice

    def test_containers_and_builds_get_no_proxy(self):
        """Nothing in the VM points a container, a build or a program at a proxy: no proxy
        variable for Podman or its containers, and no Docker config, which would hand one to
        every container Docker Compose starts. Nothing under vm/ (any Image, any file) sets one."""
        conf = tomllib.loads(self.read("/etc/containers/containers.conf.d/50-agentbox.conf"))
        self.assertEqual(list(conf), ["engine"])
        self.assertNotIn("env", conf["engine"])
        self.assertNotIn(".docker", self.read(safeguards.GUEST_SETUP))
        self.assertNotIn("DOCKER_CONFIG", self.env)
        setting = re.compile(r"(?i)\w*_proxy\b|proxy(host|port)|\.docker/config\.json|docker-config\.json|"
                             r"DOCKER_CONFIG")
        for top, dirs, files in os.walk(os.path.join(REPO, "vm")):
            for name in dirs + files:
                path = os.path.join(top, name)
                rel = os.path.relpath(path, REPO)
                self.assertNotEqual(name, "docker-config.json", rel)
                self.assertFalse(rel.endswith(os.path.join(".docker", "config.json")), rel)
                if name in files:
                    with open(path, errors="replace") as f:
                        found = setting.search(f.read())
                    self.assertIsNone(found, f"{rel}: {found and found.group(0)}")

    def test_compose_is_docker_compose(self):
        """podman compose and docker compose (podman-docker) run Docker Compose, which reads
        nested variables podman-compose cannot (#34)."""
        conf = tomllib.loads(self.read("/etc/containers/containers.conf.d/50-agentbox.conf"))
        self.assertEqual(conf["engine"]["compose_providers"], ["/usr/libexec/docker/cli-plugins/docker-compose"])
        self.assertIs(conf["engine"]["compose_warning_logs"], False)
        install = re.search(r"dnf -y install [^&]*", self.read_repo("vm/Containerfile")).group(0).split()
        self.assertIn("docker-compose", install)
        self.assertNotIn("podman-compose", install)

    def test_short_image_names_resolve_to_docker_io(self):
        """#32: Fedora searches registry.fedoraproject.org first, which the proxy blocks, so a
        compose file's traefik:v3.5.4 failed. A drop-in after Fedora's own sets docker.io alone,
        as Docker has it; one registry leaves short-name-mode nothing to prompt for."""
        drop_ins = "etc/containers/registries.conf.d"
        name = "50-agentbox.conf"
        conf = tomllib.loads(self.read(f"/{drop_ins}/{name}"))
        self.assertEqual(conf, {"unqualified-search-registries": ["docker.io"]})   # aliases stay
        # drop-ins load in alpha-numerical order, later ones overwrite: after Fedora's shortnames
        self.assertGreater(name, "000-shortnames.conf")
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, drop_ins)))[-1], name)

    def shell_docker_host(self, uid, docker_host):
        """DOCKER_HOST after a shell of user uid read the profile, with docker_host set before."""
        bin_dir = os.path.join(self.dir, f"id-{uid}")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(bin_dir, "id"), "w") as f:
            f.write(f"#!/bin/sh\necho {uid}\n")
        os.chmod(os.path.join(bin_dir, "id"), 0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
        if docker_host:
            env["DOCKER_HOST"] = docker_host
        profile = os.path.join(self.root, "etc/profile.d/agentbox.sh")
        r = subprocess.run(["sh", "-c", '. "$1"; echo "${DOCKER_HOST-unset}"', "sh", profile],
                           env=env, capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def read_repo(self, rel):
        with open(os.path.join(REPO, rel)) as f:
            return f.read()


if __name__ == "__main__":
    unittest.main()
