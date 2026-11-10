"""The GitHub Apps (#18, several since askos#25): `agentbox set-github-app` (add an App or
replace its key after GitHub accepted them), `remove-github-app`, `github-app` (each App's
installations, the VMs relying on them, owners without one) and `github-tokens` (what a VM
got from the token service, and what it was refused).

The real CLI runs with AGENTBOX_ROOT pointing at a temporary root. A fake curl stands in
for GitHub (fake_github_curl.py): it checks every JWT with the real openssl against the
public key of the App, as GitHub does. Keys are generated for the tests; Activity goes to
a stand-in for journald's socket under the root, the token service's log comes from a
fake journalctl.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from fake_journald import Journald  # noqa: E402
from journal_writers import write_passwd, writer  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(REPO, "bin", "agentbox")
TESTS = os.path.join(REPO, "tests")
APP_ID = "12345"
KEY_FILE = "app-12345.pem"
APP = {"id": 12345, "slug": "agentbox-test", "name": "Agent Box Test", "owner": "likt0r",
       "html_url": "https://github.com/apps/agentbox-test",
       "install_url": "https://github.com/apps/agentbox-test/installations/new"}

keys = None     # TemporaryDirectory with the keys of setUpModule


def openssl(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


def setUpModule():
    """The App's key as GitHub hands it out (PKCS#1), a second RSA key, an EC key and
    the App's key with a passphrase."""
    global keys, KEY, OTHER, EC, ENCRYPTED
    keys = tempfile.TemporaryDirectory()
    KEY = openssl("pkey", "-traditional", data=openssl("genpkey", "-algorithm", "RSA",
                                                         "-pkeyopt", "rsa_keygen_bits:2048"))
    OTHER = openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048")
    EC = openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256")
    ENCRYPTED = openssl("pkey", "-aes256", "-passout", "pass:geheim", data=KEY)
    for name, key in (("key", KEY), ("other", OTHER)):
        with open(os.path.join(keys.name, f"{name}.pub"), "wb") as f:
            f.write(public(key))


def tearDownModule():
    keys.cleanup()


def public(pem):
    return openssl("pkey", "-pubout", data=pem)


def key_lines(pem):
    """The base64 lines of a key: none of them may show up anywhere."""
    return [l.strip() for l in pem.decode("utf-8", "replace").splitlines()
            if len(l.strip()) >= 16 and not l.startswith("-----")]


def installation(id, login, kind="Organization", selection="all", suspended=False):
    return {"id": id, "account": {"login": login, "type": kind}, "repository_selection": selection,
            "suspended_at": "2026-10-01T00:00:00Z" if suspended else None,
            "html_url": f"https://github.com/organizations/{login}/settings/installations/{id}"}


def fake(path, program):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(TESTS, program)}" "$@"\n')
    os.chmod(path, 0o755)


class GitHubTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.root = os.path.join(self.dir, "root")
        self.github_dir = self.path("etc/agentbox/github")
        os.makedirs(self.github_dir)
        os.chmod(self.github_dir, 0o750)
        vm_records.write(self.root)
        write_passwd(self.root)
        fake(os.path.join(self.dir, "bin", "curl"), "fake_github_curl.py")
        fake(os.path.join(self.dir, "bin", "journalctl"), "fake_journalctl.py")
        self.journald = Journald(self.path("run/systemd/journal.agentbox/socket"))
        self.addCleanup(self.journald.close)
        # the working App: the stored ID and key are the ones GitHub knows
        self.store((APP_ID, KEY))
        self.github()

    def path(self, rel):
        return os.path.join(self.root, rel)

    def store(self, *apps, conf=None):
        """These Apps, (App ID, key) each, and only these; app.conf as given or naming them."""
        for name in os.listdir(self.github_dir):
            os.chmod(os.path.join(self.github_dir, name), 0o600)
            os.remove(os.path.join(self.github_dir, name))
        conf = conf or "".join(f"APP_ID={app_id}\n" for app_id, _ in apps).encode()
        for name, data, mode in (("app.conf", conf, 0o640),
                                 *((f"app-{app_id}.pem", key, 0o400) for app_id, key in apps)):
            f = os.path.join(self.github_dir, name)
            if os.path.exists(f):
                os.chmod(f, 0o600)
            with open(f, "wb") as fh:
                fh.write(data)
            os.chmod(f, mode)

    def stored(self, name):
        with open(os.path.join(self.github_dir, name), "rb") as f:
            return f.read()

    def mode(self, name):
        return stat.S_IMODE(os.stat(os.path.join(self.github_dir, name)).st_mode)

    def github(self, app_id=12345, key="key", **scenario):
        with open(os.path.join(self.dir, "github.json"), "w") as f:
            json.dump({"app_id": app_id, "public_key": os.path.join(keys.name, f"{key}.pub"), **scenario}, f)

    def run_agentbox(self, *args, stdin=b""):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_DIR": self.dir,
               "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], input=stdin if isinstance(stdin, bytes) else stdin.encode(),
                           capture_output=True, env=env, timeout=60)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def calls(self):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def jwts(self):
        """Every JWT agentbox sent to GitHub."""
        return [l.split(" ", 2)[2] for c in self.calls() if c["cmd"] == "curl"
                for l in c["stdin"].splitlines() if l.startswith("Authorization: Bearer ")]

    def assert_secret_free(self, *texts, key=None):
        """Neither the key nor a JWT is in these texts, the journal or any program's arguments."""
        everywhere = "\n".join([*texts, b"".join(self.journald.raw()).decode("utf-8", "replace"),
                                *(json.dumps(c["argv"]) for c in self.calls())])
        for secret in [*(key_lines(key) if key else []), *self.jwts()]:
            self.assertNotIn(secret, everywhere)
        self.assertNotIn("PRIVATE KEY", everywhere)


class SetGitHubAppTest(GitHubTest):
    def replace(self, *args, key=None):
        return self.run_agentbox("set-github-app", *args, stdin=key if key is not None else OTHER)

    def assert_unchanged(self):
        self.assertEqual(self.stored(KEY_FILE), KEY)
        self.assertEqual(self.stored("app.conf"), f"APP_ID={APP_ID}\n".encode())
        self.assertEqual((self.mode(KEY_FILE), self.mode("app.conf")), (0o400, 0o640))
        self.assertEqual(sorted(os.listdir(self.github_dir)), [KEY_FILE, "app.conf"])

    def test_rotated_key_replaces_the_old_one(self):
        # a new key generated on GitHub for the same App: GitHub knows only that one now
        self.github(key="other")
        pasted = OTHER.replace(b"\n", b"\r\n")         # as a browser sends it
        code, out, err = self.replace(APP_ID, "--no-prompt", key=pasted)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "GitHub App 12345 (agentbox-test) checked and saved. "
                              "The token service uses it from the next request on.\n")
        self.assertEqual(public(self.stored(KEY_FILE)), public(OTHER))
        self.assertTrue(self.stored(KEY_FILE).startswith(b"-----BEGIN PRIVATE KEY-----\n"))
        self.assertEqual(self.stored("app.conf"), b"APP_ID=12345\n")
        self.assertEqual((self.mode(KEY_FILE), self.mode("app.conf")), (0o400, 0o640))
        self.assertEqual(sorted(os.listdir(self.github_dir)), [KEY_FILE, "app.conf"])

    def test_asks_github_for_the_app_with_a_jwt_signed_by_the_new_key(self):
        self.github(key="other")
        self.replace(APP_ID, "--no-prompt")
        [call] = self.calls()
        self.assertEqual(call["argv"][-1], "https://api.github.com/app")
        self.assertIn("--proto", call["argv"])
        [jwt] = self.jwts()
        head, claims, _ = jwt.split(".")
        claims = json.loads(__import__("base64").urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        self.assertEqual(claims["iss"], APP_ID)
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        self.assertLess(claims["iat"], time.time())
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", call["stdin"])

    def test_another_app_is_added_next_to_the_stored_one(self):
        """A different App ID with its key; app.conf names both, ascending, other lines stay."""
        self.store((APP_ID, KEY), conf=b"APP_ID=12345\n# Kommentar\nOTHER=1\n")
        self.github(app_id=777, key="other", app={"id": 777, "slug": "other-app", "name": "Other",
                                                   "owner": {"login": "acme-corp"}})
        code, out, err = self.replace("777", "--no-prompt", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"id": 777, "slug": "other-app", "name": "Other", "owner": "acme-corp",
                                           "html_url": "https://github.com/apps/other-app",
                                           "install_url": "https://github.com/apps/other-app/installations/new",
                                           "stored": True, "added": True})
        self.assertEqual(self.stored("app.conf"), b"APP_ID=777\nAPP_ID=12345\n# Kommentar\nOTHER=1\n")
        self.assertEqual(public(self.stored("app-777.pem")), public(OTHER))
        self.assertEqual(self.stored(KEY_FILE), KEY)
        self.assertEqual((self.mode("app-777.pem"), self.mode("app.conf")), (0o400, 0o640))
        self.assertEqual(sorted(os.listdir(self.github_dir)), ["app-12345.pem", "app-777.pem", "app.conf"])
        self.assertEqual(self.replace("777", "--no-prompt")[1],
                         "GitHub App 777 (other-app) checked and saved. The token service uses it from the next "
                         "request on.\n")

    def test_replacing_one_apps_key_leaves_the_other(self):
        self.store(("777", EC), (APP_ID, KEY))
        self.github(key="other")
        code, out, err = self.replace(APP_ID, "--no-prompt", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual({k: json.loads(out)[k] for k in ("id", "stored", "added")},
                         {"id": 12345, "stored": True, "added": False})
        self.assertEqual(public(self.stored(KEY_FILE)), public(OTHER))
        self.assertEqual(self.stored("app-777.pem"), EC)
        self.assertEqual(self.stored("app.conf"), b"APP_ID=777\nAPP_ID=12345\n")

    def test_key_github_does_not_know_is_refused_and_the_old_one_stays(self):
        code, out, err = self.replace(APP_ID, "--no-prompt")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "agentbox: GitHub rejects the App ID or key (HTTP 401: A JSON web token could "
                              "not be decoded); nothing changed\n")
        self.assertEqual(len(self.calls()), 1)
        self.assert_unchanged()

    def test_wrong_app_id_is_refused(self):
        code, _, err = self.replace("54321", "--no-prompt", key=KEY)
        self.assertEqual(code, 1)
        self.assertEqual(err, "agentbox: GitHub rejects the App ID or key (HTTP 401: Integration not found); "
                              "nothing changed\n")
        self.assert_unchanged()

    def test_github_naming_another_app_is_refused(self):
        self.github(key="other", app={"id": 999, "slug": "x"})
        code, _, err = self.replace(APP_ID, "--no-prompt")
        self.assertEqual((code, err), (1, "agentbox: GitHub names another App for this key; "
                                          "nothing changed\n"))
        self.assert_unchanged()

    def test_github_unreachable_or_odd(self):
        for scenario, message in (({"exit": 7}, "GitHub is not reachable; nothing changed"),
                                  ({"status": 502, "answer": "<html>Bad Gateway</html>"},
                                   "GitHub rejects the App ID or key (HTTP 502: no usable answer); "
                                   "nothing changed"),
                                  ({"status": 200, "answer": "[]"},
                                   "GitHub rejects the App ID or key (HTTP 200: no usable answer); "
                                   "nothing changed")):
            with self.subTest(scenario=scenario):
                self.github(key="other", **scenario)
                self.assertEqual(self.replace(APP_ID, "--no-prompt"), (1, "", f"agentbox: {message}\n"))
                self.assert_unchanged()

    def test_what_is_no_usable_key_never_reaches_github(self):
        cert = openssl("req", "-x509", "-key", "/dev/stdin", "-subj", "/CN=t", "-days", "1", data=OTHER)
        no_pem = "agentbox: this is not a private key in PEM format (the GitHub App's .pem file)\n"
        for name, pasted, message in (
                ("empty", b"", no_pem), ("blank", b" \n\n", no_pem), ("text", b"geheimes Passwort\n", no_pem),
                ("half a key", KEY[:len(KEY) // 2], no_pem), ("public key", public(KEY), no_pem),
                ("certificate", cert, no_pem),
                ("EC key", EC, "agentbox: this is not an RSA key; GitHub Apps sign with RSA\n"),
                ("encrypted", ENCRYPTED, "agentbox: the key is encrypted; it takes the .pem file from GitHub as "
                                         "it is\n"),
                ("too long", KEY * 12, "agentbox: too long for a private key\n")):
            with self.subTest(name=name):
                code, out, err = self.replace(APP_ID, "--no-prompt", key=pasted)
                self.assertEqual((code, out, err), (1, "", message))
                self.assert_secret_free(out, err, key=pasted)
        self.assertEqual(self.calls(), [])
        self.assert_unchanged()

    def test_invalid_app_id(self):
        for bad in ("abc", "0123", "0", "1" * 13, "12 34", "-1", "١٢٣"):
            with self.subTest(bad=bad):
                code, out, err = self.replace(bad, "--no-prompt")
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, "agentbox: invalid App ID: a number, as GitHub shows it under App ID in "
                                      "the App's settings\n")
        self.assertEqual(self.calls(), [])
        self.assert_unchanged()

    def test_key_passed_as_argument_by_mistake_is_not_echoed(self):
        usage = "usage: agentbox set-github-app <app-id> [--no-prompt] [--json], the key on stdin"
        bad_id = "invalid App ID: a number, as GitHub shows it under App ID in the App's settings"
        for args, message in (((APP_ID, OTHER.decode()), usage), ((OTHER.decode(),), bad_id), ((), usage)):
            with self.subTest(args=len(args)):
                code, out, err = self.run_agentbox("set-github-app", *args, "--no-prompt", stdin=OTHER)
                self.assertEqual((code, out, err), (2, "", f"agentbox: {message}\n"))
        entries = self.journald.entries()
        self.assertEqual([e["AGENTBOX_DESCRIPTION"] for e in entries],
                         ["Add or replace the GitHub App (App ID 12345): " + usage,
                          "Add or replace the GitHub App: " + bad_id, "Add or replace the GitHub App: " + usage])
        # the App ID only when it is one, as a parameter too
        self.assertEqual([e.get("AGENTBOX_PARAM_APP_ID") for e in entries], ["12345", None, None])
        self.assert_secret_free(key=OTHER)
        self.assertEqual(self.calls(), [])
        self.assert_unchanged()

    def test_prompt(self):
        self.github(key="other")
        prompt = "Paste the GitHub App's private key (.pem), then press Enter and Ctrl+D:\n"
        code, out, err = self.replace(APP_ID)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(prompt), out)
        self.store((APP_ID, KEY))
        code, out, err = self.replace(APP_ID, "--json")
        self.assertEqual((code, err), (0, prompt))
        self.assertTrue(json.loads(out)["stored"])

    def test_needs_the_token_service_installed(self):
        self.store()
        os.remove(os.path.join(self.github_dir, "app.conf"))
        os.rmdir(self.github_dir)
        code, out, err = self.replace(APP_ID, "--no-prompt")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, f"agentbox: {self.github_dir} is missing (the package creates it)\n")
        self.assertEqual(self.calls(), [])

    def test_recorded_in_activity_as_credentials(self):
        self.github(key="other")
        self.assertEqual(self.run_agentbox("--actor=Firefox/Linux (192.0.2.23)", "set-github-app", APP_ID,
                                           "--no-prompt", stdin=OTHER)[0], 0)
        self.github()
        self.assertEqual(self.replace(APP_ID, "--no-prompt", key=EC)[0], 1)
        self.assertEqual([(e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"])
                          for e in self.journald.entries()], [
            ("credentials", "set-github-app", "ok", "Add or replace the GitHub App (App ID 12345)"),
            ("credentials", "set-github-app", "failed",
             "Add or replace the GitHub App (App ID 12345): this is not an RSA key; GitHub Apps sign with RSA")])
        self.assertNotIn("AGENTBOX_VM", self.journald.entries()[0])

    def test_neither_key_nor_jwt_ever_shows(self):
        for scenario, extra in (({"key": "other"}, ()), ({"key": "other"}, ("--json",)), ({}, ()),
                                ({"key": "other", "status": 401, "answer": {"message": "bad"}}, ()),
                                ({"key": "other", "exit": 28}, ())):
            with self.subTest(scenario=scenario, extra=extra):
                self.store((APP_ID, KEY))
                self.github(**scenario)
                code, out, err = self.replace(APP_ID, "--no-prompt", *extra)
                self.assert_secret_free(out, err, key=OTHER)
        self.assertTrue(self.jwts())


class RemoveGitHubAppTest(GitHubTest):
    def setUp(self):
        super().setUp()
        self.store(("777", OTHER), (APP_ID, KEY))

    def test_removes_only_that_app(self):
        code, out, err = self.run_agentbox("remove-github-app", "777")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "GitHub App 777 removed; the token service no longer uses it. "
                              "Delete its key on GitHub as well.\n")
        self.assertEqual(self.stored("app.conf"), b"APP_ID=12345\n")
        self.assertEqual(sorted(os.listdir(self.github_dir)), [KEY_FILE, "app.conf"])
        self.assertEqual((self.mode(KEY_FILE), self.mode("app.conf")), (0o400, 0o640))
        self.assertEqual(self.run_agentbox("remove-github-app", APP_ID, "--json"),
                         (0, '{"id": 12345, "removed": true}\n', ""))
        self.assertEqual(sorted(os.listdir(self.github_dir)), ["app.conf"])
        self.assertEqual(self.stored("app.conf"), b"")
        # GitHub is not asked: removing needs no working key
        self.assertEqual(self.calls(), [])

    def test_other_lines_of_app_conf_stay(self):
        self.store(("777", OTHER), (APP_ID, KEY), conf=b"APP_ID=777\n# Kommentar\nAPP_ID=12345\n")
        self.assertEqual(self.run_agentbox("remove-github-app", "777")[0], 0)
        self.assertEqual(self.stored("app.conf"), b"APP_ID=12345\n# Kommentar\n")

    def test_a_key_app_conf_does_not_name_is_removed_too(self):
        # left behind when set-github-app ended between writing the key and app.conf
        self.store((APP_ID, KEY), conf=b"")
        self.assertEqual(self.run_agentbox("remove-github-app", APP_ID)[0], 0)
        self.assertEqual(sorted(os.listdir(self.github_dir)), ["app.conf"])

    def test_unknown_app(self):
        code, out, err = self.run_agentbox("remove-github-app", "4242", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err), {"error": "github_app_not_found", "message": "GitHub App 4242 is not set up",
                                           "params": {"app_id": 4242}})
        self.assertEqual(sorted(os.listdir(self.github_dir)), ["app-12345.pem", "app-777.pem", "app.conf"])

    def test_invalid_arguments_change_nothing(self):
        usage = "agentbox: usage: agentbox remove-github-app <app-id> [--json]\n"
        bad_id = "agentbox: invalid App ID: a number, as GitHub shows it under App ID in the App's settings\n"
        for args, exit, message in (((), 2, usage), (("777", "12345"), 2, usage), (("abc",), 2, bad_id),
                                    (("../app",), 2, bad_id), ((KEY.decode(),), 2, bad_id)):
            with self.subTest(args=args[:1]):
                self.assertEqual(self.run_agentbox("remove-github-app", *args), (exit, "", message))
        self.assertEqual(sorted(os.listdir(self.github_dir)), ["app-12345.pem", "app-777.pem", "app.conf"])
        self.assert_secret_free(key=KEY)

    def test_recorded_in_activity_as_credentials(self):
        self.assertEqual(self.run_agentbox("--actor=Firefox/Linux (192.0.2.23)", "remove-github-app", "777")[0], 0)
        self.assertEqual(self.run_agentbox("remove-github-app", "777")[0], 1)
        self.assertEqual([(e["AGENTBOX_KIND"], e["AGENTBOX_ACTION"], e["AGENTBOX_OUTCOME"], e["AGENTBOX_DESCRIPTION"],
                           e.get("AGENTBOX_PARAM_APP_ID")) for e in self.journald.entries()], [
            ("credentials", "remove-github-app", "ok", "Remove the GitHub App (App ID 777)", "777"),
            ("credentials", "remove-github-app", "failed",
             "Remove the GitHub App (App ID 777): GitHub App 777 is not set up", "777")])


VMS = [vm_records.record("work", 1, github_owners=["Likt0r", "acme-corp", "my-org"]),
       vm_records.record("priv", 2, github_owners=["likt0r"]),
       vm_records.record("lab", 3, github_owners=["my-org", "solo"])]
INSTALLATIONS = [installation(11, "likt0r", "User"), installation(12, "acme-corp", selection="selected",
                                                                  suspended=True),
                 installation(13, "unused-org")]


class InstallationsTest(GitHubTest):
    def setUp(self):
        super().setUp()
        vm_records.write(self.root, *VMS)
        self.github(installations=INSTALLATIONS)

    def overview(self):
        code, out, err = self.run_agentbox("github-app", "--json")
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def test_installations_with_their_vms_and_missing_owners(self):
        self.assertEqual(self.overview(), {
            "apps": [{**APP, "error": None, "installations": [
                {"id": 12, "account": "acme-corp", "type": "Organization", "repository_selection": "selected",
                 "suspended": True, "html_url": "https://github.com/organizations/acme-corp/settings/installations/12",
                 "vms": ["work"]},
                {"id": 11, "account": "likt0r", "type": "User", "repository_selection": "all", "suspended": False,
                 "html_url": "https://github.com/organizations/likt0r/settings/installations/11",
                 "vms": ["priv", "work"]},
                {"id": 13, "account": "unused-org", "type": "Organization", "repository_selection": "all",
                 "suspended": False,
                 "html_url": "https://github.com/organizations/unused-org/settings/installations/13", "vms": []}]}],
            "missing": [{"owner": "my-org", "vms": ["lab", "work"]}, {"owner": "solo", "vms": ["lab"]}]})

    def two_apps(self, second=None):
        """The working App and a second one (App ID 777, key OTHER) installed on my-org."""
        self.store(("777", OTHER), (APP_ID, KEY))
        self.github(apps=[{"app_id": 12345, "public_key": os.path.join(keys.name, "key.pub"),
                           "installations": INSTALLATIONS},
                          {"app_id": 777, "public_key": os.path.join(keys.name, "other.pub"),
                           "app": {"id": 777, "slug": "org-app", "name": "Org App", "owner": {"login": "my-org"}},
                           "installations": [installation(21, "my-org"), installation(22, "likt0r", "User")],
                           **(second or {})}])

    def test_every_app_ascending_and_owners_no_app_is_installed_on(self):
        self.two_apps()
        result = self.overview()
        self.assertEqual([(a["id"], a["slug"], a["install_url"], [(i["account"], i["vms"]) for i in a["installations"]])
                          for a in result["apps"]], [
            (777, "org-app", "https://github.com/apps/org-app/installations/new",
             [("likt0r", ["priv", "work"]), ("my-org", ["lab", "work"])]),
            (12345, "agentbox-test", "https://github.com/apps/agentbox-test/installations/new",
             [("acme-corp", ["work"]), ("likt0r", ["priv", "work"]), ("unused-org", [])])])
        # my-org has the second App: only solo is missing
        self.assertEqual(result["missing"], [{"owner": "solo", "vms": ["lab"]}])
        code, out, _ = self.run_agentbox("github-app")
        self.assertEqual(out.splitlines()[:5], [
            "GitHub App 777 org-app (Org App), owned by my-org",
            "Install link: https://github.com/apps/org-app/installations/new",
            "Installed on:",
            "  likt0r               User          all repos       VMs: priv, work",
            "  my-org               Organization  all repos       VMs: lab, work"])
        self.assertEqual(out.splitlines()[5], "GitHub App 12345 agentbox-test (Agent Box Test), owned by likt0r")
        self.assert_secret_free(key=OTHER)

    def test_an_app_github_refuses_is_shown_with_its_error_next_to_the_others(self):
        self.two_apps({"status": 401, "answer": {"message": "Bad credentials"}})
        result = self.overview()
        self.assertEqual(result["apps"][0], {
            "id": 777, "slug": None, "name": None, "owner": None, "html_url": None, "install_url": None,
            "installations": [],
            "error": {"error": "github_unavailable", "message": "GitHub answers /app with HTTP 401: Bad credentials",
                      "params": {"path": "/app", "status": 401}}})
        self.assertEqual(result["apps"][1]["slug"], "agentbox-test")
        self.assertEqual(result["missing"], [{"owner": "my-org", "vms": ["lab", "work"]},
                                             {"owner": "solo", "vms": ["lab"]}])
        self.assertEqual(self.run_agentbox("github-app")[1].splitlines()[0],
                         "GitHub App 777: GitHub answers /app with HTTP 401: Bad credentials")

    def test_an_app_without_its_key_is_shown_with_its_error(self):
        self.two_apps()
        os.chmod(os.path.join(self.github_dir, "app-777.pem"), 0o600)
        os.remove(os.path.join(self.github_dir, "app-777.pem"))
        app = self.overview()["apps"][0]
        self.assertEqual((app["id"], app["error"]["error"]), (777, "invalid_private_key"))

    def test_asks_github_with_the_stored_key(self):
        self.overview()
        self.assertEqual([c["argv"][-1] for c in self.calls()],
                         ["https://api.github.com/app", "https://api.github.com/app/installations?per_page=100&page=1"])
        self.assert_secret_free(key=KEY)

    def test_pages_through_many_installations(self):
        many = [installation(100 + i, f"org-{i:03d}") for i in range(150)]
        self.github(installations=many)
        result = self.overview()
        self.assertEqual(len(result["apps"][0]["installations"]), 150)
        self.assertEqual([c["argv"][-1].rsplit("&", 1)[1] for c in self.calls()[1:]], ["page=1", "page=2"])
        self.assertEqual({m["owner"] for m in result["missing"]}, {"likt0r", "acme-corp", "my-org", "solo"})

    def test_installed_nowhere(self):
        self.github(installations=[])
        result = self.overview()
        self.assertEqual(result["apps"][0]["installations"], [])
        # owners compare without case, as on GitHub; shown as the first VM by name writes it
        self.assertEqual(result["missing"], [{"owner": "acme-corp", "vms": ["work"]},
                                             {"owner": "likt0r", "vms": ["priv", "work"]},
                                             {"owner": "my-org", "vms": ["lab", "work"]},
                                             {"owner": "solo", "vms": ["lab"]}])

    def test_text(self):
        code, out, err = self.run_agentbox("github-app")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "GitHub App 12345 agentbox-test (Agent Box Test), owned by likt0r",
            "Install link: https://github.com/apps/agentbox-test/installations/new",
            "Installed on:",
            "  acme-corp            Organization  selected repos  VMs: work  [suspended]",
            "  likt0r               User          all repos       VMs: priv, work",
            "  unused-org           Organization  all repos       VMs: -",
            "Missing Installations (these VMs get no tokens for these GitHub owners):",
            "  my-org               VMs: lab, work",
            "  solo                 VMs: lab"])

    def test_stored_key_github_does_not_accept(self):
        # the App's error, in its place in the list: it can still be removed or get a new key
        self.github(key="other")
        [app] = self.overview()["apps"]
        self.assertEqual((app["id"], app["installations"], app["error"]), (12345, [], {
            "error": "github_unavailable",
            "message": "GitHub answers /app with HTTP 401: A JSON web token could not be decoded",
            "params": {"path": "/app", "status": 401}}))
        self.assertEqual(self.run_agentbox("github-app"),
                         (0, "GitHub App 12345: GitHub answers /app with HTTP 401: A JSON web token could not be "
                             "decoded\nMissing Installations (these VMs get no tokens for these GitHub owners):\n"
                             "  acme-corp            VMs: work\n  likt0r               VMs: priv, work\n"
                             "  my-org               VMs: lab, work\n  solo                 VMs: lab\n", ""))

    def test_github_unreachable(self):
        self.github(exit=6)
        self.assertEqual(self.run_agentbox("github-app"), (1, "", "agentbox: GitHub is not reachable\n"))

    def test_no_app_set_up(self):
        for conf in (b"", None):
            with self.subTest(conf=conf):
                self.store(conf=conf or b"# none\n")
                if conf is None:
                    os.remove(os.path.join(self.github_dir, "app.conf"))
                self.assertEqual(self.run_agentbox("github-app"),
                                 (1, "", "agentbox: no GitHub App is set up: agentbox set-github-app\n"))
        self.assertEqual(self.calls(), [])

    def test_untrusted_text_from_github_is_cleaned(self):
        self.github(installations=[installation(1, "evil\x1b[31m\nlogin"), {"id": "x", "account": None}],
                    app={"id": 12345, "slug": "Bad Slug/../x", "name": "a\u202eb", "owner": "nope"})
        [app] = self.overview()["apps"]
        self.assertEqual({k: app[k] for k in APP}, {"id": 12345, "slug": None, "name": "ab", "owner": None,
                                                    "html_url": None, "install_url": None})
        self.assertEqual([(i["id"], i["account"]) for i in app["installations"]], [(None, None), (1, "evil[31mlogin")])

    def test_read_only(self):
        self.assertEqual(self.run_agentbox("--actor=timer", "github-app")[0], 0)
        self.assertEqual(self.run_agentbox("github-app", "--bogus"),
                         (2, "", "agentbox: usage: agentbox github-app [--json]\n"))
        self.assertEqual(self.journald.entries(), [])


def journal_entry(ident, message, age):
    return {"__REALTIME_TIMESTAMP": str(int((time.time() - age) * 10**6)), "__CURSOR": f"c-{age}", "_BOOT_ID": "b1",
            "PRIORITY": "5", "SYSLOG_IDENTIFIER": ident, "MESSAGE": message, **writer(ident)}


HOUR = 3600
TOKEN_LOG = [
    journal_entry("agentbox-gh-priv", "[priv] token for likt0r/askos until 2026-11-03T08:00:00Z", 7 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] refused: 'evil/repo': owner-not-allowed: evil is not a GitHub owner "
                                      "of the VM priv", 6 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] token for Likt0r/Askos until 2026-11-03T10:00:00Z", 5 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] refused: 'drop table; x': invalid-request: the request must be "
                                      "<owner>/<repo>", 4 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] refused: \"it's/x\": invalid-request: the request must be "
                                      "<owner>/<repo>", 3 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] error: 'likt0r/other': TimeoutError('timed out')", 2 * HOUR),
    journal_entry("agentbox-gh-priv", "[priv] refused: 'acme-corp/site': not-installed: the GitHub App is not "
                                      "installed on acme-corp/site", HOUR),
    journal_entry("agentbox-gh-work", "[work] token for acme-corp/site until 2026-11-03T12:00:00Z", HOUR),
    journal_entry("agentbox-proxy-priv", "[priv] ALERT evil.example [exfil]", HOUR),
]


class TokensTest(GitHubTest):
    def journal(self, entries):
        with open(os.path.join(self.dir, "journal.json"), "w") as f:
            json.dump(entries, f)

    def tokens(self, *args, vm="priv"):
        code, out, err = self.run_agentbox("github-tokens", vm, *args, "--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        times = [r.pop("time") for r in result["requests"]]
        for t in times:
            self.assertRegex(t, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
        return result, times

    def test_repositories_and_refused_requests_of_one_vm(self):
        self.journal(TOKEN_LOG)
        result, times = self.tokens()
        self.assertEqual(result["requests"], [
            {"outcome": "refused", "repository": "acme-corp/site", "request": "acme-corp/site",
             "reason": "the GitHub App is not installed on acme-corp/site", "reason_code": "not-installed",
             "reason_params": {}, "expires_at": None},
            {"outcome": "error", "repository": "likt0r/other", "request": "likt0r/other",
             "reason": "TimeoutError('timed out')", "reason_code": None, "reason_params": {}, "expires_at": None},
            {"outcome": "refused", "repository": None, "request": "it's/x",
             "reason": "the request must be <owner>/<repo>", "reason_code": "invalid-request", "reason_params": {}, "expires_at": None},
            {"outcome": "refused", "repository": None, "request": "drop table; x",
             "reason": "the request must be <owner>/<repo>", "reason_code": "invalid-request", "reason_params": {}, "expires_at": None},
            {"outcome": "issued", "repository": "Likt0r/Askos", "request": "Likt0r/Askos", "reason": None,
             "reason_code": None, "reason_params": {}, "expires_at": "2026-11-03T10:00:00Z"},
            {"outcome": "refused", "repository": "evil/repo", "request": "evil/repo",
             "reason": "evil is not a GitHub owner of the VM priv", "reason_code": "owner-not-allowed",
             "reason_params": {}, "expires_at": None},
            {"outcome": "issued", "repository": "likt0r/askos", "request": "likt0r/askos", "reason": None,
             "reason_code": None, "reason_params": {}, "expires_at": "2026-11-03T08:00:00Z"}])
        self.assertEqual(result["repositories"], [
            {"repository": "acme-corp/site", "tokens": 0, "last_token": None, "refused": 1, "last_refused": times[0]},
            {"repository": "likt0r/other", "tokens": 0, "last_token": None, "refused": 1, "last_refused": times[1]},
            {"repository": "Likt0r/Askos", "tokens": 2, "last_token": times[4], "refused": 0,
             "last_refused": None},
            {"repository": "evil/repo", "tokens": 0, "last_token": None, "refused": 1, "last_refused": times[5]}])
        self.assertEqual((result["vm"], result["truncated"], result["retention_days"]), ("priv", False, 90))

    def test_filters(self):
        self.journal(TOKEN_LOG)
        result, _ = self.tokens("--since=-330min", "--until", "-90min")
        self.assertEqual([r["request"] for r in result["requests"]], ["likt0r/other", "it's/x", "drop table; x",
                                                                      "Likt0r/Askos"])
        result, _ = self.tokens("--lines=2")
        self.assertEqual(len(result["requests"]), 2)
        self.assertTrue(result["truncated"])
        result, _ = self.tokens(vm="work")
        self.assertEqual([(r["outcome"], r["repository"]) for r in result["requests"]], [("issued", "acme-corp/site")])

    def test_nothing_requested(self):
        result, _ = self.tokens()
        self.assertEqual(result, {"vm": "priv", "repositories": [], "requests": [], "truncated": False,
                                  "retention_days": 90})

    def test_text(self):
        self.journal(TOKEN_LOG)
        code, out, err = self.run_agentbox("github-tokens", "priv")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "priv: tokens for:")
        self.assertRegex(lines[1], r"^  Likt0r/Askos +2x, last \d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(lines[2], "Refused:")
        self.assertRegex(lines[3], r"^  \d{4}-\d\d-\d\d \d\d:\d\d:\d\d  acme-corp/site: the GitHub App is not "
                                   r"installed on acme-corp/site$")
        self.assertEqual(len(lines), 8)

    def test_a_refusal_names_the_parameters_of_its_reason(self):
        # GitHub's status of github-status, so the Control Room can say it; odd names are left out
        self.journal([journal_entry("agentbox-gh-priv", "[priv] refused: 'a/b': github-status status=502 "
                                                        "request=x: GitHub answered 502", HOUR)])
        result, _ = self.tokens()
        self.assertEqual({k: result["requests"][0][k] for k in ("request", "reason", "reason_code", "reason_params")},
                         {"request": "a/b", "reason": "GitHub answered 502", "reason_code": "github-status",
                          "reason_params": {"status": "502"}})

    def test_hostile_requests_are_cleaned(self):
        self.journal([journal_entry("agentbox-gh-priv", "[priv] refused: 'a\\x1b[31m\\nb ghs_abcdefghijklmn "
                                                        "orca://pair?code=X': invalid-request: no", HOUR)])
        result, _ = self.tokens()
        self.assertEqual(result["requests"][0]["request"], "a b [hidden] orca://pair?[hidden]")
        self.assertEqual((result["requests"][0]["reason"], result["requests"][0]["reason_code"]),
                         ("no", "invalid-request"))

    def test_invalid_arguments(self):
        for args in (["--kind=github"], ["--since=gestern"], ["--lines=0"], ["--vm=work"], ["--lines"]):
            with self.subTest(args=args):
                code, out, err = self.run_agentbox("github-tokens", "priv", *args)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("agentbox: "), err)
        self.assertEqual(self.run_agentbox("github-tokens", "nope"), (1, "", "agentbox: No such VM: nope\n"))
        self.assertEqual(self.run_agentbox("github-tokens"), (1, "", "agentbox: no VM given\n"))
        self.assertEqual(self.calls(), [])

    def test_read_only(self):
        self.assertEqual(self.run_agentbox("--actor=timer", "github-tokens", "priv")[0], 0)
        self.assertEqual(self.journald.entries(), [])


if __name__ == "__main__":
    unittest.main()
