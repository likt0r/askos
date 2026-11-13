"""`/usr/lib/agentbox/opencode-forget`, which guest-setup runs as the agent at every
start: opencode's sessions stay on the Data disk, the logins made in the VM next to them do
not (auth.json, mcp-auth.json, the login tables of opencode.db), nor do approvals saved as
"always" (its permission table), and nothing it meets stops the VM.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "vm", "files", "usr", "lib", "agentbox", "opencode-forget")

# the tables of opencode 1.19.5's opencode.db this concerns, as it creates them
SCHEMA = """
CREATE TABLE account (id text PRIMARY KEY, email text NOT NULL, url text NOT NULL, access_token text NOT NULL,
                      refresh_token text NOT NULL, token_expiry integer, time_created integer NOT NULL,
                      time_updated integer NOT NULL);
CREATE TABLE account_state (id integer PRIMARY KEY, active_account_id text, active_org_id text,
                            FOREIGN KEY (active_account_id) REFERENCES account(id) ON DELETE SET NULL);
CREATE TABLE control_account (email text NOT NULL, url text NOT NULL, access_token text NOT NULL,
                              refresh_token text NOT NULL, token_expiry integer, active integer NOT NULL,
                              time_created integer NOT NULL, time_updated integer NOT NULL,
                              PRIMARY KEY (email, url));
CREATE TABLE credential (id text PRIMARY KEY, integration_id text, label text NOT NULL, value text NOT NULL,
                         connector_id text, method_id text, active integer, time_created integer NOT NULL,
                         time_updated integer NOT NULL);
CREATE TABLE permission (id text PRIMARY KEY, project_id text NOT NULL, action text NOT NULL, resource text NOT NULL,
                         time_created integer NOT NULL, time_updated integer NOT NULL);
CREATE TABLE session (id text PRIMARY KEY, title text NOT NULL);
CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, data text NOT NULL);
"""
LOGINS = """
INSERT INTO account VALUES ('a1', 'me@example.org', 'https://opencode.ai', 'at-SECRET', 'rt-SECRET', 0, 1, 1);
INSERT INTO account_state VALUES (1, 'a1', 'org1');
INSERT INTO control_account VALUES ('me@example.org', 'https://opencode.ai', 'cat-SECRET', 'crt-SECRET', 0, 1, 1, 1);
INSERT INTO credential VALUES ('c1', 'i1', 'GitHub', 'cred-SECRET', NULL, NULL, 1, 1, 1);
INSERT INTO permission VALUES ('p1', 'prj1', 'bash', '*', 1, 1);
"""
SESSIONS = """
INSERT INTO session VALUES ('s1', 'Fix the proxy');
INSERT INTO message VALUES ('m1', 's1', 'hello');
"""


class ForgetLoginsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "opencode")
        os.mkdir(self.dir)
        self.db = os.path.join(self.dir, "opencode.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args):
        r = subprocess.run([sys.executable, "-I", SCRIPT, *(args or (self.dir,))], capture_output=True,
                           text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def make_db(self, sql=SCHEMA + LOGINS + SESSIONS):
        con = sqlite3.connect(self.db)
        con.executescript(sql)
        con.commit()
        con.close()

    def rows(self, table):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            con.close()

    def write(self, name, text="{}"):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def test_logins_and_saved_approvals_go_and_sessions_stay(self):
        self.make_db()
        self.write("auth.json", '{"opencode-go": {"type": "api", "key": "sk-SECRET"}}')
        self.write("mcp-auth.json", '{"github": {"tokens": {"accessToken": "SECRET"}}}')
        self.write("kept.json")
        os.mkdir(os.path.join(self.dir, "storage"))
        self.assertEqual(self.run_script(), (0, "", ""))
        self.assertEqual(sorted(os.listdir(self.dir)), ["kept.json", "opencode.db", "storage"])
        for table in ("account", "account_state", "control_account", "credential", "permission"):
            self.assertEqual(self.rows(table), [], table)
        self.assertEqual(self.rows("session"), [("s1", "Fix the proxy")])
        self.assertEqual(self.rows("message"), [("m1", "s1", "hello")])
        with open(self.db, "rb") as f:
            self.assertNotIn(b"rt-SECRET", f.read().replace(b"\0", b""))

    def test_files_made_directories_or_links_go_too(self):
        """A directory named auth.json would make `rm -f` fail and with it every start."""
        os.makedirs(os.path.join(self.dir, "auth.json", "inner"))
        target = os.path.join(self.tmp.name, "elsewhere.json")
        with open(target, "w") as f:
            f.write("{}")
        os.symlink(target, os.path.join(self.dir, "mcp-auth.json"))
        self.assertEqual(self.run_script(), (0, "", ""))
        self.assertEqual(os.listdir(self.dir), [])
        self.assertTrue(os.path.exists(target))          # the link goes, not what it points to

    def test_a_database_without_those_tables_is_left_alone(self):
        self.make_db("CREATE TABLE session (id text PRIMARY KEY, title text NOT NULL);"
                     "INSERT INTO session VALUES ('s1', 'kept');")
        self.assertEqual(self.run_script(), (0, "", ""))
        self.assertEqual(self.rows("session"), [("s1", "kept")])

    def test_nothing_there_is_fine(self):
        self.assertEqual(self.run_script(), (0, "", ""))
        self.assertEqual(self.run_script(os.path.join(self.tmp.name, "missing")), (0, "", ""))
        self.assertFalse(os.path.exists(self.db))         # never creates a database

    def test_a_broken_database_is_reported_and_stops_nothing(self):
        self.write("opencode.db", "this is not a database")
        self.write("auth.json")
        code, out, err = self.run_script()
        self.assertEqual((code, out), (0, ""))
        self.assertRegex(err, r"^opencode-forget: opencode\.db: .+\n$")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "auth.json")))

    def test_needs_the_folder(self):
        code, _, err = self.run_script("a", "b")
        self.assertEqual((code, err), (2, "usage: opencode-forget <opencode data dir>\n"))

    def test_runs_with_the_python_of_the_image_isolated(self):
        with open(SCRIPT) as f:
            self.assertEqual(f.readline(), "#!/usr/bin/python3 -I\n")
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
