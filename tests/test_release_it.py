"""The Release tooling (ADR 0009): release-it turns the commit types since the last tag into the
next semantic version, as `fix`, `feat` and `feat!` promise, and takes the increment the Release
button hands it (.github/workflows/release.yml, tests/test_workflows.py).

Each case runs release-it with --dry-run or --release-version in a scratch Git repository (a
local bare repository as its remote) that uses this repository's .release-it.json, and reads the
version it announces.
Needs the Release tooling (`npm ci` in the repo root); without it the tests are skipped, as the
Host never has Node (ADR 0003).

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_IT = os.path.join(ROOT, "node_modules", "release-it", "bin", "release-it.js")
NODE = shutil.which("node")


@unittest.skipUnless(NODE and os.path.exists(RELEASE_IT), "needs `npm ci` in the repository root")
class NextVersionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.remote = os.path.join(tmp.name, "remote.git")
        self.repo = os.path.join(tmp.name, "agent-box")
        os.mkdir(self.repo)
        self.git("init", "-q", "--bare", self.remote, cwd=tmp.name)
        # the branch is not called main: pushes to it are blocked where these tests are written
        self.git("init", "-q", "-b", "trunk", self.repo)
        self.git("remote", "add", "origin", "file://" + self.remote)
        self.write("VERSION", "0.1.0\n")
        self.write(".gitignore", "node_modules\n")
        self.write("package.json", '{"name": "agent-box", "private": true}\n')
        shutil.copy(os.path.join(ROOT, ".release-it.json"), self.repo)
        os.symlink(os.path.join(ROOT, "node_modules"), os.path.join(self.repo, "node_modules"))
        self.commit("chore: first release")
        self.git("tag", "v0.1.0")
        self.git("push", "-q", "-u", "origin", "trunk", "--tags")

    def git(self, *args, cwd=None):
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
                       cwd=cwd or self.repo, check=True, capture_output=True)

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def commit(self, message):
        self.write(f"change-{len(os.listdir(self.repo))}", message)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def release_it(self, *args):
        r = subprocess.run([NODE, RELEASE_IT, "--ci", "--no-github.release", "--no-git.requireBranch", *args],
                           cwd=self.repo, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def next_version(self, *increment):
        out = self.release_it("--dry-run", *increment)
        found = re.search(r"Let's release \S+ \((\S+)\.\.\.(\S+)\)", out)
        self.assertTrue(found, out)
        self.assertEqual(found.group(1), "0.1.0")
        return found.group(2)

    def test_a_fix_is_the_next_patch(self):
        self.commit("fix: a bug")
        self.assertEqual(self.next_version(), "0.1.1")

    def test_a_feature_is_the_next_minor(self):
        self.commit("feat: something new")
        self.assertEqual(self.next_version(), "0.2.0")

    def test_a_breaking_change_is_the_next_major(self):
        self.commit("feat!: drop the old API")
        self.assertEqual(self.next_version(), "1.0.0")

    def test_the_biggest_change_wins(self):
        for message in ("fix: a bug", "feat: something new", "docs: explain it", "fix(web): another bug"):
            self.commit(message)
        self.assertEqual(self.next_version(), "0.2.0")

    def test_the_button_asks_for_the_next_version_alone(self):
        """With the increment `auto`, the Release button first asks release-it for the version."""
        self.commit("feat: something new")
        self.assertEqual(self.release_it("--release-version"), "0.2.0\n")

    def test_commits_that_raise_no_version_find_none_and_a_patch_is_asked_for(self):
        """release-it finds no next version then; the button starts a PATCH (CONTRIBUTING.md)."""
        for message in ("docs: explain it", "chore: tidy up", "ci: pin the actions", "refactor(web): move it"):
            self.commit(message)
        self.assertEqual(self.release_it("--release-version"), "")
        self.assertEqual(self.next_version("patch"), "0.1.1")

    def test_a_chosen_increment_wins_over_the_commits(self):
        self.commit("fix: a bug")
        self.assertEqual(self.next_version("minor"), "0.2.0")
        self.assertEqual(self.next_version("major"), "1.0.0")
        self.commit("feat!: drop the old API")
        self.assertEqual(self.next_version("patch"), "0.1.1")

    def test_the_version_is_written_to_the_one_file_that_holds_it(self):
        self.commit("fix: a bug")
        self.next_version()
        # a dry run changes nothing, and VERSION is the file the bumper reads and writes
        with open(os.path.join(self.repo, "VERSION")) as f:
            self.assertEqual(f.read(), "0.1.0\n")
        with open(os.path.join(ROOT, ".release-it.json")) as f:
            self.assertIn('"in": "VERSION"', f.read())


if __name__ == "__main__":
    unittest.main()
