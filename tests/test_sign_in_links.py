"""Sign-in links of the Control Room (#137): `agentbox sign-in-link` and the backend taking one.

The CLI runs for real with AGENTBOX_ROOT pointing at a temporary root: the Host's groups
(/etc/group), the Host setup and the directory of the links are under it, Activity goes to a
stand-in for journald's socket (fake_journald.py). The Control Room tests run the real
backend (test_control_room.ControlRoomTest) on the same kind of root and get their links from
the real CLI.

Run from the repo root: python3 -m unittest discover -s tests
"""
import calendar
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_journald import Journald  # noqa: E402
import test_control_room  # noqa: E402
from test_control_room import FIREFOX_LINUX, PASSWORD, SAFARI_IPHONE, ControlRoomTest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
LINKS = "run/agentbox-sign-in-links"
URL = re.compile(r"https://127\.0\.0\.1:8800/sign-in#link=([A-Za-z0-9_-]{43})")

sys.path.insert(0, REPO)
from agentbox import activity, cli, paths, sign_in_links  # noqa: E402

setUpModule = test_control_room.setUpModule
tearDownModule = test_control_room.tearDownModule


def sha256(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Root:
    """A Host under a temporary root: the group libvirt, the links' directory, a Host setup."""

    def make_root(self, root):
        self.root = root
        os.makedirs(self.path(LINKS))
        os.makedirs(self.path("etc/agentbox"), exist_ok=True)
        self.member(True)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def member(self, yes):
        """This user in the group libvirt (its gid among this login's groups), or not."""
        gid = os.getegid() if yes else max(os.getgroups() + [os.getegid()]) + 4711
        with open(self.path("etc/group"), "w") as f:
            f.write(f"wheel:x:10:\nlibvirt:x:{gid}:\n")

    def host_setup(self, record):
        with open(self.path("etc/agentbox/host.json"), "w") as f:
            json.dump(record, f)

    def links(self):
        return sorted(os.listdir(self.path(LINKS)))

    def link_file(self, token):
        with open(os.path.join(self.path(LINKS), sha256(token))) as f:
            return f.read()

    def run_cli(self, *args, **env):
        r = subprocess.run([AGENTBOX, *args], capture_output=True, text=True, timeout=60,
                           env={"PATH": os.environ["PATH"], "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8",
                                "TZ": "UTC", **env})
        return r.returncode, r.stdout, r.stderr

    def new_link(self):
        code, out, err = self.run_cli("sign-in-link", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)["url"].partition("#link=")[2]


class CommandTest(Root, unittest.TestCase):
    """agentbox sign-in-link: who may run it, the link it prints, what the Host keeps of it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.make_root(os.path.join(os.path.realpath(tmp.name), "root"))
        self.journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.journald.close)

    def test_a_member_of_libvirt_gets_a_link_with_the_token_in_the_fragment(self):
        code, out, err = self.run_cli("sign-in-link")
        self.assertEqual(code, 0, err)
        # only the link on stdout, so `xdg-open "$(agentbox sign-in-link)"` works
        [line] = out.splitlines()
        token = URL.fullmatch(line).group(1)
        self.assertNotIn("?", line)
        self.assertRegex(err, r"^agentbox: valid once, for 2 minutes \(until \d\d:\d\d:\d\d\)")
        self.assertEqual(self.links(), [sha256(token)])

    def test_json(self):
        before = time.time()
        code, out, err = self.run_cli("sign-in-link", "--json")
        self.assertEqual((code, err), (0, ""))
        answer = json.loads(out)
        self.assertEqual(set(answer), {"url", "expires", "mode"})
        self.assertRegex(answer["url"], URL)
        self.assertEqual(answer["mode"], "local")
        expires = calendar.timegm(time.strptime(answer["expires"], "%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(int(before) + 120 <= expires <= time.time() + 120, answer)

    def test_network_mode_names_the_bind_address_and_port(self):
        self.host_setup({"mode": "network", "bind": "192.0.2.8", "home_network": "192.0.2.0/24",
                         "web_port": 8443})
        code, out, err = self.run_cli("sign-in-link", "--json")
        self.assertEqual(code, 0, err)
        answer = json.loads(out)
        self.assertRegex(answer["url"], r"^https://192\.0\.2\.8:8443/sign-in#link=[A-Za-z0-9_-]{43}$")
        self.assertEqual(answer["mode"], "network")

    def test_the_host_keeps_only_the_hash_and_the_expiry(self):
        before = int(time.time())
        token = self.new_link()
        [name] = self.links()
        self.assertEqual(name, sha256(token))
        stored = json.loads(self.link_file(token))
        self.assertEqual(set(stored), {"expires"})
        self.assertTrue(before + 120 <= stored["expires"] <= time.time() + 120, stored)
        self.assertNotIn(token, self.link_file(token))
        mode = os.stat(os.path.join(self.path(LINKS), name)).st_mode & 0o777
        self.assertEqual(mode, 0o644)       # for the Control Room; the directory keeps others out

    def test_every_link_is_new(self):
        tokens = {self.new_link() for _ in range(3)}
        self.assertEqual(len(tokens), 3)
        self.assertEqual(self.links(), sorted(sha256(t) for t in tokens))

    @unittest.skipIf(os.geteuid() == 0, "root may always")
    def test_who_is_not_in_libvirt_is_refused_and_nothing_is_written(self):
        self.member(False)
        code, out, err = self.run_cli("sign-in-link", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"], "sudo_failed")
        self.assertIn("root and the members of libvirt", json.loads(err)["message"])
        # no group libvirt at all
        with open(self.path("etc/group"), "w") as f:
            f.write("wheel:x:10:\n")
        self.assertEqual(self.run_cli("sign-in-link")[0], 1)
        self.assertEqual(self.links(), [])

    def test_the_control_room_cannot_make_one(self):
        code, out, err = self.run_cli("sign-in-link", "--json", SUDO_USER="agentbox-web")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"], "control_room_refused")
        self.assertFalse(cli.DECLARATIONS["sign-in-link"].control_room)
        with open(os.path.join(REPO, "rpm", "agentbox-web.sudoers")) as f:
            self.assertNotIn("sign-in-link", f.read())
        self.assertEqual(self.links(), [])

    def test_without_the_directory(self):
        os.rmdir(self.path(LINKS))
        code, out, err = self.run_cli("sign-in-link", "--json")
        self.assertEqual((code, out), (1, ""))
        said = json.loads(err)
        self.assertEqual(said["error"], "action_failed")
        self.assertIn("systemd-tmpfiles --create agentbox.conf", said["message"])

    def test_no_arguments(self):
        self.assertEqual(self.run_cli("sign-in-link", "now")[0], 2)
        self.assertEqual(self.links(), [])

    def test_activity_records_the_host_user_never_the_token(self):
        token = self.new_link()
        self.run_cli("sign-in-link", SUDO_USER="owner")
        entries = self.journald.entries()
        self.assertEqual([(e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"]) for e in entries],
                         [("credentials", "sign-in-link", "ok")] * 2)
        self.assertEqual(entries[0]["AGENTBOX_ACTOR"], pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual(entries[1]["AGENTBOX_ACTOR"], "owner")
        self.assertEqual(entries[0]["AGENTBOX_DESCRIPTION"], "Create a sign-in link for the Control Room")
        self.assertNotIn("AGENTBOX_VM", entries[0])
        raw = b"".join(self.journald.raw())
        self.assertNotIn(token.encode(), raw)
        self.assertNotIn(sha256(token).encode(), raw)

    @unittest.skipIf(os.geteuid() == 0, "root may always write into the journal")
    def test_no_link_without_its_activity_entry(self):
        # a member of libvirt not (yet) in agentbox-journal: the journal refuses the entry
        os.chmod(self.journald.path, 0)
        code, out, err = self.run_cli("sign-in-link", "--json")
        self.assertEqual((code, out), (1, ""))
        said = json.loads(err)
        self.assertEqual(said["error"], "action_failed")
        self.assertIn("agentbox-journal", said["message"])
        self.assertIn("agentbox setup", said["message"])
        self.assertEqual(self.links(), [])
        self.assertEqual(self.journald.entries(), [])
        # and without any journal
        self.journald.close()
        os.unlink(self.journald.path)
        code, out, err = self.run_cli("sign-in-link")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no link was made", err)
        self.assertEqual(self.links(), [])


class TakeTest(unittest.TestCase):
    """sign_in_links.take(), as the Control Room calls it: once, within two minutes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patcher = mock.patch.object(paths, "SIGN_IN_LINKS", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_once(self):
        token, expires = sign_in_links.create(1000)
        self.assertEqual(expires, 1120)
        self.assertTrue(sign_in_links.take(token, 1001))
        self.assertFalse(sign_in_links.take(token, 1002))
        self.assertEqual(os.listdir(self.dir), [])

    def test_for_two_minutes(self):
        token, _ = sign_in_links.create(1000)
        self.assertTrue(sign_in_links.take(token, 1119))
        token, _ = sign_in_links.create(1000)
        self.assertFalse(sign_in_links.take(token, 1120))
        self.assertEqual(os.listdir(self.dir), [])       # used up as well

    def test_of_two_uses_at_once_one_signs_in(self):
        for _ in range(20):
            token, _ = sign_in_links.create(1000)
            start, results = threading.Barrier(4), []

            def use():
                start.wait()
                results.append(sign_in_links.take(token, 1001))
            threads = [threading.Thread(target=use) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(results), [False, False, False, True])

    def test_unknown_and_malformed_tokens(self):
        token, _ = sign_in_links.create(1000)
        for other in ("x" * 43, token[:-1], token + "A", "../" + token, "", None, 43):
            with self.subTest(other=other):
                self.assertFalse(sign_in_links.take(other, 1001))
        self.assertTrue(sign_in_links.take(token, 1001))

    def test_expired_links_are_removed_when_another_is_taken(self):
        old, _ = sign_in_links.create(1000)
        new, _ = sign_in_links.create(1100)
        self.assertFalse(sign_in_links.take("x" * 43, 1150))
        self.assertEqual(os.listdir(self.dir), [sha256(new)])
        self.assertFalse(sign_in_links.take(old, 1150))

    def test_a_file_just_being_written_stays(self):
        token, _ = sign_in_links.create(time.time())
        empty = os.path.join(self.dir, "0" * 64)
        open(empty, "w").close()
        sign_in_links.sweep(time.time())
        self.assertIn("0" * 64, os.listdir(self.dir))
        sign_in_links.sweep(time.time() + 121)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertFalse(sign_in_links.take(token, time.time()))


class ControlRoomLinkTest(Root, ControlRoomTest):
    """POST /api/v1/session with {"link": <token>}: an ordinary session, once, within two minutes;
    refused like a wrong password, also by the brake; Activity with address and device."""

    def setUp(self):
        super().setUp()
        self.make_root(self.root)
        # the CLI's Activity (a link is made only when its entry is recorded)
        self.cli_journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.cli_journald.close)

    def sign_in(self, link):
        return self.request("POST", "/api/v1/session", {"link": link})

    def entries(self):
        return [(e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"]) for e in self.journald.entries()]

    def assert_never_logged(self, *tokens):
        raw = b"".join(self.journald.raw())
        for token in tokens:
            self.assertNotIn(token.encode(), raw)
            self.assertNotIn(token, self.server_log())

    def test_a_link_signs_in_once_with_an_ordinary_session(self):
        with open(self.settings, "w") as f:
            f.write('{"session_hours": 3}')
        token = self.new_link()
        status, obj, resp = self.sign_in(token)
        self.assertEqual(status, 200, obj)
        self.assertEqual(set(obj), {"authenticated", "expires", "default_language"})
        cookie = resp.getheader("Set-Cookie")
        self.assertRegex(cookie, r"^agentbox=[A-Za-z0-9_-]{43}; Max-Age=10800; Path=/; Secure; HttpOnly; "
                                 r"SameSite=Strict$")
        self.assertNotIn(token, cookie)
        self.assertEqual(self.links(), [])
        self.cookie = cookie.split(";")[0]
        self.assertEqual(self.request("GET", "/api/v1/session")[0], 200)
        # used: refused, in another browser too
        self.cookie = None
        self.assertError(self.sign_in(token), 401, "sign_in_link_refused")
        self.assertEqual(self.entries(), [("signin.link", "ok"), ("signin.link-refused", "failed")])
        self.assert_never_logged(token)

    def test_an_expired_link_is_refused_and_removed(self):
        token = self.new_link()
        path = os.path.join(self.path(LINKS), sha256(token))
        with open(path, "w") as f:
            json.dump({"expires": int(time.time()) - 1}, f)
        answer = self.sign_in(token)
        self.assertError(answer, 401, "sign_in_link_refused")
        self.assertIsNone(answer[2].getheader("Set-Cookie"))
        self.assertEqual(self.links(), [])

    def test_unknown_and_malformed_tokens_are_refused(self):
        for token in ("A" * 43, "short"):
            with self.subTest(token=token):
                started = time.monotonic()
                self.assertError(self.sign_in(token), 401, "sign_in_link_refused")
                self.assertGreaterEqual(time.monotonic() - started, 1)     # as slow as a wrong password
        self.assertEqual(self.entries(), [("signin.link-refused", "failed")] * 2)

    def test_refused_links_count_towards_the_brake(self):
        self.sign_in("A" * 43)
        self.assertError(self.request("POST", "/api/v1/session", {"password": "guess-4711"}), 401,
                         "wrong_password")
        for i in range(3):
            self.sign_in(f"{i}" * 43)
        token = self.new_link()
        answer = self.sign_in(token)
        self.assertError(answer, 429, "too_many_attempts")
        self.assertTrue(0 < int(answer[2].getheader("Retry-After")) <= 900)
        self.assertError(self.request("POST", "/api/v1/session", {"password": PASSWORD}), 429, "too_many_attempts")
        # the brake took no link
        self.assertEqual(self.links(), [sha256(token)])
        self.assertEqual(self.entries()[-2:], [("signin.locked", "failed")] * 2)

    def test_activity_names_device_and_address_never_the_token(self):
        self.user_agent = FIREFOX_LINUX
        token = self.new_link()
        self.assertEqual(self.sign_in(token)[0], 200)
        self.user_agent = SAFARI_IPHONE
        self.sign_in(token)
        ok, refused = self.journald.entries()
        self.assertEqual({k: v for k, v in ok.items() if k != "MESSAGE"}, {
            "PRIORITY": "5", "SYSLOG_IDENTIFIER": "agentbox-activity", "AGENTBOX_KIND": "signin",
            "AGENTBOX_ACTION": "signin.link", "AGENTBOX_CODE": "signin.link", "AGENTBOX_OUTCOME": "ok",
            "AGENTBOX_ACTOR": "Firefox/Linux (127.0.0.1)", "AGENTBOX_DESCRIPTION": "Signed in with a sign-in link",
            "AGENTBOX_ADDRESS": "127.0.0.1", "AGENTBOX_DEVICE": "Firefox/Linux"})
        self.assertEqual((refused["AGENTBOX_ACTION"], refused["AGENTBOX_OUTCOME"], refused["AGENTBOX_DEVICE"],
                          refused["AGENTBOX_ADDRESS"], refused["PRIORITY"]),
                         ("signin.link-refused", "failed", "Safari/iPhone", "127.0.0.1", "4"))
        self.assert_never_logged(token, sha256(token))

    def test_one_of_password_and_link(self):
        token = self.new_link()
        self.assertError(self.request("POST", "/api/v1/session", {"password": PASSWORD, "link": token}), 400,
                         "invalid_field")
        self.assertError(self.sign_in(5), 400, "invalid_field")
        self.assertError(self.sign_in(None), 400, "invalid_field")      # {"link": null}: no password either
        self.assertEqual(self.links(), [sha256(token)])
        self.assertEqual(self.journald.entries(), [])
        self.assertNotIn(token, self.server_log())

    def test_the_backend_and_the_readers_of_activity_know_the_same_sign_ins(self):
        from test_control_room_v1 import server_module
        self.assertEqual(set(server_module().SIGN_INS), activity.SIGN_INS)


if __name__ == "__main__":
    unittest.main()
