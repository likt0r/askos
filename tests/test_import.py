"""#47: `agentbox import` copies a folder of the Host into a VM, and agentbox-inbox, the
wrapper in the Image that unpacks it.

The real CLI runs with AGENTBOX_ROOT and fake virsh (fake_virsh.py) as in test_activity.py.
The fake attaches and detaches disks and, for guest-exec of agentbox-inbox, runs the real
wrapper of vm/files in the VM's own root (FAKE_VIRSH_DIR/guest-<vm>), its device the tar
file attached with the serial agentbox-inbox: what lands in that root's ~/code is what the
VM would get. The wrapper is also run directly (WrapperTest), with tar files made here.

Run from the repo root: python3 -m unittest discover -s tests
"""
import fcntl
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_activity import AGENTBOX, ActivityTest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-inbox"
WRAPPER = os.path.join(REPO, "vm", "files", INBOX.lstrip("/"))
DEVICE = "/dev/disk/by-id/virtio-agentbox-inbox"
CODE = "var/home/agent/code"
ATTACH_OPTIONS = ["--live", "--mode", "readonly", "--targetbus", "virtio", "--driver", "qemu", "--subdriver", "raw",
                  "--serial", "agentbox-inbox"]


def git_env(home):
    return {"PATH": os.environ["PATH"], "HOME": home, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.org", "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.org", "LC_ALL": "C"}


def write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


class ImportTest(ActivityTest):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.projects = os.path.join(self.dir, "projects")
        os.makedirs(self.projects)

    def git(self, cwd, *args):
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=git_env(self.home))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def code(self, *rel):
        """A path in the VM's ~/code (the fake VM's root)."""
        return os.path.join(self.dir, "guest-work", CODE, *rel)

    def transfers(self):
        d = self.path("var/lib/agentbox/transfers")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def attached(self):
        """The tar files the VM got (copies the fake kept)."""
        d = os.path.join(self.dir, "attached")
        return [os.path.join(d, n) for n in sorted(os.listdir(d))] if os.path.isdir(d) else []

    def failure(self, *args, exit_code=1):
        code, out, err = self.run_agentbox("import", *args, "--json")
        self.assertEqual((code, out), (exit_code, ""), err)
        return json.loads(err)

    def imported(self, *args):
        code, out, err = self.run_agentbox("import", *args, "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def repository(self, name="foo"):
        """A repository with work in progress: a branch, a stash, an unpushed commit, staged,
        modified, deleted, untracked and ignored files, a symbolic link, an executable and a hook."""
        src = os.path.join(self.projects, name)
        self.git(self.projects, "init", "-q", "-b", "main", name)
        write(os.path.join(src, "README.md"), "hello\n")
        write(os.path.join(src, "src", "app.py"), "print('app')\n")
        write(os.path.join(src, "run.sh"), "#!/bin/sh\necho run\n", 0o755)
        write(os.path.join(src, "gone.txt"), "deleted in the working tree\n")
        write(os.path.join(src, ".gitignore"), "node_modules/\n.env\n")
        os.symlink("README.md", os.path.join(src, "readme-link"))
        self.git(src, "add", "-A")
        self.git(src, "commit", "-q", "-m", "first")
        remote = os.path.join(self.dir, "remote.git")
        self.git(self.dir, "init", "-q", "--bare", remote)
        self.git(src, "remote", "add", "origin", remote)
        self.git(src, "push", "-q", "-u", "origin", "main")
        write(os.path.join(src, "src", "app.py"), "print('second')\n")
        self.git(src, "commit", "-q", "-am", "second, not pushed")
        self.git(src, "checkout", "-q", "-b", "feature")
        write(os.path.join(src, "feature.txt"), "feature\n")
        self.git(src, "add", "feature.txt")
        self.git(src, "commit", "-q", "-m", "on a branch")
        self.git(src, "checkout", "-q", "main")
        write(os.path.join(src, "README.md"), "stashed change\n")
        self.git(src, "stash", "-q")
        write(os.path.join(src, "staged.txt"), "staged\n")
        self.git(src, "add", "staged.txt")
        write(os.path.join(src, "src", "app.py"), "print('modified')\n")
        os.remove(os.path.join(src, "gone.txt"))
        write(os.path.join(src, "notes.txt"), "untracked\n")
        write(os.path.join(src, "node_modules", "dep", "index.js"), "ignored\n")
        write(os.path.join(src, ".env"), "SECRET=ignored\n")
        write(os.path.join(src, ".git", "hooks", "pre-commit"), "#!/bin/sh\nexit 0\n", 0o755)
        return src

    def state(self, folder):
        """What Git says about a repository: status, stashes, all commits and branches."""
        return {"status": self.git(folder, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-b"),
                "stash": self.git(folder, "stash", "list", "--format=%gd %s"),
                "log": self.git(folder, "log", "--all", "--format=%H %s"),
                "branches": self.git(folder, "branch", "-a", "-vv", "--format=%(refname) %(objectname) %(upstream)")}


class RepositoryTest(ImportTest):
    def test_a_repository_arrives_with_its_work_in_progress(self):
        src = self.repository()
        before = self.state(src)
        result = self.imported("work", src)
        self.assertEqual((result["vm"], result["folder"], result["target"], result["path"]),
                         ("work", src, "foo", "/var/home/agent/code/foo"))
        dest = self.code("foo")
        self.assertEqual(self.state(dest), before)
        self.assertIn("stashed change", self.git(dest, "stash", "show", "-p"))
        with open(os.path.join(dest, "src", "app.py")) as f:
            self.assertEqual(f.read(), "print('modified')\n")
        self.assertEqual(os.readlink(os.path.join(dest, "readme-link")), "README.md")
        self.assertTrue(os.stat(os.path.join(dest, "run.sh")).st_mode & stat.S_IXUSR)
        self.assertTrue(os.path.isfile(os.path.join(dest, "notes.txt")))

    def test_ignored_files_stay_out(self):
        src = self.repository()
        self.imported("work", src)
        for rel in ("node_modules", ".env", "gone.txt"):
            self.assertFalse(os.path.lexists(self.code("foo", rel)), rel)
        [tar] = self.attached()
        with tarfile.open(tar) as t:
            names = t.getnames()
        self.assertIn(".git/HEAD", names)
        self.assertIn(".git/refs/stash", names)
        self.assertFalse([n for n in names if n.startswith("node_modules") or n == ".env"], names)
        # the whole .git: every file of it is in the archive
        in_git = {os.path.relpath(os.path.join(d, f), src) for d, _, files in os.walk(os.path.join(src, ".git"))
                  for f in files}
        self.assertEqual(in_git - set(names), set())

    def test_hooks_are_removed_except_the_samples(self):
        src = self.repository()
        self.imported("work", src)
        hooks = os.listdir(self.code("foo", ".git", "hooks"))
        self.assertNotIn("pre-commit", hooks)
        self.assertTrue(hooks)
        self.assertTrue(all(h.endswith(".sample") for h in hooks), hooks)

    def test_a_linked_worktree_is_refused_with_a_hint_to_its_main_repository(self):
        src = self.repository()
        linked = os.path.join(self.projects, "foo-feature")
        self.git(src, "worktree", "add", "-q", linked, "feature")
        said = self.failure("work", linked)
        self.assertEqual(said["error"], "linked_worktree")
        self.assertIn(f"import its main repository instead: {src}", said["message"])
        self.assertEqual((self.calls(), self.transfers()), ([], []))

    def test_a_git_that_is_a_symbolic_link_is_refused_as_such(self):
        src = self.repository()
        elsewhere = os.path.join(self.projects, "elsewhere")
        os.rename(os.path.join(src, ".git"), elsewhere)
        os.symlink(elsewhere, os.path.join(src, ".git"))
        said = self.failure("work", src)
        self.assertEqual(said["error"], "not_a_folder")
        self.assertIn(".git is a symbolic link", said["message"])
        self.assertNotIn("worktree", said["message"])
        self.assertEqual((self.calls(), self.transfers()), ([], []))

    def test_a_repository_inside_the_folder_goes_by_the_same_rule(self):
        src = self.repository()
        inner = os.path.join(src, "vendor", "lib")
        os.makedirs(os.path.dirname(inner))
        self.git(os.path.dirname(inner), "init", "-q", "-b", "main", "lib")
        write(os.path.join(inner, ".gitignore"), "build/\n")
        write(os.path.join(inner, "lib.c"), "int x;\n")
        write(os.path.join(inner, "build", "lib.o"), "ignored\n")
        self.imported("work", src)
        self.assertTrue(os.path.isfile(self.code("foo", "vendor", "lib", "lib.c")))
        self.assertTrue(os.path.isfile(self.code("foo", "vendor", "lib", ".git", "HEAD")))
        self.assertFalse(os.path.lexists(self.code("foo", "vendor", "lib", "build")))


class FolderTest(ImportTest):
    def test_a_hard_link_counts_as_a_file_on_both_sides(self):
        src = os.path.join(self.projects, "hl")
        write(os.path.join(src, "a.txt"), "abc\n")
        os.link(os.path.join(src, "a.txt"), os.path.join(src, "b.txt"))
        answer = self.imported("work", src)
        self.assertEqual((answer["files"], answer["bytes"]), (2, 4))
        self.assertEqual(os.stat(self.code("hl", "b.txt")).st_size, 4)

    def test_a_folder_without_git_goes_completely(self):
        src = os.path.join(self.projects, "notes")
        write(os.path.join(src, "a.txt"), "a\n")
        write(os.path.join(src, ".env"), "x\n")
        write(os.path.join(src, "node_modules", "m.js"), "m\n")
        os.makedirs(os.path.join(src, "empty"))
        result = self.imported("work", src)
        self.assertEqual((result["files"], result["bytes"]), (3, 6))
        for rel in ("a.txt", ".env", "node_modules/m.js"):
            self.assertTrue(os.path.isfile(self.code("notes", rel)), rel)
        self.assertTrue(os.path.isdir(self.code("notes", "empty")))

    def test_a_target_with_parents_and_the_default_target(self):
        src = os.path.join(self.projects, "foo")
        write(os.path.join(src, "a.txt"), "a\n")
        self.assertEqual(self.imported("work", src + "/", "kunden/foo-neu/")["target"], "kunden/foo-neu")
        self.assertTrue(os.path.isfile(self.code("kunden", "foo-neu", "a.txt")))
        self.assertEqual(self.imported("work", src)["target"], "foo")
        self.assertEqual(self.calls("guest-exec")[-1]["arg"], ["receive", DEVICE, "foo"])

    def test_the_archive_holds_no_setuid_bit_owner_or_path_leading_out(self):
        src = os.path.join(self.projects, "odd")
        write(os.path.join(src, "tool"), "#!/bin/sh\n", 0o755)
        os.chmod(os.path.join(src, "tool"), 0o4755)
        os.symlink("/etc/passwd", os.path.join(src, "absolute-link"))
        os.symlink("../../outside", os.path.join(src, "outside-link"))
        os.symlink("tool", os.path.join(src, "inside-link"))
        os.mkfifo(os.path.join(src, "fifo"))
        result = self.imported("work", src)
        self.assertEqual(sorted((s["path"], s["reason"]) for s in result["skipped"]),
                         [("absolute-link", "link_outside"), ("fifo", "special_file"),
                          ("outside-link", "link_outside")])
        [tar] = self.attached()
        with tarfile.open(tar) as t:
            members = t.getmembers()
        self.assertEqual(sorted(m.name for m in members), ["inside-link", "tool"])
        for m in members:
            self.assertEqual((m.uid, m.gid, m.uname, m.gname, m.mode & 0o7000), (0, 0, "", "", 0), m.name)
            self.assertFalse(m.name.startswith("/") or ".." in m.name.split("/"), m.name)
        self.assertEqual(os.stat(self.code("odd", "tool")).st_mode & 0o7000, 0)
        code, out, _ = self.run_agentbox("import", "work", src, "odd2")
        self.assertEqual(code, 0)
        self.assertIn("skipped 3 (links leading out of the folder, special files)", out)

    def test_text_output_names_files_and_bytes(self):
        src = os.path.join(self.projects, "foo")
        write(os.path.join(src, "a.txt"), "x" * 2048)
        code, out, err = self.run_agentbox("import", "work", src)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"work: {src} is in ~/code/foo (1 files, 2.0 KiB)\n")


class TransportTest(ImportTest):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.projects, "foo")
        write(os.path.join(self.src, "a.txt"), "a\n")

    def assertCleanedUp(self):
        """Detached after the attach, the tar file deleted."""
        cmds = [c["cmd"] for c in self.calls() if c["cmd"] in ("attach-disk", "detach-disk")]
        self.assertEqual(cmds, ["attach-disk", "detach-disk"])
        self.assertEqual(self.calls("detach-disk")[0]["target"], self.calls("attach-disk")[0]["target"])
        self.assertEqual(self.calls("detach-disk")[0]["options"], ["--live"])
        self.assertEqual(self.transfers(), [])

    def test_attached_read_only_raw_without_a_filesystem_then_detached_and_deleted(self):
        self.imported("work", self.src)
        [attach] = self.calls("attach-disk")
        self.assertEqual((attach["dom"], attach["target"], attach["options"]), ("agentbox-work", "vdc", ATTACH_OPTIONS))
        self.assertTrue(attach["source"].startswith(self.path("var/lib/agentbox/transfers/work-")), attach)
        self.assertTrue(attach["source"].endswith(".tar"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path("var/lib/agentbox/transfers")).st_mode), 0o711)
        self.assertEqual([c["path"] for c in self.calls("guest-exec")], [INBOX])
        self.assertCleanedUp()

    def test_an_existing_target_is_refused_and_left_alone(self):
        write(self.code("foo", "mine.txt"), "mine\n")
        said = self.failure("work", self.src)
        self.assertEqual((said["error"], said["params"]), ("target_exists", {"vm": "work", "target": "foo"}))
        self.assertEqual(os.listdir(self.code("foo")), ["mine.txt"])
        self.assertCleanedUp()

    def test_a_build_without_the_wrapper_fails_with_its_code(self):
        self.scenario(missing_wrappers=[INBOX])
        said = self.failure("work", self.src)
        self.assertEqual(said["error"], "inbox_missing")
        self.assertIn("agentbox build <image> --reset work", said["message"])
        self.assertCleanedUp()

    def test_a_guest_agent_not_answering(self):
        self.scenario(agent_down=True)
        self.assertEqual(self.failure("work", self.src, exit_code=4)["error"], "guest_agent_unavailable")
        self.assertCleanedUp()

    def test_unpacking_failing_in_the_vm(self):
        self.scenario(responses=[{"exitcode": 5, "stderr": "agentbox-inbox: archive refused:\x1b[2J bad‮"}])
        said = self.failure("work", self.src)
        self.assertEqual(said["error"], "import_failed")
        self.assertIn("unpacking in the VM fails: archive refused:[2J bad", said["message"])
        self.assertNotIn("\x1b", said["message"])
        self.assertNotIn("‮", said["message"])
        self.assertCleanedUp()

    def test_the_answer_of_the_vm_is_untrusted(self):
        for answer in ('{"files": true, "bytes": 1}', '{"files": -1, "bytes": 1}', '{"files": 1}', "[1, 2]",
                       '{"files": 1.5, "bytes": 2}', "not json", '{"files": 1, "bytes": 99999999999999999999}'):
            with self.subTest(answer=answer):
                self.scenario(responses=[{"stdout": answer}])
                self.assertEqual(self.failure("work", self.src)["error"], "import_failed")
                self.assertEqual(self.transfers(), [])

    def test_attaching_failing(self):
        self.scenario(attach_fails=True)
        said = self.failure("work", self.src)
        self.assertEqual(said["error"], "import_failed")
        self.assertIn("cannot be attached", said["message"])
        self.assertEqual((self.calls("guest-exec"), self.transfers()), ([], []))
        self.assertEqual(self.attached(), [])

    def test_a_leftover_tar_file_nobody_holds_is_removed_by_the_next_import(self):
        d = self.path("var/lib/agentbox/transfers")
        os.makedirs(d)
        orphan, held = os.path.join(d, "work-orphan.tar"), os.path.join(d, "work-held.tar")
        write(orphan, "a copy of a folder")
        write(held, "of a running import")
        with open(held, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.imported("work", self.src)
            self.assertEqual(self.transfers(), ["work-held.tar"])
        self.assertFalse(os.path.exists(orphan))

    def test_detaching_failing_still_records_files_and_bytes_in_activity(self):
        self.scenario(detach_fails=True)
        self.failure("work", self.src)
        [entry] = [e for e in self.entries() if e.get("AGENTBOX_ACTION") == "import"]
        self.assertEqual((entry["AGENTBOX_OUTCOME"], entry["AGENTBOX_PARAM_FILES"], entry["AGENTBOX_PARAM_BYTES"]),
                         ("failed", "1", "2"))

    def test_detaching_failing_is_an_error_even_when_the_folder_arrived(self):
        self.scenario(detach_fails=True)
        said = self.failure("work", self.src)
        self.assertEqual(said["error"], "import_failed")
        self.assertIn("the folder is in ~/code/foo, but the disk vdc could not be detached", said["message"])
        self.assertTrue(os.path.isfile(self.code("foo", "a.txt")))
        self.assertEqual(self.transfers(), [])


    def test_a_stopped_import_detaches_the_disk_and_the_next_one_works(self):
        self.scenario(attach_signals_parent=True)
        code, out, err = self.run_agentbox("import", "work", self.src)
        self.assertNotEqual(code, 0, out)
        self.assertCleanedUp()
        self.scenario()
        self.assertEqual(self.imported("work", self.src)["files"], 1)

    def test_a_disk_left_by_an_interrupted_import_is_detached_first(self):
        self.scenario(vms={"work": {"disks": {"vdc": {"source": "/gone.tar", "serial": "agentbox-inbox"}}}})
        self.assertEqual(self.imported("work", self.src)["files"], 1)
        cmds = [c["cmd"] for c in self.calls() if c["cmd"] in ("attach-disk", "detach-disk")]
        self.assertEqual(cmds, ["detach-disk", "attach-disk", "detach-disk"])
        self.assertEqual(self.transfers(), [])

    def test_a_leftover_disk_that_cannot_be_detached_stops_the_import(self):
        self.scenario(detach_fails=True,
                      vms={"work": {"disks": {"vdc": {"source": "/gone.tar", "serial": "agentbox-inbox"}}}})
        said = self.failure("work", self.src)
        self.assertEqual(said["error"], "import_failed")
        self.assertIn("vdc", said["message"])
        self.assertEqual((self.calls("attach-disk"), self.transfers()), ([], []))


class PackTest(unittest.TestCase):
    def test_a_child_is_killed_and_reaped_when_the_parent_is_interrupted(self):
        sys.path.insert(0, REPO)
        from agentbox import transfer
        pids = []
        real_fork = os.fork

        def fork():
            pid = real_fork()
            pids.append(pid)
            return pid

        def slow(source, fd):
            time.sleep(60)

        def interrupt(signum, frame):
            raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as d, open(os.path.join(d, "out"), "wb") as out:
            old = signal.signal(signal.SIGALRM, interrupt)
            self.addCleanup(signal.signal, signal.SIGALRM, old)
            self.addCleanup(signal.alarm, 0)
            self.addCleanup(setattr, transfer, "_archive", transfer._archive)
            self.addCleanup(setattr, os, "fork", real_fork)
            transfer._archive, os.fork = slow, fork
            signal.alarm(1)
            with self.assertRaises(KeyboardInterrupt):
                transfer.pack(d, out.fileno())
        os.fork = real_fork
        [pid] = [p for p in pids if p]
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)               # reaped already
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class IdentityTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, REPO)
        from agentbox import transfer
        self.transfer = transfer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SUDO_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_under_sudo_packing_runs_as_the_invoking_user(self):
        if os.getuid() == 0:
            self.skipTest("needs a user that is not root")
        os.environ.update(SUDO_UID=str(os.getuid()), SUDO_GID=str(os.getgid()))
        folder = os.path.join(self.tmp.name, "f")
        os.mkdir(folder)
        self.assertEqual(self.transfer._identity(folder), (os.getuid(), os.getgid()))

    def test_without_sudo_or_with_a_useless_sudo_environment_it_is_the_owner_of_the_folder(self):
        folder = os.path.join(self.tmp.name, "f")
        os.mkdir(folder)
        owner = (os.stat(folder).st_uid, os.stat(folder).st_gid)
        for env in ({}, {"SUDO_UID": "0", "SUDO_GID": "0"}, {"SUDO_UID": "x", "SUDO_GID": "1"},
                    {"SUDO_UID": "1000"}, {"SUDO_UID": "4000000000", "SUDO_GID": "1"},
                    {"SUDO_UID": "-5", "SUDO_GID": "1"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(self.transfer._identity(folder), owner)

    def test_the_child_switches_to_the_invoking_user_when_root(self):
        if os.getuid() == 0:
            self.skipTest("needs a user that is not root")
        os.environ.update(SUDO_UID=str(os.getuid()), SUDO_GID=str(os.getgid()))
        seen = os.path.join(self.tmp.name, "seen")

        def become(uid, gid):
            with open(seen, "w") as f:
                json.dump([uid, gid], f)
        folder = os.path.join(self.tmp.name, "f")
        os.mkdir(folder)
        with open(os.path.join(self.tmp.name, "out"), "wb") as out, \
                mock.patch.object(self.transfer, "_become", become), mock.patch("os.geteuid", return_value=0):
            self.transfer.pack(folder, out.fileno())
        with open(seen) as f:
            self.assertEqual(json.load(f), [os.getuid(), os.getgid()])


class LifecycleTest(ImportTest):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.projects, "foo")
        write(os.path.join(self.src, "a.txt"), "a\n")

    def test_a_vm_that_is_off_is_started_first(self):
        self.scenario(state="shut off")
        self.imported("work", self.src)
        cmds = [c["cmd"] for c in self.calls() if c["cmd"] in ("start", "attach-disk", "detach-disk")]
        self.assertEqual(cmds, ["start", "attach-disk", "detach-disk"])

    def test_a_paused_vm_is_refused(self):
        self.scenario(vms={"work": {"state": "shut off", "managed_save": True}})
        said = self.failure("work", self.src)
        self.assertEqual(said["error"], "vm_paused")
        self.assertEqual((self.calls(), self.transfers(), self.attached()), ([], [], []))

    def test_no_pause_or_reset_comes_between_attach_and_delete(self):
        # every guest-agent call takes a second: the import holds the lifecycle lock meanwhile
        for n, (cmd, stop) in enumerate((("reset", "managedsave-remove"), ("pause", "managedsave"))):
            with self.subTest(cmd):
                self.scenario(agent_hangs=1)
                if os.path.exists(os.path.join(self.dir, "calls.jsonl")):
                    os.remove(os.path.join(self.dir, "calls.jsonl"))
                p = self.background("import", "work", self.src, f"foo{n}")
                deadline = time.monotonic() + 20
                while not self.calls("attach-disk"):
                    self.assertLess(time.monotonic(), deadline)
                    self.assertIsNone(p.poll())
                    time.sleep(0.05)
                other = self.background(cmd, "work")
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
                other.communicate(timeout=60)
                cmds = [c["cmd"] for c in self.calls()]
                self.assertIn(stop, cmds)
                self.assertLess(cmds.index("detach-disk"), cmds.index(stop), cmds)
                self.assertEqual(self.transfers(), [])

    def background(self, *args):
        p = subprocess.Popen([AGENTBOX, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=self.environment())
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def environment(self):
        return {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
                "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC", **self.env}


class InputTest(ImportTest):
    def test_unknown_vm_missing_folder_and_a_file(self):
        write(os.path.join(self.projects, "file.txt"), "x\n")
        for args, error in ((("nope", self.projects), "vm_not_found"),
                            (("work", os.path.join(self.projects, "missing")), "folder_not_found"),
                            (("work", os.path.join(self.projects, "file.txt")), "not_a_folder")):
            with self.subTest(args=args):
                self.assertEqual(self.failure(*args)["error"], error)
        self.assertEqual((self.calls(), self.transfers()), ([], []))

    def test_targets_outside_code_are_refused(self):
        src = os.path.join(self.projects, "foo")
        os.makedirs(src)
        for target in ("/var/home/agent/x", "../x", "a/../../x", "a//b", ".", "a/./b", "bad\x1bname"):
            with self.subTest(target=target):
                said = self.failure("work", src, target, exit_code=2)
                self.assertEqual((said["error"], said["params"]), ("invalid_argument", {"argument": "target"}))
        self.assertEqual(self.failure("work", "/", exit_code=2)["error"], "invalid_argument")
        self.assertEqual(self.failure("work", exit_code=2)["error"], "invalid_argument")
        self.assertEqual(self.failure("work", src, "--force", exit_code=2)["error"], "invalid_argument")
        self.assertEqual((self.calls(), self.transfers()), ([], []))


class ActivityEntryTest(ImportTest):
    def test_every_import_is_recorded_with_folder_target_files_and_bytes(self):
        src = os.path.join(self.projects, "foo")
        write(os.path.join(src, "a.txt"), "abc\n")
        self.imported("work", src, "x/foo")
        write(self.code("foo", "mine.txt"), "mine\n")
        self.failure("work", src)
        ok, failed = [e for e in self.entries() if e.get("AGENTBOX_ACTION") == "import"]
        self.assertEqual((ok["AGENTBOX_VM"], ok["AGENTBOX_OUTCOME"], ok["AGENTBOX_PARAM_FOLDER"],
                          ok["AGENTBOX_PARAM_TARGET"], ok["AGENTBOX_PARAM_FILES"], ok["AGENTBOX_PARAM_BYTES"]),
                         ("work", "ok", src, "x/foo", "1", "4"))
        self.assertEqual(ok["AGENTBOX_DESCRIPTION"], f"Import {src} into ~/code/x/foo")
        self.assertEqual((failed["AGENTBOX_OUTCOME"], failed["AGENTBOX_ERROR"], failed["AGENTBOX_PARAM_TARGET"]),
                         ("failed", "target_exists", "foo"))
        self.assertNotIn("AGENTBOX_PARAM_FILES", failed)


@unittest.skipIf(os.geteuid() == 0, "checks what happens without root")
class RootTest(ImportTest):
    def test_needs_root(self):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_VIRSH_DIR": self.dir,
               "LANG": "C.UTF-8"}
        r = subprocess.run([AGENTBOX, "import", "work", self.projects], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"sudo agentbox import work {self.projects}", r.stderr)
        self.assertEqual(self.calls(), [])


@unittest.skipIf(os.geteuid() == 0, "without root the wrapper stays in its test root")
class WrapperTest(unittest.TestCase):
    """agentbox-inbox, run directly: its device and ~/code under a test root (AGENTBOX_INBOX_ROOT)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.code = os.path.join(self.root, CODE)
        os.makedirs(self.code)

    def archive(self, *members, built_elsewhere=False):
        """A tar file as the device: members are (TarInfo, bytes or None). built_elsewhere: written next to the
        device and renamed into place, so a reader sees it whole or not at all."""
        device = self.root + DEVICE
        os.makedirs(os.path.dirname(device), exist_ok=True)
        target = device + ".part" if built_elsewhere else device
        with tarfile.open(target, "w", format=tarfile.PAX_FORMAT) as t:
            for info, data in members:
                if data is not None:
                    info.size = len(data)
                t.addfile(info, io.BytesIO(data) if data is not None else None)
        if built_elsewhere:
            os.rename(target, device)

    def run_wrapper(self, *args):
        r = subprocess.run([sys.executable, "-I", WRAPPER, *args], capture_output=True, text=True,
                           env={"PATH": os.environ["PATH"], "AGENTBOX_INBOX_ROOT": self.root})
        return r.returncode, r.stdout, r.stderr

    def receive(self, target="foo"):
        return self.run_wrapper("receive", DEVICE, target)

    def everything(self):
        """Every path under the test root except the device."""
        return sorted(os.path.relpath(os.path.join(d, n), self.root) for d, dirs, files in os.walk(self.root)
                      for n in dirs + files if not os.path.join(d, n).startswith(self.root + "/dev"))

    def test_unpacks_and_counts(self):
        self.archive((tarfile.TarInfo("a.txt"), b"abc"), (dir_info("sub"), None), (tarfile.TarInfo("sub/b"), b"12"))
        code, out, err = self.receive("x/y/foo")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"files": 2, "bytes": 5})
        with open(os.path.join(self.code, "x/y/foo/sub/b")) as f:
            self.assertEqual(f.read(), "12")

    def test_path_traversal_is_refused_and_nothing_stays(self):
        self.archive((tarfile.TarInfo("ok.txt"), b"ok"), (tarfile.TarInfo("../evil"), b"x"))
        code, _, err = self.receive()
        self.assertEqual(code, 5, err)
        self.assertIn("outside the destination", err)
        self.assertEqual(self.everything(), ["var", "var/home", "var/home/agent", "var/home/agent/code"])

    def test_an_absolute_name_stays_inside_the_target(self):
        self.archive((tarfile.TarInfo(self.root + "/evil"), b"x"))
        self.receive()
        self.assertFalse(os.path.lexists(os.path.join(self.root, "evil")))
        self.assertTrue(all(p.startswith(CODE) or CODE.startswith(p) for p in self.everything()), self.everything())

    def test_links_out_of_the_target_are_refused(self):
        for linkname in ("../../outside", "/etc", self.root):
            with self.subTest(linkname=linkname):
                self.archive((link_info("link", linkname), None), (tarfile.TarInfo("link/x"), b"x"))
                code, _, err = self.receive()
                self.assertEqual(code, 5, err)
                self.assertEqual(os.listdir(self.code), [])

    def test_setuid_bits_are_dropped(self):
        info = tarfile.TarInfo("tool")
        info.mode = 0o6755
        self.archive((info, b"#!/bin/sh\n"))
        self.assertEqual(self.receive()[0], 0)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.code, "foo", "tool")).st_mode), 0o755)

    def test_device_files_are_refused(self):
        info = tarfile.TarInfo("null")
        info.type, info.devmajor, info.devminor = tarfile.CHRTYPE, 1, 3
        self.archive((info, None))
        self.assertEqual(self.receive()[0], 5)
        self.assertEqual(os.listdir(self.code), [])

    def test_an_existing_target_is_refused_without_overwriting(self):
        write(os.path.join(self.code, "foo", "a.txt"), "mine")
        self.archive((tarfile.TarInfo("a.txt"), b"theirs"))
        code, _, err = self.receive()
        self.assertEqual(code, 3, err)
        with open(os.path.join(self.code, "foo", "a.txt")) as f:
            self.assertEqual(f.read(), "mine")

    def test_targets_leading_out_of_code_are_refused(self):
        self.archive((tarfile.TarInfo("a.txt"), b"a"))
        for target in ("../x", "/abs", "a/../../x", "", "a//b"):
            with self.subTest(target=target):
                self.assertEqual(self.receive(target)[0], 2)
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.code, "out"))
        code, _, err = self.receive("out/new")
        self.assertEqual(code, 2, err)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_hooks_but_the_samples_are_removed(self):
        self.archive((dir_info(".git"), None), (dir_info(".git/hooks"), None),
                     (tarfile.TarInfo(".git/hooks/pre-commit"), b"#!/bin/sh\n"),
                     (tarfile.TarInfo(".git/hooks/pre-push.sample"), b"#!/bin/sh\n"),
                     (dir_info(".git/hooks/pre-commit.d"), None),
                     (tarfile.TarInfo(".git/hooks/pre-commit.d/x"), b"x"),
                     (link_info(".git/hooks/post-merge", "pre-push.sample"), None))
        self.assertEqual(self.receive()[0], 0)
        self.assertEqual(os.listdir(os.path.join(self.code, "foo", ".git", "hooks")), ["pre-push.sample"])

    def test_hooks_of_nested_repositories_and_submodules_are_removed_too(self):
        hook = (tarfile.TarInfo, b"#!/bin/sh\n")
        members = [(dir_info(".git"), None), (dir_info(".git/hooks"), None)]
        for git in (".git", "vendor/.git", ".git/modules/sub", ".git/modules/sub/modules/deep"):
            if git != ".git":
                members += [(dir_info(git), None), (tarfile.TarInfo(git + "/HEAD"), b"ref: refs/heads/main\n")]
            members += [(dir_info(git + "/hooks"), None), (tarfile.TarInfo(git + "/hooks/pre-commit"), hook[1]),
                        (tarfile.TarInfo(git + "/hooks/keep.sample"), hook[1])]
        members += [(dir_info("sub"), None), (link_info("sub/.git", "../.git/modules/sub"), None)]
        self.archive(*members)
        self.assertEqual(self.receive()[0], 0)
        foo = os.path.join(self.code, "foo")
        for git in (".git", "vendor/.git", ".git/modules/sub", ".git/modules/sub/modules/deep"):
            self.assertEqual(os.listdir(os.path.join(foo, git, "hooks")), ["keep.sample"], git)

    def test_hooks_behind_a_link_are_not_followed(self):
        outside = os.path.join(self.root, "outside", "hooks")
        write(os.path.join(outside, "pre-commit"), "x")
        self.archive((dir_info(".git"), None), (dir_info("lnk"), None), (link_info("lnk/.git", "../.git"), None))
        self.assertEqual(self.receive()[0], 0)
        self.assertTrue(os.path.exists(os.path.join(outside, "pre-commit")))

    def test_a_device_that_appears_late_is_waited_for(self):
        device = self.root + DEVICE
        os.makedirs(os.path.dirname(device))
        env = {"PATH": os.environ["PATH"], "AGENTBOX_INBOX_ROOT": self.root, "AGENTBOX_INBOX_DEVICE_WAIT": "20"}
        p = subprocess.Popen([sys.executable, "-I", WRAPPER, "receive", DEVICE, "foo"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        self.archive((tarfile.TarInfo("a.txt"), b"abc"), built_elsewhere=True)
        out, err = p.communicate(timeout=30)
        self.assertEqual((p.returncode, json.loads(out)), (0, {"files": 1, "bytes": 3}), err)

    def test_a_device_that_never_appears_fails_with_a_clear_message(self):
        env = {"PATH": os.environ["PATH"], "AGENTBOX_INBOX_ROOT": self.root, "AGENTBOX_INBOX_DEVICE_WAIT": "1"}
        r = subprocess.run([sys.executable, "-I", WRAPPER, "receive", DEVICE, "x/foo"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(f"{DEVICE} does not appear", r.stderr)
        self.assertEqual(self.everything(), ["var", "var/home", "var/home/agent", "var/home/agent/code"])

    def test_parents_made_for_a_failed_run_are_removed_but_not_those_that_were_there(self):
        self.archive((tarfile.TarInfo("ok.txt"), b"ok"), (tarfile.TarInfo("../evil"), b"x"))
        os.makedirs(os.path.join(self.code, "there"))
        self.assertEqual(self.receive("a/b/foo")[0], 5)
        self.assertEqual(self.receive("there/c/foo")[0], 5)
        self.assertEqual(os.listdir(self.code), ["there"])
        self.assertEqual(os.listdir(os.path.join(self.code, "there")), [])

    def test_only_its_device_and_fsfreeze_calls(self):
        self.archive((tarfile.TarInfo("a.txt"), b"a"))
        self.assertEqual(self.run_wrapper("receive", "/dev/vda", "foo")[0], 2)
        self.assertEqual(self.run_wrapper("rm", "-rf")[0], 2)
        for arg in ("freeze", "thaw"):
            self.assertEqual(self.run_wrapper(arg)[:2], (0, ""))
        self.assertEqual(os.listdir(self.code), [])


def dir_info(name):
    info = tarfile.TarInfo(name)
    info.type, info.mode = tarfile.DIRTYPE, 0o755
    return info


def link_info(name, target):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target
    return info


if __name__ == "__main__":
    unittest.main()
