"""How agentbox reads and writes its files (agentbox/state_files.py) and what qemu-img says
about a disk (programs.disk_info).

Works in a temporary directory; owners are only asked for, never changed (tests run as a
normal user).

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import programs, state_files  # noqa: E402
from agentbox.errors import Fail  # noqa: E402


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def path(self, *names):
        return os.path.join(self.dir, *names)

    def file(self, name, data, mode=0o644):
        with open(self.path(name), "wb") as f:
            f.write(data)
        os.chmod(self.path(name), mode)
        return self.path(name)

    def fails(self, call, *args, **kw):
        with self.assertRaises(Fail) as caught:
            call(*args, **kw)
        return caught.exception

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    # --- reading ---

    def test_read_gives_the_bytes_or_the_default_when_missing(self):
        self.assertEqual(state_files.read(self.file("a", b"x\n")), b"x\n")
        self.assertEqual(state_files.read(self.path("none"), b""), b"")
        self.assertIsNone(state_files.read(self.path("none"), None))

    def test_read_without_a_default_or_of_a_directory_ends_the_command(self):
        f = self.fails(state_files.read, self.path("none"))
        self.assertEqual((f.message, f.error, f.params),
                         (f"{self.path('none')}: No such file or directory", "action_failed", {}))
        f = self.fails(state_files.read, self.dir, b"", path=self.dir)
        self.assertEqual((f.message, f.error, f.params), (f"{self.dir}: Is a directory", "action_failed",
                                                          {"path": self.dir}))

    def test_content_lines_leave_out_comments_and_empty_lines(self):
        self.assertEqual(state_files.content_lines(self.file("l", b"# c\na\n\nb")), [b"a", b"b"])
        self.assertEqual(state_files.content_lines(self.path("none")), [])

    def test_json_any_value_or_none(self):
        self.assertEqual(state_files.read_json(self.file("j", b'[1, "\xc3\xa4"]')), [1, "ä"])
        for name, data in (("broken", b"{"), ("latin1", b'"\xe4"')):
            self.assertIsNone(state_files.read_json(self.file(name, data)), name)
        self.assertIsNone(state_files.read_json(self.path("none")))
        self.assertIsNone(state_files.read_json(self.dir))

    def test_json_object_only_a_dict_else_the_default(self):
        self.assertEqual(state_files.json_object(self.file("o", b'{"a": 1}')), {"a": 1})
        self.assertIsNone(state_files.json_object(self.file("l", b"[1]")))
        self.assertEqual(state_files.json_object(self.file("n", b"null"), {}), {})
        self.assertEqual(state_files.json_object(self.path("none"), {}), {})

    # --- writing ---

    def test_write_json_with_each_files_own_format(self):
        p = self.path("out.json")
        state_files.write_json(p, {"b": "ä", "a": 1})
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b'{\n  "b": "\\u00e4",\n  "a": 1\n}\n')
        self.assertEqual(self.mode(p), 0o644)
        state_files.write_json(p, {"b": "ä", "a": 1}, 0o600, indent=None, sort_keys=True, ensure_ascii=False)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), '{"a": 1, "b": "ä"}\n'.encode())
        self.assertEqual(self.mode(p), 0o600)

    def test_write_if_changed_compares_content_and_mode(self):
        p = self.path("unit")
        marks = []
        self.assertTrue(state_files.write_if_changed(p, b"x", before=lambda: marks.append(1)))
        self.assertEqual((self.mode(p), marks), (0o644, [1]))
        self.assertFalse(state_files.write_if_changed(p, b"x", before=lambda: marks.append(2)))
        self.assertTrue(state_files.write_if_changed(p, b"x", 0o640))
        self.assertEqual(self.mode(p), 0o640)
        self.assertTrue(state_files.write_if_changed(p, b"y", 0o640))
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"y")
        self.assertEqual(marks, [1])

    def test_write_if_changed_compares_the_group_only_when_given(self):
        p = self.file("env", b"x", 0o640)
        own = os.stat(p).st_gid
        self.assertFalse(state_files.write_if_changed(p, b"x", 0o640, own))
        with mock.patch.object(state_files, "write_atomic") as write:
            self.assertTrue(state_files.write_if_changed(p, b"x", 0o640, own + 1))
        write.assert_called_once_with(p, b"x", 0o640, own + 1)

    def test_write_if_changed_reports_or_raises_what_it_cannot_read(self):
        f = self.fails(state_files.write_if_changed, self.dir, b"x", path=self.dir)
        self.assertEqual((f.message, f.params), (f"{self.dir}: Is a directory", {"path": self.dir}))
        with self.assertRaises(IsADirectoryError):
            state_files.write_if_changed(self.dir, b"x", fail=False)

    # --- removing and listing ---

    def test_remove_says_whether_the_file_was_there(self):
        p = self.file("gone", b"")
        self.assertTrue(state_files.remove(p))
        self.assertFalse(os.path.exists(p))
        self.assertFalse(state_files.remove(p))

    def test_remove_ends_the_command_or_raises_on_other_failures(self):
        os.mkdir(self.path("d"))
        f = self.fails(state_files.remove, self.path("d"))
        self.assertEqual((f.error, f.params), ("action_failed", {}))
        self.assertTrue(f.message.startswith(self.path("d") + ": "), f.message)
        with self.assertRaises(OSError):
            state_files.remove(self.path("d"), fail=False)

    def test_listdir_sorted_or_empty_when_missing(self):
        for n in ("b", "a", "c"):
            self.file(n, b"")
        self.assertEqual(state_files.listdir(self.dir), ["a", "b", "c"])
        self.assertEqual(state_files.listdir(self.path("none")), [])
        f = self.fails(state_files.listdir, self.path("a"), vm="work")
        self.assertEqual((f.message, f.params), (f"{self.path('a')}: Not a directory", {"vm": "work"}))
        with self.assertRaises(NotADirectoryError):
            state_files.listdir(self.path("a"), fail=False)

    # --- directories and owners ---

    def test_directory_with_parents_and_its_mode_whatever_the_umask(self):
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        p = self.path("a", "b")
        self.assertTrue(state_files.directory(p, 0o755))
        self.assertEqual(self.mode(p), 0o755)
        self.assertFalse(state_files.directory(p, 0o711))
        self.assertEqual(self.mode(p), 0o711)

    def test_directory_only_new_leaves_an_existing_one_alone(self):
        p = self.path("kept")
        os.mkdir(p, 0o750)
        os.chmod(p, 0o750)
        self.assertFalse(state_files.directory(p, 0o700, only_new=True))
        self.assertEqual(self.mode(p), 0o750)
        self.assertTrue(state_files.directory(self.path("new"), 0o700, only_new=True))
        self.assertEqual(self.mode(self.path("new")), 0o700)

    def test_directory_owner_root_and_the_group_when_given(self):
        p = self.path("secrets")
        with mock.patch.object(os, "chown") as chown:
            state_files.directory(p, 0o750)
            chown.assert_not_called()
            state_files.directory(p, 0o750, 107)
        chown.assert_called_once_with(p, 0, 107)

    def test_directory_failures_are_raised(self):
        self.file("file", b"")
        with self.assertRaises(FileExistsError):
            state_files.directory(self.path("file"), 0o700)

    def test_ids_only_as_root(self):
        with mock.patch.object(os, "geteuid", return_value=1000):
            self.assertEqual(state_files.user_ids("qemu", "is libvirt installed?"), (-1, -1))
            self.assertEqual(state_files.group_id("agentbox-web", "the package creates it"), -1)
        user = mock.Mock(pw_uid=107, pw_gid=108)
        with mock.patch.object(os, "geteuid", return_value=0), \
                mock.patch("pwd.getpwnam", return_value=user), mock.patch("grp.getgrnam", return_value=mock.Mock(gr_gid=990)):
            self.assertEqual(state_files.user_ids("qemu", "is libvirt installed?"), (107, 108))
            self.assertEqual(state_files.group_id("agentbox-web", "the package creates it"), 990)

    def test_a_missing_user_or_group_ends_the_command(self):
        with mock.patch.object(os, "geteuid", return_value=0), \
                mock.patch("pwd.getpwnam", side_effect=KeyError("qemu")), \
                mock.patch("grp.getgrnam", side_effect=KeyError("agentbox-web")):
            f = self.fails(state_files.user_ids, "qemu", "is libvirt installed?")
            self.assertEqual((f.message, f.error, f.params),
                             ("user qemu is missing (is libvirt installed?)", "action_failed", {"user": "qemu"}))
            f = self.fails(state_files.group_id, "agentbox-web", "the package creates it")
            self.assertEqual((f.message, f.error, f.params),
                             ("the group agentbox-web is missing (the package creates it)", "action_failed",
                              {"group": "agentbox-web"}))


class DiskInfoTest(unittest.TestCase):
    def answer(self, stdout):
        return subprocess.CompletedProcess([], 0, stdout, "")

    def test_the_json_object_of_qemu_img_info(self):
        with mock.patch.object(programs, "run", return_value=self.answer('{"virtual-size": 1024}')) as run:
            self.assertEqual(programs.disk_info("/d.qcow2"), {"virtual-size": 1024})
        run.assert_called_once_with(["qemu-img", "info", "-U", "--output=json", "/d.qcow2"], 30)

    def test_none_when_qemu_img_cannot_tell(self):
        for stdout in ("", "not json", "[1]"):
            with mock.patch.object(programs, "run", return_value=self.answer(stdout)):
                self.assertIsNone(programs.disk_info("/d.qcow2"), stdout)
        with mock.patch.object(programs, "run", side_effect=Fail("qemu-img is missing", 127, error="action_failed")):
            self.assertIsNone(programs.disk_info("/d.qcow2"))


if __name__ == "__main__":
    unittest.main()
