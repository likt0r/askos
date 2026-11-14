"""The list records (agentbox/list_records.py): the files, entry checks and texts the CLI, the
proxy and the classifier share, and the seam between them: the proxy programs import that one
leaf module of the package, and the RPM restarts the proxies when it changes."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from agentbox import list_records as records  # noqa: E402

PROGRAMS = ("agentbox-proxy", "agentbox-suspicious", "agentbox-feeds")
# a proxy program loaded as a module (its main is guarded), then the package modules it imported
IMPORTS = """
import importlib.machinery, importlib.util, json, sys
loader = importlib.machinery.SourceFileLoader("program", sys.argv[1])
module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(module)
print(json.dumps(sorted(n for n in sys.modules if n == "agentbox" or n.startswith("agentbox."))))
"""


def watch(domain="a.example", category="exfil", **fields):
    return {"domain": domain, "category": category, "source": "", "note": "", "added": 0, "added_by": "t",
            **fields}


class SeamTest(unittest.TestCase):
    def imports(self, program):
        with tempfile.TemporaryDirectory() as conf:
            r = subprocess.run([sys.executable, "-I", "-B", "-c", IMPORTS, os.path.join(REPO, "proxy", program)],
                               capture_output=True, text=True, timeout=60,
                               env={**os.environ, "AGENTBOX_CONF_DIR": conf, "AGENTBOX_FEEDS_DIR": conf})
        self.assertEqual(r.returncode, 0, r.stderr)
        return set(json.loads(r.stdout))

    def test_the_proxy_programs_import_only_list_records(self):
        """They run as their own users: nothing of the CLI (activity, images, github) comes with
        it, and list_records itself imports only the standard library."""
        for program in PROGRAMS:
            with self.subTest(program=program):
                self.assertEqual(self.imports(program), {"agentbox", "agentbox.list_records"})

    def test_the_rpm_restarts_the_proxies_when_a_module_they_import_changes(self):
        with open(os.path.join(REPO, "rpm", "agentbox.spec")) as f:
            code = re.search(r"^%global proxy_code (.*)$", f.read(), re.M).group(1).split()
        imported = set().union(*(self.imports(p) for p in ("agentbox-proxy", "agentbox-suspicious")))
        modules = {f"%{{_prefix}}/lib/agentbox/agentbox/{m.split('.', 1)[1]}.py" for m in imported if "." in m}
        self.assertEqual(set(code), {"%{_libexecdir}/agentbox-proxy", "%{_libexecdir}/agentbox-suspicious"} | modules)

    def test_patterns_py_no_longer_loads_the_classifier(self):
        with open(os.path.join(REPO, "agentbox", "patterns.py")) as f:
            self.assertNotIn("scripts", f.read())


class RecordTest(unittest.TestCase):
    def test_a_record_without_its_list_is_broken(self):
        self.assertEqual(records.entries('{"entries": [1]}'), [1])
        for text, why in (("{", "not valid JSON"), ('{"entries": {}}', "no list entries"), ("[]", "no list entries")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, f"^{why}$"):
                records.entries(text)

    def test_the_texts_of_a_broken_watchlist(self):
        partial = {k: v for k, v in watch().items() if k != "note"}
        cases = [
            ([watch(category="exfill")], "broken entry (unknown category exfill)"),
            ([watch(), watch(category="c2")], "a.example listed twice"),
            ([partial], "broken entry (field note missing or of the wrong type)"),
            ([watch(added=True)], "broken entry (field added missing or of the wrong type)"),
            ([watch("A.example")], "broken entry (invalid domain A.example)"),
            ([watch("ex\xa0ample\x01.com")], "broken entry (invalid domain ex?ample?.com)"),
            ([watch(note="x" * 201)], "broken entry (text too long)"),
            ([{**watch(), "regex": "^a$"}], "broken entry (both domain and regex)"),
            (["a.example"], "broken entry (not a JSON object)"),
        ]
        for entries, why in cases:
            with self.subTest(why=why), self.assertRaises(ValueError) as e:
                records.checked(entries, records.watch_problem)
            self.assertEqual(str(e.exception), why)

    def test_a_domain_and_a_pattern_of_the_same_text_are_two_entries(self):
        both = [watch("a.example"), {**watch(), "regex": "a.example"}]
        del both[1]["domain"]
        self.assertEqual(records.checked(both, records.watch_problem), both)

    def test_a_refused_pattern_is_no_broken_entry(self):
        e = {"regex": "^(a+)+$", "note": "", "added": 0, "added_by": "t"}
        self.assertIsNone(records.legitimate_problem(e))
        self.assertRegex(records.refused_entry(e), r"^regex entry \^\(a\+\)\+\$ refused: a quantifier")


class AllowlistRecordTest(unittest.TestCase):
    def entry(self, **fields):
        return {"domain": "a.example", "scope": "all", "expires": None, "note": "", "added": 0, "added_by": "t",
                **fields}

    def test_the_proxy_and_the_cli_refuse_the_same_entries(self):
        for e, why in ((self.entry(scope="Work"), "broken entry (invalid scope Work)"),
                       (self.entry(scope="v1"), "broken entry (invalid scope v1)"),
                       (self.entry(note="x" * 201), "broken entry (note too long)"),
                       (self.entry(expires=1.5), "broken entry (field expires missing or of the wrong type)"),
                       (self.entry(domain="com"), "broken entry (invalid domain com)")):
            with self.subTest(why=why), self.assertRaises(ValueError) as caught:
                records.allowlist_entries(json.dumps({"entries": [e]}))
            self.assertEqual(str(caught.exception), why)
        with self.assertRaisesRegex(ValueError, r"^a\.example \(all\) twice$"):
            records.allowlist_entries(json.dumps({"entries": [self.entry(), self.entry(note="x")]}))
        both = [self.entry(), self.entry(scope="work")]
        self.assertEqual(records.allowlist_entries(json.dumps({"entries": both})), both)


class LegacyTest(unittest.TestCase):
    def test_text_lists(self):
        text = "Bad.Example. exfil # Report 1\n\n# a comment\nother.example\n"
        self.assertEqual(records.legacy_lines(text)[0], ("Bad.Example. exfil ", " Report 1"))
        self.assertEqual(records.legacy_domains(text), ["bad.example", "other.example"])
        self.assertEqual(records.legacy_allowlist("A.example.\n  # c\nb.example # c\n"), ["a.example", "b.example"])

    def test_harmless_marks(self):
        self.assertEqual(records.harmless_marks("# Domains\nA.example. extra\nregex ^b\\.example$ # c\nregex\n"),
                         [{"domain": "a.example"}, {"regex": "^b\\.example$"}, {"domain": "regex"}])


class OpenWebTest(unittest.TestCase):
    def test_whole_seconds_in_the_future(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, records.OPEN_WEB.format("work"))
            self.assertEqual(records.open_until(path), 0)
            later = int(time.time()) + 600
            for text, until in ((f"{later}.75\n", later), ("inf", 0), ("1e20", 0), ("nan", 0), ("100", 0)):
                with self.subTest(text=text):
                    with open(path, "w") as f:
                        f.write(text)
                    self.assertEqual(records.open_until(path), until)


if __name__ == "__main__":
    unittest.main()
