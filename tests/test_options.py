"""A command's arguments (agentbox/options.py): the rules every command shares.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import options  # noqa: E402
from agentbox.errors import Fail  # noqa: E402

KNOWN = {"json": False, "note": True, "reset": options.MANY, "lines": lambda v: v.isdigit()}


class ParseTest(unittest.TestCase):
    def fails(self, args, known=KNOWN, **kw):
        with self.assertRaises(Fail) as caught:
            options.parse(args, known, **kw)
        f = caught.exception
        self.assertEqual(f.error, "invalid_argument")
        return f.message, f.params.get("option"), f.exit

    def test_positional_arguments_flags_and_values(self):
        self.assertEqual(options.parse(["work", "--json", "--note", "a b", "x"], KNOWN),
                         (["work", "x"], {"json": True, "note": "a b"}))
        self.assertEqual(options.parse(["--note=a=b", "--lines=5"], KNOWN), ([], {"note": "a=b", "lines": "5"}))
        self.assertEqual(options.parse([], KNOWN), ([], {}))

    def test_a_handler_reuses_the_dispatchers_parse(self):
        parsed = options.Parsed(["work", "--note", "hello"], ["work"], {"note": "hello"})
        self.assertEqual(options.parse(parsed, {"note": True, "json": False}), (["work"], {"note": "hello"}))
        self.assertEqual(list(parsed), ["work", "--note", "hello"])

    def test_a_value_is_taken_as_given_also_when_it_looks_like_an_option(self):
        self.assertEqual(options.parse(["--note", "--json"], KNOWN), ([], {"note": "--json"}))
        self.assertEqual(options.parse(["--note", ""], KNOWN), ([], {"note": ""}))

    def test_a_missing_value(self):
        self.assertEqual(self.fails(["--note"]), ("--note needs a value", "--note", 2))

    def test_a_flag_takes_no_value(self):
        self.assertEqual(self.fails(["--json=yes"]), ("--json takes no value", "--json", 2))

    def test_an_unknown_option_names_the_option_without_its_value(self):
        self.assertEqual(self.fails(["--colour=red"]), ("unknown option: --colour=red", "--colour", 2))
        self.assertEqual(self.fails(["--"]), ("unknown option: --", "--", 2))

    def test_no_abbreviations(self):
        self.assertEqual(self.fails(["--js"]), ("unknown option: --js", "--js", 2))
        self.assertEqual(self.fails(["--not", "x"]), ("unknown option: --not", "--not", 2))

    def test_an_option_with_one_value_given_twice(self):
        self.assertEqual(self.fails(["--note", "a", "--note=b"]), ("--note given twice", "--note", 2))
        self.assertEqual(self.fails(["--lines", "1", "--lines", "2"]), ("--lines given twice", "--lines", 2))

    def test_a_flag_twice_is_still_the_flag(self):
        self.assertEqual(options.parse(["--json", "--json"], KNOWN), ([], {"json": True}))

    def test_an_option_that_may_repeat_gives_the_list_of_its_values(self):
        self.assertEqual(options.parse(["--reset", "work", "--reset=priv"], KNOWN), ([], {"reset": ["work", "priv"]}))

    def test_a_check_refuses_a_value(self):
        self.assertEqual(self.fails(["--lines", "many"]), ("invalid value for --lines: many", "--lines", 2))

    def test_without_positional_arguments_one_is_an_unknown_option(self):
        self.assertEqual(self.fails(["work"], positional=False), ("unknown option: work", "work", 2))
        self.assertEqual(options.parse(["--json"], KNOWN, positional=False), ([], {"json": True}))

    def test_the_exit_code_and_prefix_of_the_command(self):
        self.assertEqual(self.fails(["--note"], exit=1, prefix="work: "), ("work: --note needs a value", "--note", 1))

    def test_unprintable_characters_never_reach_the_message(self):
        message, option, _ = self.fails(["--\x1b[31mx=\x07"])
        self.assertTrue(message.isprintable(), message)
        self.assertTrue(option.isprintable(), option)


if __name__ == "__main__":
    unittest.main()
