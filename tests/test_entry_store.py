"""The storage the Allowlist and the lists share (agentbox/entry_store.py): arguments, the
checks of a stored entry, writing a record, read-modify-write and the text lists from before.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import entry_store, paths, patterns  # noqa: E402
from agentbox.errors import Fail  # noqa: E402
from agentbox.network import valid_domain  # noqa: E402

FIELDS = {"note": str, "added": int}


def shown(text, limit):
    return text[:limit]


class ArgumentsTest(unittest.TestCase):
    def fails(self, call, *args):
        with self.assertRaises(Fail) as caught:
            call(*args)
        return caught.exception

    def test_target_is_a_domain_or_a_pattern_in_its_place(self):
        self.assertEqual(entry_store.target(["example.org"], {}, valid_domain), ("domain", "example.org"))
        self.assertEqual(entry_store.target([], {"regex": "^a[0-9]+\\.org$"}, valid_domain),
                         ("regex", "^a[0-9]+\\.org$"))
        # a stored pattern the rules refuse can still be named
        self.assertEqual(entry_store.target([], {"regex": "(a+)+"}, valid_domain), ("regex", "(a+)+"))

    def test_target_refuses_too_many_arguments_and_an_invalid_domain(self):
        f = self.fails(entry_store.target, ["example.org"], {"regex": "^a$"}, valid_domain)
        self.assertEqual((f.error, f.message),
                         ("invalid_argument", "--regex takes the place of the domain; too many arguments: example.org"))
        f = self.fails(entry_store.target, ["example.org", "x"], {}, valid_domain)
        self.assertEqual((f.error, f.message), ("invalid_argument", "too many arguments: x"))
        f = self.fails(entry_store.target, [], {}, valid_domain)
        self.assertEqual(f.error, "invalid_domain")

    def test_target_checks_the_domain_with_the_function_given(self):
        seen = []
        self.assertEqual(entry_store.target(["a.org"], {}, lambda d: seen.append(d) or d.upper()),
                         ("domain", "A.ORG"))
        self.assertEqual(seen, ["a.org"])

    def test_text_is_one_stripped_line_with_its_own_error_code(self):
        self.assertIsNone(entry_store.text(None, "note", 10))
        self.assertEqual(entry_store.text("  a b  ", "note", 10), "a b")
        for name in ("note", "source"):
            f = self.fails(entry_store.text, "x" * 11, name, 10)
            self.assertEqual((f.error, f.message, f.params["option"]),
                             (f"invalid_{name}", f"--{name}: at most 10 printable characters on one line",
                              f"--{name}"))
            self.assertEqual(self.fails(entry_store.text, "a\nb", name, 10).error, f"invalid_{name}")

    def test_label_param_and_given(self):
        self.assertEqual(entry_store.label(("domain", "a.org")), "a.org")
        self.assertEqual(entry_store.label(("regex", "^a$")), "pattern ^a$")
        self.assertEqual(entry_store.param(("regex", "^a$")), {"regex": "^a$"})
        self.assertEqual(entry_store.given(("domain", "a.org")), {"domain": "a.org", "regex": None})
        self.assertEqual(entry_store.given(("regex", "^a$")), {"domain": None, "regex": "^a$"})


class ProblemTest(unittest.TestCase):
    def problem(self, e, extra=None):
        return entry_store.problem(e, FIELDS, shown, extra)

    def test_the_common_checks(self):
        self.assertIsNone(self.problem({"domain": "a.org", "note": "", "added": 1}))
        self.assertIsNone(self.problem({"regex": "(a+)+", "note": "", "added": 1}))
        self.assertEqual(self.problem([]), "not a JSON object")
        self.assertEqual(self.problem({"note": "", "added": 1}), "field domain missing or of the wrong type")
        self.assertEqual(self.problem({"regex": 1, "note": "", "added": 1}), "field regex missing or of the wrong type")
        self.assertEqual(self.problem({"domain": "a.org", "note": "", "added": True}),
                         "field added missing or of the wrong type")
        self.assertEqual(self.problem({"domain": "a.org", "regex": "^a$", "note": "", "added": 1}),
                         "both domain and regex")

    def test_an_invalid_domain_is_shown_by_the_list_s_function(self):
        self.assertEqual(entry_store.problem({"domain": "A" * 70, "note": "", "added": 1}, FIELDS,
                                             lambda t, n: f"<{t[:n]}>"), f"invalid domain <{'A' * 60}>")

    def test_the_list_s_own_checks_come_last(self):
        def extra(e):
            return "note too long" if len(e["note"]) > 3 else None
        self.assertEqual(self.problem({"domain": "a.org", "note": "abcd", "added": 1}, extra), "note too long")
        self.assertEqual(self.problem({"domain": "-", "note": "abcd", "added": 1}, extra), "invalid domain -")
        self.assertIsNone(self.problem({"domain": "a.org", "note": "abc", "added": 1}, extra))


class ValidTest(unittest.TestCase):
    def valid(self, entries, **kw):
        def broken(why):
            raise ValueError(why)
        return entry_store.valid(entries, lambda e: entry_store.problem(e, FIELDS, shown), broken, **kw)

    def test_a_broken_or_second_entry_ends_it(self):
        good = [{"domain": "a.org", "note": "", "added": 1}, {"regex": "a.org", "note": "", "added": 1}]
        self.assertIs(self.valid(good), good)
        with self.assertRaisesRegex(ValueError, r"^broken entry \(not a JSON object\)$"):
            self.valid(good + [1])
        with self.assertRaisesRegex(ValueError, r"^a\.org listed twice$"):
            self.valid(good + [dict(good[0])])

    def test_key_and_twice_are_the_list_s(self):
        entries = [{"domain": "a.org", "scope": "x", "note": "", "added": 1},
                   {"domain": "a.org", "scope": "y", "note": "", "added": 1}]
        key = lambda e: (patterns.key(e), e["scope"])  # noqa: E731
        twice = lambda e: f"{patterns.value(e)} ({e['scope']}) twice"  # noqa: E731
        self.assertEqual(self.valid(entries, key=key, twice=twice), entries)
        with self.assertRaisesRegex(ValueError, r"^a\.org \(y\) twice$"):
            self.valid(entries + [dict(entries[1])], key=key, twice=twice)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        # the lock files (state_files.locked) under this test's directory
        patcher = mock.patch.object(paths, "STATE", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.dir, "list.json")

    def legacy(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def stored(self):
        with open(self.path) as f:
            return f.read()

    def test_write_sorts_and_moves_the_legacy_files_aside(self):
        old = [self.legacy("one", "a.org\n"), os.path.join(self.dir, "missing")]
        entry_store.write(self.path, [{"domain": "b.org"}, {"domain": "a.org"}], patterns_sorted, old)
        self.assertEqual(json.loads(self.stored()), {"entries": [{"domain": "a.org"}, {"domain": "b.org"}]})
        self.assertTrue(self.stored().endswith("}\n"))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        self.assertFalse(os.path.exists(old[0]))
        self.assertTrue(os.path.exists(old[0] + ".migrated"))

    def test_write_ends_the_command_when_a_legacy_file_cannot_be_moved(self):
        old = self.legacy("one", "a.org\n")
        real = os.replace

        def replace(src, dst):
            if src == old:
                raise PermissionError(13, "Permission denied")
            real(src, dst)
        with mock.patch.object(entry_store.os, "replace", replace):
            with self.assertRaises(Fail) as caught:
                entry_store.write(self.path, [], patterns_sorted, [old])
        self.assertEqual((caught.exception.error, caught.exception.message),
                         ("action_failed", f"{old}: Permission denied"))
        self.assertTrue(os.path.exists(old))

    def test_changing_holds_the_record_s_lock_while_it_runs(self):
        from agentbox import state_files
        self.assertFalse(state_files.held(self.path))
        with entry_store.changing(self.path, lambda: ([], []), lambda es, lg: None, patterns_sorted):
            # held() asks without waiting, as a second agentbox call would
            self.assertTrue(state_files.held(self.path))
        self.assertFalse(state_files.held(self.path))

    def changes(self, entries, legacy, change):
        written = []
        with entry_store.changing(self.path, lambda: (entries, legacy), lambda es, lg: written.append((list(es), lg)),
                                  patterns_sorted) as es:
            change(es)
        return written

    def test_changing_writes_only_a_change_or_a_migration(self):
        e = [{"domain": "b.org"}, {"domain": "a.org"}]
        self.assertEqual(self.changes(list(e), [], lambda es: es.reverse()), [])
        self.assertEqual(self.changes(list(e), [], lambda es: es.pop()), [([{"domain": "b.org"}], [])])
        self.assertEqual(self.changes(list(e), ["old"], lambda es: None), [(e, ["old"])])

    def test_changing_writes_nothing_when_the_command_ends(self):
        written = []
        with self.assertRaisesRegex(Fail, "^no$"):
            with entry_store.changing(self.path, lambda: ([], []), lambda es, lg: written.append(es),
                                      patterns_sorted) as es:
                es.append({"domain": "a.org"})
                raise Fail("no", error="action_failed")
        self.assertEqual(written, [])

    def test_find_by_ref_and_fields(self):
        entries = [{"domain": "a.org", "scope": "all"}, {"regex": "a.org", "scope": "x"}]
        self.assertIs(entry_store.find(entries, ("regex", "a.org")), entries[1])
        self.assertIs(entry_store.find(entries, ["domain", "a.org"], scope="all"), entries[0])
        self.assertIsNone(entry_store.find(entries, ("domain", "a.org"), scope="x"))

    def test_legacy_lines_and_domains(self):
        path = self.legacy("old", "A.Org. # docs\n\n# only a comment\nb.org exfil # src\n")
        self.assertEqual(entry_store.legacy_lines(path),
                         [("A.Org. ", " docs"), ("", ""), ("", " only a comment"), ("b.org exfil ", " src")])
        self.assertIsNone(entry_store.legacy_lines(os.path.join(self.dir, "missing")))
        self.assertEqual(entry_store.legacy_domain("A.Org."), "a.org")
        with self.assertRaises(Fail) as caught:
            entry_store.legacy_lines(self.dir)
        self.assertEqual(caught.exception.error, "action_failed")


def patterns_sorted(entries):
    return sorted(entries, key=patterns.key)


if __name__ == "__main__":
    unittest.main()
