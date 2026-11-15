"""The classifier adapter (agentbox/classifier.py, #103): the only module that starts
agentbox-suspicious or reads its answers, each answer's shape checked once.

Seam A: the `agentbox` CLI against a temporary root (AGENTBOX_ROOT) with a fake classifier
installed in it, which answers each question with a good answer, except the one it is told to
break; a fake `journalctl` (fake_journalctl.py) on PATH holds one proxy line for the allowlist
preview, so the classifier is asked about one host.

Run from the repo root: python3 -m unittest discover -s tests
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from journal_writers import write_passwd  # noqa: E402
from test_allowlist import proxy_line  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
AGENTBOX = os.path.join(ROOT, "bin", "agentbox")
HOST = "a.plain.example"            # the host of the proxy log the preview asks about
FEED = {"present": True, "entries": 1, "updated": 1793700000, "error": None}
GOOD = {
    "--json": {"domain": "plain.example", "signals": [], "alert": False, "strong": False, "harmless": None,
               "harmless_regex": False, "legitimate": None, "legitimate_regex": False,
               "feeds": {"threat-intelligence": FEED}},
    "--signals": {"hosts": {HOST: {"signals": [], "strong": False}}, "watchlist_unreadable": None},
    "--lifts": {"signals": [], "feed_entries": [], "feed_present": True},
    "--constants": {"categories": {"exfil": True, "paste": False}, "genuine": ["pypi.org"]},
    "--watchlist": {"feeds": {"threat-intelligence": FEED}},
}
# the fake classifier: GOOD's answer to its first argument, or the broken one in <dir>/broken
# ({"question": ..., "answer": text}) for that question
FAKE = '''#!PYTHON
import json, sys
good = GOOD
try:
    with open(DIR + "/broken") as f:
        broken = json.load(f)
except FileNotFoundError:
    broken = {}
if sys.argv[1] == broken.get("question"):
    print(broken["answer"])
else:
    print(json.dumps(good[sys.argv[1]]))
'''


def without(answer, key):
    return json.dumps({k: v for k, v in answer.items() if k != key})


def but(answer, **fields):
    return json.dumps({**answer, **fields})


SIGNAL = {"signal": "young", "strong": False, "suppressed": False, "reason": "newly registered", "code": "young",
          "params": {}}
# each question, the command that asks it, and broken answers: not JSON, the wrong type, a
# field missing or of the wrong type
QUESTIONS = {
    "--json": (["check-domain", "plain.example"],
               ["not json", "[]", without(GOOD["--json"], "feeds"), but(GOOD["--json"], feeds=[]),
                but(GOOD["--json"], feeds={"threat-intelligence": "ok"}), without(GOOD["--json"], "signals"),
                but(GOOD["--json"], signals=[{"signal": "young"}]),
                but(GOOD["--json"], signals=[{**SIGNAL, "params": {"n": 1}}]),
                but(GOOD["--json"], signals=[{**SIGNAL, "strong": "no"}]),
                *(but(GOOD["--json"], signals=[{k: v for k, v in SIGNAL.items() if k != field}])
                  for field in ("reason", "code", "suppressed")),
                without(GOOD["--json"], "domain"),
                but(GOOD["--json"], alert="no"), without(GOOD["--json"], "strong"),
                but(GOOD["--json"], harmless_regex=None), but(GOOD["--json"], legitimate_regex=1),
                but(GOOD["--json"], harmless=1), but(GOOD["--json"], legitimate=["pypi.org"])]),
    "--signals": (["check-regex", "allowlist", "--regex", r"[a-z]+\.plain\.example", "--scope", "work"],
                  ["not json", "{}", but(GOOD["--signals"], hosts=[]), but(GOOD["--signals"], hosts={}),
                   but(GOOD["--signals"], hosts={HOST: []}),
                   but(GOOD["--signals"], hosts={HOST: {"signals": "young", "strong": False}}),
                   but(GOOD["--signals"], hosts={HOST: {"signals": [1], "strong": False}}),
                   but(GOOD["--signals"], hosts={HOST: {"signals": []}}),
                   but(GOOD["--signals"], hosts={HOST: {"signals": [], "strong": "no"}}),
                   without(GOOD["--signals"], "watchlist_unreadable"),
                   but(GOOD["--signals"], watchlist_unreadable=3)]),
    "--lifts": (["legitimate-add", "plain.example"],
                ["not json", without(GOOD["--lifts"], "feed_present"), but(GOOD["--lifts"], signals=[1]),
                 but(GOOD["--lifts"], feed_entries="x"), but(GOOD["--lifts"], feed_present="yes")]),
    "--constants": (["watch-add", "plain.example", "--category", "paste"],
                    ["not json", but(GOOD["--constants"], categories={"paste": "no"}),
                     but(GOOD["--constants"], categories=[]), without(GOOD["--constants"], "genuine"),
                     but(GOOD["--constants"], genuine=[1])]),
    "--watchlist": (["watchlist"], ["not json", "[]", but(GOOD["--watchlist"], feeds=[]),
                                    but(GOOD["--watchlist"], feeds={"threat-intelligence": None})]),
}


class ClassifierTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.root = os.path.join(self.dir, "root")
        vm_records.write(self.root)
        write_passwd(self.root)
        conf = os.path.join(self.root, "etc", "agentbox")
        for name in ("watchlist.json", "legitimate.json"):
            with open(os.path.join(conf, name), "w") as f:
                f.write('{"entries": []}\n')
        self.install(os.path.join(self.root, "usr", "libexec", "agentbox-suspicious"),
                     FAKE.replace("PYTHON", sys.executable, 1).replace("GOOD", repr(GOOD), 1)
                     .replace("DIR", repr(self.dir), 1))
        self.install(os.path.join(self.dir, "bin", "journalctl"),
                     f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(TESTS, "fake_journalctl.py")}" "$@"\n')
        with open(os.path.join(self.dir, "journal.json"), "w") as f:
            json.dump([proxy_line("work", f"BLOCKED {HOST} (not on the Allowlist)", 600)], f)

    @staticmethod
    def install(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def run_agentbox(self, *args):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"], "FAKE_DIR": self.dir,
               "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "CET-1"}
        r = subprocess.run([AGENTBOX, *args, "--json"], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def files(self):
        conf = os.path.join(self.root, "etc", "agentbox")
        out = {}
        for name in sorted(os.listdir(conf)):
            if os.path.isfile(os.path.join(conf, name)):
                with open(os.path.join(conf, name), "rb") as f:
                    out[name] = f.read()
        return out

    def break_answer(self, question, answer):
        with open(os.path.join(self.dir, "broken"), "w") as f:
            json.dump({"question": question, "answer": answer}, f)

    def test_each_question_takes_a_good_answer(self):
        for question, (command, _) in QUESTIONS.items():
            with self.subTest(question=question):
                code, out, err = self.run_agentbox(*command)
                self.assertEqual((code, err), (0, ""), out)
        preview = json.loads(self.run_agentbox(*QUESTIONS["--signals"][0])[1])["preview"]
        self.assertEqual([(h["host"], h["blocked"]) for h in preview["hosts"]], [(HOST, 1)])

    def test_a_broken_answer_of_each_kind_ends_the_command(self):
        for question, (command, answers) in QUESTIONS.items():
            for answer in answers:
                with self.subTest(question=question, answer=answer):
                    self.break_answer(question, answer)
                    before = self.files()
                    code, out, err = self.run_agentbox(*command)
                    self.assertEqual((code, out), (1, ""), err)
                    said = json.loads(err)
                    self.assertEqual((said["error"], said["message"]),
                                     ("action_failed", "agentbox-suspicious: invalid answer"))
                    self.assertEqual(self.files(), before)


def sources():
    """The CLI's modules and the Control Room backend: (path, text)."""
    folder = os.path.join(ROOT, "agentbox")
    paths = [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n.endswith(".py")]
    for path in paths + [os.path.join(ROOT, "web", "agentbox-web"), AGENTBOX]:
        with open(path, encoding="utf-8") as f:
            yield path, f.read()


def names(text):
    """The imported names of a source, as (module, name) for `from .module import name`, and
    the attributes it reads, as (module, attribute) for `module.attribute`."""
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            yield from ((node.module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            yield node.value.id, node.attr


class SeamTest(unittest.TestCase):
    def test_only_the_adapter_starts_the_classifier_or_reads_its_answers(self):
        for path, text in sources():
            if os.path.basename(path) in ("classifier.py", "paths.py"):
                continue
            with self.subTest(path=path):
                self.assertNotIn(("paths", "SUSPICIOUS"), set(names(text)))
                self.assertNotIn("libexec/agentbox-suspicious", text.replace(os.sep, "/"))
                self.assertNotIn("agentbox_suspicious", text)
                self.assertNotIn("invalid answer", text)

    def test_no_module_imports_a_private_helper_of_detection_allowlist_lists_or_the_adapter(self):
        for path, text in sources():
            with self.subTest(path=path):
                self.assertEqual([(m, n) for m, n in names(text)
                                  if m in ("detection", "allowlist", "lists", "classifier") and n.startswith("_")], [])


if __name__ == "__main__":
    unittest.main()
