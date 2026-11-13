"""The Release a Host runs (ADR 0009): one version in the file VERSION, reported by the CLI
(`agentbox version`, `agentbox --version`) and by the Control Room's GET /api/v1/version.

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root; the real backend runs on
loopback against a fake `agentbox` (see test_control_room_v1.py).

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_control_room_v1 as v1  # noqa: E402
from test_control_room_v1 import V1Test  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(ROOT, "bin", "agentbox")
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?"

# the certificate and password of the backend tests are set up by that module's fixture
setUpModule = v1.setUpModule
tearDownModule = v1.tearDownModule


def version_file():
    with open(os.path.join(ROOT, "VERSION")) as f:
        return f.read().strip()


class CliVersionTest(unittest.TestCase):
    def run_agentbox(self, *args):
        with tempfile.TemporaryDirectory() as root:
            env = {"PATH": os.environ["PATH"], "AGENTBOX_ROOT": root, "LANG": "C.UTF-8"}
            r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, env=env, timeout=30)
        return r.returncode, r.stdout, r.stderr

    def test_the_version_file_holds_a_semantic_version(self):
        self.assertRegex(version_file(), "^" + SEMVER + "$")

    def test_reports_the_release(self):
        self.assertEqual(self.run_agentbox("version"), (0, f"agentbox {version_file()}\n", ""))

    def test_dash_dash_version_is_the_same(self):
        self.assertEqual(self.run_agentbox("--version"), (0, f"agentbox {version_file()}\n", ""))

    def test_json(self):
        code, out, _ = self.run_agentbox("version", "--json")
        self.assertEqual((code, json.loads(out)), (0, {"release": version_file()}))

    def test_refuses_options_it_does_not_know(self):
        self.assertEqual(self.run_agentbox("version", "--bogus")[0], 2)


class RepositoryLinkTest(unittest.TestCase):
    """Where a Release's files are in the repository (#67): its tag v<release>, links only."""

    def setUp(self):
        sys.path.insert(0, ROOT)
        self.addCleanup(sys.path.remove, ROOT)
        from agentbox import version
        self.version = version

    def test_the_repository_is_the_one_the_rpm_names(self):
        with open(os.path.join(ROOT, "rpm", "agentbox.spec")) as f:
            url = re.search(r"(?m)^URL:\s+(\S+)$", f.read()).group(1)
        self.assertEqual(self.version.repository_url(), url)
        self.assertEqual(url, "https://github.com/" + self.version.REPOSITORY)

    def test_a_release_is_tagged_v_and_a_checkout_has_no_tag(self):
        tag = self.version.tag
        self.assertEqual([tag("0.2.1"), tag("1.0.0-rc.1"), tag("0.0.0")], ["v0.2.1", "v1.0.0-rc.1", None])
        for odd in ("v0.2.1", "0.2", "", None, 1, "0.2.1/../x", "0.2.1 "):
            with self.subTest(odd=odd):
                self.assertIsNone(tag(odd))

    def test_file_links_need_a_repository_and_a_ref(self):
        url = self.version.file_url
        self.assertEqual(url("likt0r/askos", "v0.2.1", "vm/Containerfile"),
                         "https://github.com/likt0r/askos/blob/v0.2.1/vm/Containerfile")
        for repository, ref in (("likt0r/askos", None), (None, "v0.2.1"), ("likt0r", "v1"),
                                ("likt0r/..", "v1"), ("a/b/c", "v1"), ("evil.example:443/x", "v1")):
            with self.subTest(repository=repository, ref=ref):
                self.assertIsNone(url(repository, ref, "vm/Containerfile"))


class ReleaseInTheApiTest(V1Test):
    UI = False

    def release(self):
        self.login()
        status, body, _ = self.request("GET", "/api/v1/version")
        self.assertEqual(status, 200)
        return body["release"]

    def test_the_backend_reports_the_release_of_the_repository(self):
        self.assertEqual(self.release(), version_file())


class ReleaseFileTest(V1Test):
    """AGENTBOX_WEB_RELEASE_FILE says where the installed Release's VERSION file is."""
    UI = False
    CONTENT = "3.1.4\n"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "VERSION")
        if cls.CONTENT is not None:
            with open(cls.path, "wb") as f:
                f.write(cls.CONTENT if isinstance(cls.CONTENT, bytes) else cls.CONTENT.encode())
        cls.ENV = {"AGENTBOX_WEB_RELEASE_FILE": cls.path}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def release(self):
        self.login()
        return self.request("GET", "/api/v1/version")[1]["release"]

    def test_reads_that_file(self):
        self.assertEqual(self.release(), "3.1.4")


class MalformedReleaseFileTest(ReleaseFileTest):
    CONTENT = "banana\n"

    def test_reads_that_file(self):
        self.assertIsNone(self.release())


class UndecodableReleaseFileTest(ReleaseFileTest):
    # no UTF-8: no Release, not a failure of the request
    CONTENT = b"\xff\xfe1.0.0\n"

    def test_reads_that_file(self):
        self.assertIsNone(self.release())


class MissingReleaseFileTest(ReleaseFileTest):
    CONTENT = None

    def test_reads_that_file(self):
        self.assertIsNone(self.release())


if __name__ == "__main__":
    unittest.main()
