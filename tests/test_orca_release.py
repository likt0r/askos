"""Orca in the Base image (#113): vm/build/orca-release, which vm/Containerfile runs in the
Build container to pick Orca's GitHub release (the newest at least package_min_age_days old,
or orca_version), download its x86_64 RPM and check it against the SHA-256 GitHub lists for
it, and then to download the Node runtime of Orca's server as the installed Orca pins it.

The script runs as the Containerfile runs it (python3 -I), against a fake curl
(fake_registry_curl.py) that plays GitHub's API, its downloads and nodejs.org; nothing
reaches the network. `agentbox fetch-versions` picks Orca with this script's own functions
(agentbox/versions.py loads it), which ChooseTest checks.

Run from the repo root: python3 -m unittest discover -s tests
"""
import datetime
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_registry import REPO, fake  # noqa: E402

sys.path.insert(0, REPO)
from agentbox import versions  # noqa: E402

SCRIPT = os.path.join(REPO, "vm", "build", "orca-release")
DAY = 86400
API = "https://api.github.com/repos/stablyai/orca/releases"
DOWNLOAD = "https://github.com/stablyai/orca/releases/download"
NODE = "24.22.1"
NODE_ARCHIVE = f"node-v{NODE}-linux-x64.tar.gz"
NODE_URL = f"https://nodejs.org/dist/v{NODE}/{NODE_ARCHIVE}"


def script():
    """vm/build/orca-release as a module (it has no .py suffix)."""
    loader = importlib.machinery.SourceFileLoader("orca_release", SCRIPT)
    spec = importlib.util.spec_from_loader("orca_release", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rpm_content(version):
    return f"the RPM of Orca {version}\n".encode()


def release(version, days, draft=False, prerelease=False, tag=None, rpm=True, digest=None, size=None, state="uploaded",
            asset_days=None, created_days=None):
    """A release as GitHub's API lists it, published `days` ago, with Orca's usual assets;
    its x86_64 RPM (unless rpm=False) has the SHA-256 of rpm_content(version) unless
    `digest` says otherwise (None for none: give digest=False). The asset was created and
    last updated when the release was published, unless asset_days (updated) or created_days say
    how many days ago."""
    now = time.time()
    name = f"orca-ide-{version}.x86_64.rpm"
    content = rpm_content(version)
    assets = [{"name": "latest-linux.yml", "state": "uploaded", "size": 677, "digest": "sha256:" + "e" * 64},
              {"name": f"orca-ide-{version}.aarch64.rpm", "state": "uploaded", "size": 157920669,
               "digest": "sha256:" + "9" * 64}]
    if rpm:
        assets.append({"name": name, "state": state, "size": len(content) if size is None else size,
                       "digest": None if digest is False else digest or "sha256:" + hashlib.sha256(content).hexdigest(),
                       "created_at": stamp(now - (days if created_days is None else created_days) * DAY - 600),
                       "updated_at": stamp(now - (days if asset_days is None else asset_days) * DAY - 600),
                       "browser_download_url": f"{DOWNLOAD}/v{version}/{name}",
                       "content_type": "application/x-redhat-package-manager"})
    tag = tag or f"v{version}"
    return {"tag_name": tag, "name": tag, "draft": draft, "prerelease": prerelease, "immutable": False,
            "created_at": stamp(now - days * DAY - 1800), "published_at": stamp(now - days * DAY), "assets": assets}


# Orca's releases on GitHub, newest first: 1.4.253 is a day old, so a Build with the
# default package_min_age_days (2) takes 1.4.251, skipping a draft and two prereleases
RELEASES = [
    release("1.4.254", 0.1, draft=True),
    release("1.4.253", 1),
    release("0.0.64", 1.2, prerelease=True, tag="mobile-android-v0.0.64"),
    release("1.4.252", 1.5),
    release("1.4.252-rc.0", 2.5, prerelease=True),
    release("1.4.251", 3),
    release("1.4.250", 5),
    release("1.4.249", 10),
]


def pin_file(archive_sha, executable_sha, size, source="official", archive=NODE_ARCHIVE, version=NODE, extra=""):
    """resources/app.asar.unpacked/out/shared/node-runtime-pin.js as Orca 1.4 ships it (tsc's output)."""
    return f"""\"use strict";
/** Pinned external Node runtime (docs/reference/node-runtime-design.html D1). */
Object.defineProperty(exports, "__esModule", {{ value: true }});
exports.SERVER_TARGETS = [
    'darwin-arm64',
    'linux-x64-glibc',
    'linux-x64-musl'
];
exports.COMPAT_SERVER_TARGET_BASES = {{
    'linux-x64-glibc217': 'linux-x64-glibc'
}};
// @generated-begin by config/scripts/update-node-runtime-pin.mjs
exports.NODE_RUNTIME_PIN = {{
    version: '{version}',
    electron: '43.8.1',
    napi: 10,
    headers: {{
        file: 'node-v{version}-headers.tar.gz',
        sha256: '322e0a27db9088093ab8347bdc8c7e0aaa21dc9ed548390bf06b394494c09267'
    }}
}};
exports.NODE_RUNTIME_ASSETS = {{
    'linux-arm64-glibc': {{
        source: 'official',
        archive: 'node-v{version}-linux-arm64.tar.gz',
        archiveSha256: '33c37be234ba8bee041961047b19160f4d7d0f164e8e246f78bdd10feff148b3',
        executableSha256: 'c8c5721679d8d1a09e9987e345d5f9a64899bb85f7a384783ba8a93e6e62348a',
        executableSize: 123377224
    }},
    'linux-x64-glibc': {{
        source: '{source}',
        archive: '{archive}',
        archiveSha256: '{archive_sha}',
        executableSha256: '{executable_sha}',
        executableSize: {size}
    }},{extra}
    'linux-x64-musl': {{
        source: 'official',
        archive: 'node-v{version}-linux-x64-musl.tar.gz',
        archiveSha256: '9b250d29d6ab8ae38dd23019e2c04a6d6b2916ab1dad721c5f6e1da2b2901c6a',
        executableSha256: '569128f3066ac7be59b10b9a0adef708def642ee37cbf9d560a9bdb68f872108',
        executableSize: 132485920
    }}
}};
exports.NODE_RUNTIME_COMPAT_ASSETS = {{
    'linux-x64-glibc217': {{
        source: 'unofficial',
        archive: 'node-v{version}-linux-x64-glibc-217.tar.gz',
        archiveSha256: '422ddeec2c08902a34ef80ae25de388c7b7e2920763030a5262d56b10675c570',
        executableSha256: '6ecc984d4bd0ff3012e91efa39eaf5f7e519037d5b1cbab8a6e1bb5c7914e18a',
        executableSize: 139795880
    }}
}};
// @generated-end
"""


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        fake(os.path.join(self.dir, "bin", "curl"), "fake_registry_curl.py")
        self.files = os.path.join(self.dir, "files")
        os.makedirs(self.files)
        self.out = os.path.join(self.dir, "out")
        self.known = {"github": {}, "downloads": {}}
        self.serve(RELEASES)

    def serve(self, releases, **downloads):
        """GitHub's list of Orca's releases, and the RPM of each (or what `downloads` says)."""
        self.known["github"]["stablyai/orca"] = releases
        for r in releases if isinstance(releases, list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("tag_name"), str):
                continue
            version = r["tag_name"].removeprefix("v")
            name = f"orca-ide-{version}.x86_64.rpm"
            path = os.path.join(self.files, name)
            with open(path, "wb") as f:
                f.write(rpm_content(version))
            self.known["downloads"][f"{DOWNLOAD}/{r['tag_name']}/{name}"] = path
        self.known["downloads"].update(downloads)
        self.save()

    def save(self):
        with open(os.path.join(self.dir, "registry.json"), "w") as f:
            json.dump(self.known, f)

    def file(self, name, content):
        path = os.path.join(self.files, name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def run_script(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_DIR": self.dir,
               "LANG": "C.UTF-8"}
        return subprocess.run([sys.executable, "-I", SCRIPT, *args], env=env, capture_output=True, text=True,
                              timeout=60)

    def rpm(self, *args, days="2", version=""):
        return self.run_script("rpm", self.out, "--min-age-days", days, "--version", version, *args)

    def failed(self, r, message):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(message, r.stderr)
        self.assertTrue(r.stderr.startswith("orca-release: "), r.stderr)
        self.assertEqual(r.stderr.count("\n"), 1, r.stderr)

    def calls(self):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                return [json.loads(line)["argv"] for line in f]
        except FileNotFoundError:
            return []

    def urls(self):
        return [argv[-1] for argv in self.calls()]

    def downloaded(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []


class RpmTest(ScriptTest):
    def test_the_newest_release_at_least_the_minimum_age_old(self):
        r = self.rpm()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.251.x86_64.rpm"])
        with open(os.path.join(self.out, "orca-ide-1.4.251.x86_64.rpm"), "rb") as f:
            self.assertEqual(f.read(), rpm_content("1.4.251"))
        self.assertIn("Orca 1.4.251: the newest release at least 2 days old", r.stdout)
        self.assertIn("SHA-256 as given by GitHub", r.stdout)
        self.assertEqual(self.urls(), [f"{API}?per_page=100&page=1", f"{DOWNLOAD}/v1.4.251/orca-ide-1.4.251.x86_64.rpm"])

    def test_the_minimum_age_counts(self):
        for days, version in (("1", "1.4.253"), ("4", "1.4.250"), ("7", "1.4.249")):
            with self.subTest(days=days):
                r = self.rpm(days=days)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"orca-ide-{version}.x86_64.rpm", self.downloaded())

    def test_curl_takes_only_https_with_time_limits_and_a_size_cap(self):
        self.assertEqual(self.rpm().returncode, 0)
        for argv in self.calls():
            with self.subTest(url=argv[-1]):
                self.assertEqual(argv[0], "-q")                  # no ~/.curlrc
                for option, value in (("--proto", "=https"), ("--proto-redir", "=https")):
                    self.assertEqual(argv[argv.index(option) + 1], value)
                for option in ("--connect-timeout", "-m", "--max-filesize"):
                    self.assertTrue(argv[argv.index(option) + 1].isdigit(), option)
        download = self.calls()[-1]
        self.assertIn("-f", download)
        self.assertEqual(int(download[download.index("--max-filesize") + 1]), 1 << 30)

    def test_the_highest_version_wins_over_a_later_publication(self):
        # a fix for an older line, published after 1.4.251
        self.serve([release("1.4.253", 1), release("1.3.9", 2.5), release("1.4.251", 3)])
        self.assertEqual(self.rpm().returncode, 0)
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.251.x86_64.rpm"])

    def test_drafts_prereleases_and_other_tags_never_count(self):
        self.serve([release("1.4.260", 9, draft=True), release("1.4.259", 9, prerelease=True),
                    release("0.0.70", 9, tag="mobile-android-v0.0.70"), release("1.4.258-rc.1", 9, prerelease=True),
                    release("1.4.257", 9, tag="1.4.257"), release("1.4.250", 9)])
        r = self.rpm()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.250.x86_64.rpm"])

    def test_the_url_comes_from_the_version_never_from_the_answer(self):
        odd = release("1.4.251", 3)
        for asset in odd["assets"]:
            asset["browser_download_url"] = "http://evil.example/orca.rpm"
        self.serve([odd])
        self.assertEqual(self.rpm().returncode, 0)
        self.assertEqual(self.urls()[-1], f"{DOWNLOAD}/v1.4.251/orca-ide-1.4.251.x86_64.rpm")

    def test_the_list_is_paged_until_a_release_is_old_enough(self):
        young = [release(f"1.5.{n}", 0.5) for n in range(150, 50, -1)]
        self.serve([*young, release("1.4.251", 3)])
        r = self.rpm()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.251.x86_64.rpm"])
        self.assertEqual(self.urls()[:2], [f"{API}?per_page=100&page=1", f"{API}?per_page=100&page=2"])

    def test_the_highest_version_counts_even_on_a_later_page(self):
        old = [release(f"1.3.{n}", 9) for n in range(200, 100, -1)]
        self.serve([*old, release("1.4.251", 3)])
        r = self.rpm()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.251.x86_64.rpm"])
        self.assertEqual(self.urls()[:2], [f"{API}?per_page=100&page=1", f"{API}?per_page=100&page=2"])

    def test_a_replaced_asset_of_an_old_release_is_skipped(self):
        for kwargs in ({"asset_days": 0.5}, {"created_days": 0.5}):
            with self.subTest(**kwargs):
                self.serve([release("1.4.252", 5, **kwargs), release("1.4.251", 6)])
                r = self.rpm()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("orca-ide-1.4.251.x86_64.rpm", self.downloaded())
                self.assertNotIn("orca-ide-1.4.252.x86_64.rpm", self.downloaded())
        self.serve([release("1.4.252", 5, asset_days=0.5)])
        self.failed(self.rpm(), "and whose RPM is not younger")

    def test_an_asset_without_readable_times_is_too_young(self):
        odd = release("1.4.252", 5)
        for asset in odd["assets"]:
            asset["updated_at"] = "2020-01-01T00:00:00"          # no time zone
        self.serve([odd, release("1.4.251", 6)])
        self.assertEqual(self.rpm().returncode, 0)
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.251.x86_64.rpm"])

    def test_a_pin_fails_for_an_asset_replaced_after_the_release(self):
        self.serve([release("1.4.252", 5, asset_days=0.5), release("1.4.253", 1)])
        self.failed(self.rpm(version="1.4.252"), "was created or replaced after the release was published")
        self.assertEqual(self.rpm(version="1.4.253").returncode, 0)       # young release, original asset
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.253.x86_64.rpm"])

    def test_a_download_without_a_length_is_cut_off_at_the_size(self):
        self.serve([release("1.4.251", 3)], **{f"{DOWNLOAD}/v1.4.251/orca-ide-1.4.251.x86_64.rpm": {"flood": True}})
        self.failed(self.rpm(), "more than ")
        self.assertEqual(self.downloaded(), [])

    def test_a_download_without_a_length_is_cut_off_at_the_cap(self):
        build = script()
        build.MAX_NODE_ARCHIVE = 3 << 20
        self.known["downloads"][NODE_URL] = {"flood": True}
        self.save()
        path = os.path.join(self.dir, "x")
        env = mock.patch.dict(os.environ, {"FAKE_DIR": self.dir,
                                           "PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)
        with self.assertRaises(build.Failed) as e:
            build.download(NODE_URL, path, build.MAX_NODE_ARCHIVE)
        self.assertIn(f"more than {3 << 20} bytes", str(e.exception))
        self.assertFalse(os.path.exists(path))

    def test_none_old_enough_fails(self):
        self.serve([release("1.4.253", 1), release("1.4.252", 1.5)])
        self.failed(self.rpm(), "no release of Orca that is at least 2 days old")
        self.assertEqual(self.downloaded(), [])

    def test_a_pinned_version_regardless_of_its_age(self):
        r = self.rpm(version="1.4.253")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.downloaded(), ["orca-ide-1.4.253.x86_64.rpm"])
        self.assertIn("Orca 1.4.253: pinned (orca_version)", r.stdout)
        self.assertEqual(self.urls()[0], f"{API}/tags/v1.4.253")
        # an older one too
        self.assertEqual(self.rpm(version="1.4.249").returncode, 0)
        self.assertIn("orca-ide-1.4.249.x86_64.rpm", self.downloaded())

    def test_a_pin_that_is_no_published_release_fails(self):
        for version, message in (("1.4.999", "no release v1.4.999 on GitHub"),
                                 ("1.4.254", "no release v1.4.254 on GitHub"),        # a draft
                                 ("0.0.64", "no release v0.0.64 on GitHub")):        # another tag
            with self.subTest(version=version):
                self.failed(self.rpm(version=version), message)
        self.serve([release("1.4.255", 9, prerelease=True)])
        self.failed(self.rpm(version="1.4.255"), "v1.4.255 is a draft or a prerelease")
        self.assertEqual(self.downloaded(), [])

    def test_a_digest_mismatch_fails_and_leaves_no_file(self):
        self.serve([release("1.4.251", 3, digest="sha256:" + "0" * 64)])
        self.failed(self.rpm(), "instead of " + "0" * 64 + " as given by GitHub")
        self.assertEqual(self.downloaded(), [])

    def test_a_tampered_download_fails(self):
        path = self.file("tampered.rpm", b"the RPM of Orca 1.4.25X\n")        # same size, other bytes
        self.serve([release("1.4.251", 3)], **{f"{DOWNLOAD}/v1.4.251/orca-ide-1.4.251.x86_64.rpm": path})
        self.failed(self.rpm(), "as given by GitHub")
        self.assertEqual(self.downloaded(), [])

    def test_a_download_of_another_size_fails(self):
        path = self.file("longer.rpm", rpm_content("1.4.251") + b"more")
        self.serve([release("1.4.251", 3)], **{f"{DOWNLOAD}/v1.4.251/orca-ide-1.4.251.x86_64.rpm": path})
        self.failed(self.rpm(), f"more than {len(rpm_content('1.4.251'))} bytes delivered")
        self.assertEqual(self.downloaded(), [])

    def test_a_release_without_the_x86_64_rpm_fails(self):
        self.serve([release("1.4.251", 3, rpm=False), release("1.4.250", 5)])
        self.failed(self.rpm(), "release v1.4.251 has no orca-ide-1.4.251.x86_64.rpm")
        self.serve([release("1.4.251", 3, state="starter")])
        self.failed(self.rpm(), "has no orca-ide-1.4.251.x86_64.rpm")

    def test_an_rpm_without_a_digest_fails(self):
        for digest in (False, "md5:" + "0" * 32, "sha256:" + "0" * 63, "sha256:" + "G" * 64):
            with self.subTest(digest=digest):
                self.serve([release("1.4.251", 3, digest=digest)])
                self.failed(self.rpm(), "GitHub names no SHA-256 for orca-ide-1.4.251.x86_64.rpm")
        self.assertEqual(self.downloaded(), [])

    def test_an_rpm_too_large_fails_before_the_download(self):
        self.serve([release("1.4.251", 3, size=(1 << 30) + 1)])
        self.failed(self.rpm(), "unexpected size")
        self.assertEqual(len(self.calls()), 1)

    def test_a_missing_download_fails(self):
        self.serve([release("1.4.251", 3)], **{f"{DOWNLOAD}/v1.4.251/orca-ide-1.4.251.x86_64.rpm": {"status": 404}})
        self.failed(self.rpm(), "download failed (curl exit 22")
        self.assertEqual(self.downloaded(), [])

    def test_github_unreachable_or_failing_fails(self):
        self.known["exit"] = 6
        self.save()
        self.failed(self.rpm(), "GitHub not reachable")
        del self.known["exit"]
        self.serve({"status": 403})
        self.failed(self.rpm(), "GitHub responds with 403")
        self.failed(self.rpm(version="1.4.250"), "GitHub responds with 403")

    def test_odd_answers_fail(self):
        for answer in ({"status": 500}, [None, "x", 1, {"tag_name": ["v1.4.251"]}, {"draft": False}]):
            with self.subTest(answer=answer):
                self.serve(answer)
                r = self.rpm()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(self.downloaded(), [])

    def test_its_arguments_are_checked(self):
        for args in ([], ["rpm"], ["rpm", self.out], ["rpm", self.out, "--min-age-days", "x"],
                     ["rpm", self.out, "--min-age-days", "2", "--min-age-days", "3"], ["rpm", "a", "b", "--min-age-days", "2"],
                     ["runtime", "a"], ["nonsense"]):
            with self.subTest(args=args):
                self.failed(self.run_script(*args), "orca-release rpm DEST --min-age-days N")
        for version in ("1.4", "v1.4.250", "1.4.250-rc.0", "01.4.250", "1.4.250;true", "../x"):
            with self.subTest(version=version):
                self.failed(self.rpm(version=version), "invalid version of Orca")
        self.assertEqual(self.calls(), [])


class RuntimeTest(ScriptTest):
    """The Node runtime of Orca's server, as the installed Orca pins it."""

    def setUp(self):
        super().setUp()
        self.node = b"\x7fELF the pinned node\n"
        self.archive = self.tarball({f"node-v{NODE}-linux-x64/bin/node": self.node,
                                     f"node-v{NODE}-linux-x64/README.md": b"Node.js\n"})
        self.orca = os.path.join(self.dir, "orca")
        self.pin()
        self.dest = os.path.join(self.dir, "usr/lib/agentbox/orca-runtimes")

    def tarball(self, members, name=NODE_ARCHIVE):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            for path, content in members.items():
                info = tarfile.TarInfo(path)
                info.size, info.mode = len(content), 0o755
                tar.addfile(info, io.BytesIO(content))
        path = self.file(name, data.getvalue())
        self.known["downloads"][NODE_URL] = path
        self.save()
        return path

    def sha(self, data):
        return hashlib.sha256(data).hexdigest()

    def pin(self, archive_sha=None, executable_sha=None, size=None, marker=None, **kwargs):
        with open(self.archive, "rb") as f:
            archive_sha = archive_sha or self.sha(f.read())
        executable_sha = executable_sha or self.sha(self.node)
        shared = os.path.join(self.orca, "resources/app.asar.unpacked/out/shared")
        target = os.path.join(self.orca, "resources/orcad-template/targets/linux-x64-glibc")
        os.makedirs(shared, exist_ok=True)
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(shared, "node-runtime-pin.js"), "w") as f:
            f.write(pin_file(archive_sha, executable_sha, len(self.node) if size is None else size, **kwargs))
        with open(os.path.join(target, ".runtime-node"), "w") as f:
            f.write((marker or executable_sha) + "\n")

    def runtime(self):
        return self.run_script("runtime", self.orca, self.dest)

    def installed(self):
        found = []
        for top, dirs, files in os.walk(self.dest) if os.path.isdir(self.dest) else []:
            found += [os.path.relpath(os.path.join(top, n), self.dest) for n in files]
        return sorted(found)

    def test_the_runtime_orca_pins_lands_where_guest_setup_takes_it(self):
        r = self.runtime()
        self.assertEqual(r.returncode, 0, r.stderr)
        node = f"node-{self.sha(self.node)}/bin/node"
        self.assertEqual(self.installed(), [node])
        with open(os.path.join(self.dest, node), "rb") as f:
            self.assertEqual(f.read(), self.node)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dest, node)).st_mode), 0o755)
        self.assertEqual(self.urls(), [NODE_URL])
        argv = self.calls()[0]
        for option, value in (("--proto", "=https"), ("--proto-redir", "=https"), ("--max-filesize", str(200 << 20))):
            self.assertEqual(argv[argv.index(option) + 1], value)
        self.assertIn(f"Node {NODE} for Orca's server: {NODE_URL}", r.stdout)
        # what guest-setup copies into the agent's home is this layout
        with open(os.path.join(REPO, "vm/files/usr/lib/agentbox/guest-setup")) as f:
            self.assertIn("for r in /usr/lib/agentbox/orca-runtimes/node-*; do", f.read())

    def test_an_archive_other_than_pinned_fails(self):
        self.pin(archive_sha="0" * 64)
        self.failed(self.runtime(), f"{NODE_ARCHIVE}: SHA-256")
        self.assertEqual(self.installed(), [])

    def test_a_node_other_than_pinned_fails(self):
        self.pin(executable_sha="1" * 64)
        self.failed(self.runtime(), "bin/node: SHA-256")
        self.assertEqual(self.installed(), [])

    def test_the_template_must_name_the_same_node(self):
        self.pin(marker="2" * 64)
        self.failed(self.runtime(), ".runtime-node names")
        self.assertEqual(self.calls(), [])

    def test_a_size_other_than_pinned_fails(self):
        self.pin(size=len(self.node) + 1)
        self.failed(self.runtime(), "is not a file of")

    def test_a_pin_it_cannot_read_unambiguously_fails(self):
        for kwargs in ({"source": "unofficial"}, {"archive": "node-v24.22.1-linux-x64.tar.xz"},
                       {"archive": f"node-v{NODE}-linux-x64.tar.gz/../../x"}, {"version": "24.22"},
                       {"extra": pin_file("3" * 64, "4" * 64, 1).split("exports.NODE_RUNTIME_ASSETS = {")[1]
                                 .split("    'linux-x64-musl'")[0].rstrip(",")}):
            with self.subTest(kwargs=kwargs):
                self.pin(**kwargs)
                r = self.runtime()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("node-runtime-pin.js", r.stderr)
        os.remove(os.path.join(self.orca, "resources/app.asar.unpacked/out/shared/node-runtime-pin.js"))
        self.failed(self.runtime(), "node-runtime-pin.js")
        self.assertEqual(self.calls(), [])

    def test_an_archive_without_node_fails(self):
        self.archive = self.tarball({f"node-v{NODE}-linux-x64/README.md": b"no node\n"})
        self.pin()
        self.failed(self.runtime(), f"node-v{NODE}-linux-x64/bin/node is missing")

    def test_a_missing_download_fails(self):
        self.known["downloads"][NODE_URL] = {"status": 404}
        self.save()
        self.failed(self.runtime(), "download failed")


class ChooseTest(unittest.TestCase):
    """agentbox fetch-versions says which Orca the next Build takes: with this script's own
    functions, loaded from the installed Release (versions.orca_rules)."""

    def setUp(self):
        versions._orca = None
        self.addCleanup(setattr, versions, "_orca", None)

    def test_the_fetch_loads_the_script_of_the_installed_release(self):
        with mock.patch.object(versions.paths, "VM_SOURCE", REPO):
            rules = versions.orca_rules()
        self.assertEqual(rules.__file__, SCRIPT)
        self.assertIs(versions.orca_rules(), rules)         # once
        cutoff = time.time() - 2 * DAY
        self.assertEqual(rules.version_of(rules.choose(RELEASES, cutoff)), "1.4.251")
        self.assertEqual(rules.version_of(rules.choose(RELEASES, cutoff, "1.4.253")), "1.4.253")

    def test_loading_it_runs_nothing(self):
        with mock.patch.object(versions.paths, "VM_SOURCE", REPO), mock.patch.object(sys, "argv", ["x", "rpm"]), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("ran")):
            versions.orca_rules()

    def test_without_the_script_the_fetch_fails(self):
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(versions.paths, "VM_SOURCE", empty):
            with self.assertRaises(versions.Fail) as caught:
                versions.orca_newest(time.time())
        self.assertEqual((caught.exception.error, caught.exception.exit), ("action_failed", 127))
        self.assertIn("orca-release is missing", caught.exception.message)

    def test_a_script_that_cannot_be_loaded_fails_the_fetch(self):
        for content, why in (("def broken(:\n", "SyntaxError"), ("import no_such_module\n", "ModuleNotFoundError"),
                             ("raise RuntimeError\n", "RuntimeError"),
                             ('REPO = "x"\n', "it lacks PAGES, PER_PAGE, Failed, choose, asset_ok, version_of, "
                                             "rpm_asset")):
            versions._orca = None
            with self.subTest(why=why), tempfile.TemporaryDirectory() as source:
                os.makedirs(os.path.join(source, "vm", "build"))
                with open(os.path.join(source, "vm", "build", "orca-release"), "w") as f:
                    f.write(content)
                with mock.patch.object(versions.paths, "VM_SOURCE", source), \
                        self.assertRaises(versions.Fail) as caught:
                    versions.orca_newest(time.time())
                self.assertEqual(caught.exception.error, "action_failed")
                self.assertIn(f"cannot be loaded ({why})", caught.exception.message)

    def test_without_the_script_the_fetch_asks_nobody(self):
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(versions.paths, "VM_SOURCE", empty), \
                mock.patch.object(versions, "_min_age_days", return_value=2), \
                mock.patch.object(versions, "_orca_pin", return_value=None), \
                mock.patch.object(versions, "_get", side_effect=AssertionError("asked")):
            with self.assertRaises(versions.Fail):
                versions.fetch(lambda line: None)

    def test_both_take_the_same_versions(self):
        # orca_version (settings) and what the fetch found are checked with the settings'
        # pattern; the script decides with its own
        with mock.patch.object(versions.paths, "VM_SOURCE", REPO):
            self.assertEqual(versions.orca_rules().VERSION.pattern, versions.settings.ORCA_VERSION.pattern)


if __name__ == "__main__":
    unittest.main()
