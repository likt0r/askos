"""The GitHub Actions workflows (.github/workflows) and the Release button (docs/releasing.md).

WorkflowsTest checks what every workflow promises without running it: each action pinned to
a commit, inputs reaching a script only through environment variables, write access only in
the jobs that need it, and Dependabot keeping the pins current with titles the title check
accepts. ReleaseWorkflowTest checks the Release workflow (release.yml): only the button starts
it, the RPM comes from the tag, and CI never publishes. The scripts of its steps run here with
bash, with fake `release-it` and `gh` programs, in a temporary directory each.

The workflows are read line by line, without a YAML library (ADR 0006): a key's block is every
line after it that is indented deeper.

Run from the repo root: python3 -m unittest discover -s tests
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(REPO, ".github", "workflows")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^ # v\d+\.\d+\.\d+$")


def read(*parts):
    with open(os.path.join(REPO, *parts)) as f:
        return f.read()


def workflows():
    return {name: read(".github", "workflows", name).splitlines()
            for name in sorted(os.listdir(WORKFLOWS)) if name.endswith((".yml", ".yaml"))}


def indent(line):
    return len(line) - len(line.lstrip(" "))


def ends_block(line, depth):
    return line.strip() and not line.lstrip().startswith("#") and indent(line) <= depth


def block(lines, key, depth=0):
    """The value after `key:` at that indentation and the lines of its block."""
    for i, line in enumerate(lines):
        found = re.match(rf"^ {{{depth}}}{re.escape(key)}:(.*)$", line)
        if found:
            body = []
            for line in lines[i + 1:]:
                if ends_block(line, depth):
                    break
                body.append(line)
            return found.group(1).strip(), body
    return None, []


def jobs(lines):
    _, body = block(lines, "jobs")
    names = [found.group(1) for line in body if (found := re.match(r"^  ([\w-]+):\s*$", line))]
    return {name: block(body, name, 2)[1] for name in names}


def steps(job):
    """The steps of a job, each as its lines."""
    _, body = block(job, "steps", 4)
    items = []
    for line in body:
        if re.match(r"^ {6}- ", line):
            items.append([])
        if items:
            items[-1].append(line)
    return items


def scripts(lines):
    """Every `run:` script in these lines, as the shell gets it."""
    for i, line in enumerate(lines):
        found = re.match(r"^(\s*(?:-\s+)?)run:\s*(.*)$", line)
        if not found:
            continue
        if found.group(2)[:1] in ("|", ">"):
            body = []
            for line in lines[i + 1:]:
                if ends_block(line, len(found.group(1))):
                    break
                body.append(line)
            yield textwrap.dedent("\n".join(body)) + "\n"
        else:
            yield found.group(2)


def writes(job):
    value, body = block(job, "permissions", 4)
    return [line.strip() for line in body if line.strip().endswith(": write")] + \
        (["write-all"] if value == "write-all" else [])


def uses(step):
    found = [re.match(r"^\s*(?:-\s+)?uses:\s*(\S+)", line) for line in step]
    return next((f.group(1) for f in found if f), "")


def make_executable(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class WorkflowsTest(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit_with_its_version_as_a_comment(self):
        """A tag can be moved to other code, a commit cannot; Dependabot reads the comment."""
        found = 0
        for name, lines in workflows().items():
            for line in lines:
                pin = re.match(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", line)
                if pin:
                    found += 1
                    self.assertRegex(pin.group(1), PINNED, f"{name}: {line.strip()}")
                    self.assertRegex(pin.group(2), VERSION_COMMENT, f"{name}: {line.strip()}")
        self.assertGreater(found, 0)

    def test_inputs_reach_a_script_only_through_its_environment(self):
        """An expression inside a script is pasted into its text before bash reads it: a title,
        a branch name or an input could run as code."""
        for name, lines in workflows().items():
            for script in scripts(lines):
                self.assertNotIn("${{", script, name)

    def test_nothing_may_write_unless_a_job_says_so(self):
        for name, lines in workflows().items():
            value, body = block(lines, "permissions")
            self.assertIsNotNone(value, f"{name} sets no permissions")
            if value != "{}":
                self.assertEqual(value, "", name)
                for line in body:
                    if line.strip() and not line.lstrip().startswith("#"):
                        self.assertRegex(line, r"^\s+[\w-]+: (read|none)\s*$", name)
        writing = {(name, job): writes(lines) for name, wf in workflows().items()
                   for job, lines in jobs(wf).items() if writes(lines)}
        # release-it pushes the release commit and the tag and creates the draft release; attach
        # uploads the RPMs to it. The rpm job, which installs the UI's npm packages, only reads.
        self.assertEqual(writing, {("release.yml", "release"): ["contents: write"],
                                   ("release.yml", "attach"): ["contents: write"]})

    def test_no_install_script_runs_where_the_token_can_write(self):
        for name, wf in workflows().items():
            for job, lines in jobs(wf).items():
                if not writes(lines):
                    continue
                for script in scripts(lines):
                    for install in re.findall(r"\bnpm (?:ci|install|i)\b[^\n;&|]*", script):
                        self.assertIn("--ignore-scripts", install, f"{name}: {job}")

    def test_a_checkout_keeps_its_credentials_only_where_release_it_pushes(self):
        keeping = set()
        for name, wf in workflows().items():
            for job, lines in jobs(wf).items():
                for step in steps(lines):
                    if uses(step).startswith("actions/checkout@") and \
                            not any(line.strip() == "persist-credentials: false" for line in step):
                        keeping.add((name, job))
        self.assertEqual(keeping, {("release.yml", "release")})

    def test_dependabot_keeps_the_actions_current(self):
        lines = read(".github", "dependabot.yml").splitlines()
        _, updates = block(lines, "updates")
        text = "\n".join(updates)
        self.assertIn("- package-ecosystem: github-actions", text)
        self.assertRegex(text, r"(?m)^\s+directory: /$")
        self.assertRegex(text, r"(?m)^\s+interval: weekly$")
        prefix = re.search(r"(?m)^\s+prefix: (\S+)$", text).group(1)
        self.assertEqual(prefix, "ci")

    def test_dependabot_titles_pass_the_title_check(self):
        """pr-title.yml refuses titles that are no Conventional Commit; Dependabot's must pass."""
        check = next(scripts(jobs(workflows()["pr-title.yml"])["pr-title"]))
        pattern = re.search(r"^pattern='(.*)'$", check, re.MULTILINE).group(1)
        prefix = re.search(r"(?m)^\s+prefix: (\S+)$", read(".github", "dependabot.yml")).group(1)
        for title in ("bump actions/checkout from 7.0.1 to 7.0.2",
                      "bump the actions group with 3 updates"):
            # Dependabot puts its commit message prefix and a colon before its own title
            title = f"{prefix}: {title}"
            r = subprocess.run(["grep", "-Eq", pattern], input=title, text=True)
            self.assertEqual(r.returncode, 0, title)
        r = subprocess.run(["grep", "-Eq", pattern], input="Bump actions/checkout", text=True)
        self.assertEqual(r.returncode, 1)


class ReleaseWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = workflows()["release.yml"]
        cls.jobs = jobs(cls.lines)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)

    def step(self, job, name):
        for step in steps(self.jobs[job]):
            if step[0].strip() == f"- name: {name}":
                return step
        self.fail(f"no step {name!r} in job {job}")

    def run_step(self, job, name, cwd, **env):
        """The step's script as GitHub runs it with `shell: bash`."""
        script = os.path.join(self.tmp, "step.sh")
        with open(script, "w") as f:
            f.write(next(scripts(self.step(job, name))))
        env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "HOME": self.tmp, "LANG": "C.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", **env}
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", script], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=60)

    def clear(self):
        """Removes what a run of a step left, for the next subtest."""
        shutil.rmtree(os.path.join(self.tmp, "repo"), ignore_errors=True)
        for name in ("calls", "output"):
            if os.path.exists(os.path.join(self.tmp, name)):
                os.remove(os.path.join(self.tmp, name))

    def git(self, cwd, *args):
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
                       cwd=cwd, check=True, capture_output=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})

    # -- what starts it and what it may do

    def test_only_the_button_starts_it(self):
        _, on = block(self.lines, "on")
        self.assertEqual([line.strip() for line in on if re.match(r"^  \S", line)], ["workflow_dispatch:"])

    def test_the_button_offers_a_dry_run_and_the_increment(self):
        _, on = block(self.lines, "on")
        _, inputs = block(block(on, "workflow_dispatch", 2)[1], "inputs", 4)
        _, dry_run = block(inputs, "dry_run", 6)
        self.assertIn("type: boolean", [line.strip() for line in dry_run])
        self.assertIn("default: true", [line.strip() for line in dry_run])
        _, increment = block(inputs, "increment", 6)
        self.assertEqual(block(increment, "options", 8)[0], "[auto, patch, minor, major]")
        self.assertEqual(block(increment, "default", 8)[0], "auto")

    def test_a_dry_run_builds_and_attaches_nothing(self):
        for job in ("rpm", "attach"):
            self.assertEqual(block(self.jobs[job], "if", 4)[0], "${{ !inputs.dry_run }}", job)
        self.assertIn("if: ${{ !inputs.dry_run }}", [line.strip() for line in self.step("release", "Hand the tag on")])

    def test_the_rpm_is_built_from_the_tag_for_the_fedora_releases_of_ci(self):
        """The installed CLI has to report the tag: rpm/install-test.sh hands it to tests/test_rpm.py."""
        rpm = self.jobs["rpm"]
        self.assertEqual(block(rpm, "needs", 4)[0], "release")
        checkout = next(step for step in steps(rpm) if uses(step).startswith("actions/checkout@"))
        self.assertIn("ref: ${{ needs.release.outputs.tag }}", [line.strip() for line in checkout])
        self.assertIn("TAG: ${{ needs.release.outputs.tag }}", [line.strip() for line in rpm])
        build = "".join(scripts(rpm))
        self.assertIn('/src/rpm/build.sh /out', build)
        self.assertIn('/src/rpm/install-test.sh "/out/$rpm" "$TAG"', build)
        fedora = re.compile(r"^\s+fedora: (\[.*\])$")
        ci = jobs(workflows()["ci.yml"])["rpm"]
        self.assertEqual([m.group(1) for line in rpm if (m := fedora.match(line))],
                         [m.group(1) for line in ci if (m := fedora.match(line))])

    def test_what_it_builds_comes_from_lockfiles_not_from_a_cache(self):
        for job, lines in self.jobs.items():
            for step in steps(lines):
                if uses(step).startswith("actions/setup-node@"):
                    stripped = [line.strip() for line in step]
                    self.assertIn("package-manager-cache: false", stripped, job)
                    self.assertFalse([line for line in stripped if line.startswith("cache:")], job)

    def test_ci_never_publishes_the_release(self):
        with open(os.path.join(REPO, ".release-it.json")) as f:
            github = json.load(f)["github"]
        self.assertEqual((github["release"], github["draft"]), (True, True))
        text = "\n".join(self.lines)
        for publishing in ("gh release edit", "gh release create", "--draft=false", "draft: false"):
            self.assertNotIn(publishing, text)

    # -- its scripts, run

    def test_it_refuses_to_run_off_main(self):
        r = self.run_step("release", "Refuse to run off main", self.tmp, GITHUB_REF="refs/heads/main")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for ref in ("refs/heads/release-button", "refs/tags/v0.1.0", "refs/heads/main-old"):
            r = self.run_step("release", "Refuse to run off main", self.tmp, GITHUB_REF=ref)
            self.assertEqual(r.returncode, 1, ref)
            self.assertIn(f"::error::A Release is cut from main only, not from {ref}.", r.stdout)

    def release_it(self, increment, dry_run=False, next_version="", fail=False):
        """Runs the release-it step with a fake release-it; returns its result and its calls."""
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "node_modules", ".bin"))
        self.git(repo, "init", "-q")
        calls = os.path.join(self.tmp, "calls")
        make_executable(os.path.join(repo, "node_modules", ".bin", "release-it"), textwrap.dedent("""\
            #!/bin/bash
            echo "$*" >> "$CALLS"
            if [ "$*" = "--ci --release-version" ]; then
              [ -z "$FAIL" ] || { echo "There are no commits since the latest tag." >&2; exit 1; }
              [ -z "$NEXT" ] || echo "$NEXT"
            fi
            """))
        r = self.run_step("release", "Run release-it", repo, DRY_RUN="true" if dry_run else "false",
                          INCREMENT=increment, GITHUB_TOKEN="token", CALLS=calls, NEXT=next_version,
                          FAIL="1" if fail else "")
        made = []
        if os.path.exists(calls):
            with open(calls) as f:
                made = f.read().splitlines()
        return r, made

    def test_auto_leaves_the_version_to_the_commits(self):
        r, calls = self.release_it("auto", next_version="0.2.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, ["--ci --release-version", "--ci"])

    def test_auto_with_only_commits_that_raise_no_version_is_a_patch(self):
        r, calls = self.release_it("auto")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, ["--ci --release-version", "--ci patch"])

    def test_a_chosen_increment_is_handed_to_release_it(self):
        for increment in ("patch", "minor", "major"):
            with self.subTest(increment):
                self.clear()
                r, calls = self.release_it(increment)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(calls, [f"--ci {increment}"])

    def test_a_dry_run_is_handed_to_release_it(self):
        r, calls = self.release_it("auto", dry_run=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, ["--ci --release-version", "--ci --dry-run patch"])

    def test_an_unknown_increment_runs_nothing(self):
        r, calls = self.release_it("premajor")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(calls, [])
        self.assertIn("::error::Unknown increment: premajor", r.stdout)

    def test_a_failing_release_it_stops_the_step(self):
        r, calls = self.release_it("auto", fail=True)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(calls, ["--ci --release-version"])
        self.assertIn("There are no commits since the latest tag.", r.stderr)

    def test_release_it_commits_as_the_actions_bot(self):
        self.release_it("patch")
        r = subprocess.run(["git", "config", "--local", "--get-regexp", "^user\\."], cwd=os.path.join(self.tmp, "repo"),
                           capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.splitlines(), ["user.name github-actions[bot]",
                                                 "user.email 41898282+github-actions[bot]@users.noreply.github.com"])

    def tag_step(self, tag_at_head=True, tag_earlier=False):
        repo = os.path.join(self.tmp, "repo")
        os.mkdir(repo)
        self.git(repo, "init", "-q")
        with open(os.path.join(repo, "VERSION"), "w") as f:
            f.write("0.2.0\n")
        self.git(repo, "add", "VERSION")
        self.git(repo, "commit", "-q", "-m", "chore: release 0.2.0")
        if tag_earlier:
            self.git(repo, "tag", "-a", "v0.2.0", "-m", "Release 0.2.0")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "docs: later")
        elif tag_at_head:
            # release-it annotates its tags
            self.git(repo, "tag", "-a", "v0.2.0", "-m", "Release 0.2.0")
        output = os.path.join(self.tmp, "output")
        open(output, "w").close()
        r = self.run_step("release", "Hand the tag on", repo, GITHUB_OUTPUT=output)
        with open(output) as f:
            return r, f.read()

    def test_the_tag_of_the_release_commit_is_handed_on(self):
        r, output = self.tag_step()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(output, "tag=v0.2.0\n")

    def test_no_new_tag_stops_the_release(self):
        """release-it ends without an error when it finds no version: then nothing is built."""
        for case in ({"tag_at_head": False}, {"tag_earlier": True}):
            with self.subTest(**case):
                self.clear()
                r, output = self.tag_step(**case)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(output, "")
                self.assertIn("::error::release-it made no tag v0.2.0 on the release commit.", r.stdout)

    def attach(self, draft):
        rpms = os.path.join(self.tmp, "rpm")
        os.mkdir(rpms)
        for fedora in ("44", "45"):
            with open(os.path.join(rpms, f"agentbox-0.2.0-1.fc{fedora}.noarch.rpm"), "wb") as f:
                f.write(f"rpm for Fedora {fedora}".encode())
        calls = os.path.join(self.tmp, "gh-calls")
        make_executable(os.path.join(self.bin, "gh"), textwrap.dedent("""\
            #!/bin/bash
            echo "$*" >> "$CALLS"
            if [ "$1 $2" = "release view" ]; then echo "$DRAFT"; fi
            """))
        r = self.run_step("attach", "Attach the RPMs and SHA256SUMS to the draft release", rpms, TAG="v0.2.0",
                          GH_TOKEN="token", GH_REPO="likt0r/askos", CALLS=calls, DRAFT=draft)
        with open(calls) as f:
            return r, f.read().splitlines(), rpms

    def test_the_rpms_and_their_sha256_go_to_the_draft(self):
        r, calls, rpms = self.attach("true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(calls, ["release view v0.2.0 --json isDraft --jq .isDraft",
                                 "release upload v0.2.0 agentbox-0.2.0-1.fc44.noarch.rpm "
                                 "agentbox-0.2.0-1.fc45.noarch.rpm SHA256SUMS"])
        with open(os.path.join(rpms, "SHA256SUMS")) as f:
            sums = f.read()
        self.assertEqual(sums, "".join(
            f"{hashlib.sha256(f'rpm for Fedora {fedora}'.encode()).hexdigest()}  agentbox-0.2.0-1.fc{fedora}.noarch.rpm\n"
            for fedora in ("44", "45")))
        # what a Host runs after `gh release download` (docs/releasing.md)
        check = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=rpms, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_nothing_is_attached_to_a_published_release(self):
        r, calls, rpms = self.attach("false")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(calls, ["release view v0.2.0 --json isDraft --jq .isDraft"])
        self.assertIn("::error::The release v0.2.0 is not a draft: CI attaches files only to a draft.", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(rpms, "SHA256SUMS")))


if __name__ == "__main__":
    unittest.main()
