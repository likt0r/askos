"""What the Control Room may run through sudo (#50). Its sudoers rule lets agentbox-web run
agentbox only with one of the Control Room's commands (cli.CONTROL_ROOM_COMMANDS) as the first
argument, and agentbox itself, called through sudo by agentbox-web, runs only these commands and
never --force, before anything runs.

BackendTest reads web/agentbox-web and finds every agentbox command it runs, which must be
exactly that set and always the first argument. SudoersTest reads rpm/agentbox-web.sudoers: its
command list is the same set, and its regular expression, matched as sudo matches it (the
arguments joined with spaces), takes what the backend passes and nothing with another command
or an option first. CallerTest runs the real CLI as test_activity.py does, with SUDO_USER as sudo
sets it. (test_rpm.InstalledTest asks the installed sudo itself.)

Run from the repo root: python3 -m unittest discover -s tests
"""
import ast
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_activity import FIREFOX, REPO, ActivityTest  # noqa: E402

sys.path.insert(0, REPO)
from agentbox import cli, paths  # noqa: E402

SERVER = os.path.join(REPO, "web", "agentbox-web")
SUDOERS = os.path.join(REPO, "rpm", "agentbox-web.sudoers")
# the backend's functions that run agentbox with the command as their first argument
RUNNERS = ("agentbox", "agentbox_json", "run", "run_json")


def backend_commands():
    """{command: [line, ...]} of every agentbox command web/agentbox-web runs. A call whose
    command cannot be told from the source, or an argv whose first argument is not the command,
    fails the test instead of being left out."""
    with open(SERVER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def function(node):
        while node in parent:
            node = parent[node]
            if isinstance(node, ast.FunctionDef):
                return node
        return None

    def passed_on(arg, node):
        """What a runner got, handed on unchanged: its *args (agentbox_json -> agentbox, run_json
        -> run), or its own first parameter, the command, into the argv."""
        f = function(node)
        if f is None or f.name not in RUNNERS:
            return False
        if isinstance(arg, ast.Starred):
            return isinstance(arg.value, ast.Name) and f.args.vararg is not None and arg.value.id == f.args.vararg.arg
        return isinstance(arg, ast.Name) and bool(f.args.args) and arg.id == f.args.args[0].arg

    def loop_values(name, node):
        """The texts a loop variable takes: `for name in ("a", "b"):` around node."""
        while node in parent:
            node = parent[node]
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == name:
                if isinstance(node.iter, (ast.Tuple, ast.List)) and all(
                        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.iter.elts):
                    return [e.value for e in node.iter.elts]
        return None

    def values(arg, node):
        """The commands an argument can be: a text, a loop variable over texts, or a parameter
        of an enclosing function that every caller passes as one of these."""
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return [arg.value]
        if not isinstance(arg, ast.Name):
            return None
        found = loop_values(arg.id, node)
        if found is not None:
            return found
        f = function(node)              # the innermost function with that parameter (closures)
        while f is not None and arg.id not in [a.arg for a in f.args.args]:
            f = function(f)
        if f is None:
            return None
        params = [a.arg for a in f.args.args]
        index, out, callers = params.index(arg.id), [], 0
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == f.name:
                callers += 1
                given = values(call.args[index], call) if len(call.args) > index else None
                if given is None:
                    return None
                out += given
        return out if callers else None

    commands, unknown = {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in RUNNERS:
            first = node.args[0] if node.args else None
            if first is not None and passed_on(first, node):
                continue
            found = values(first, node) if first is not None else None
        elif isinstance(node, ast.Name) and node.id == "AGENTBOX" and isinstance(node.ctx, ast.Load):
            # [*AGENTBOX, <command>, ...]: the argv itself, the command right after the program
            starred, argv = parent.get(node), parent.get(parent.get(node))
            if not (isinstance(starred, ast.Starred) and isinstance(argv, ast.List)):
                unknown.append(node.lineno)
                continue
            rest = argv.elts[argv.elts.index(starred) + 1:]
            if rest and passed_on(rest[0], node):
                continue
            found = values(rest[0], node) if rest else None
        else:
            continue
        if found is None:
            unknown.append(node.lineno)
        for command in found or ():
            commands.setdefault(command, []).append(node.lineno)
    if unknown:
        raise AssertionError(f"web/agentbox-web runs agentbox with a command the test cannot tell, lines {unknown}")
    return commands


def sudoers_rule():
    """(the command list, the regular expression) of the one rule of rpm/agentbox-web.sudoers."""
    with open(SUDOERS, encoding="utf-8") as f:
        rules = [line for line in f.read().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert len(rules) == 1, rules
    m = re.fullmatch(rf"agentbox-web ALL=\(root\) NOPASSWD: {re.escape(paths.CLI)} (\^\(([a-z|-]+)\)\( \.\*\)\?\$)",
                     rules[0])
    assert m, rules[0]
    return m.group(2).split("|"), m.group(1)


def sudo_allows(*args):
    """Whether the rule's regular expression takes these arguments as sudo matches them: joined
    with spaces, as a POSIX extended expression, in which . also matches a newline and $ only
    the very end (Python's $ would also match before a last newline: fullmatch instead)."""
    return re.fullmatch(sudoers_rule()[1][1:-1], " ".join(args), re.DOTALL) is not None


class BackendTest(unittest.TestCase):
    def test_the_backend_runs_exactly_the_control_rooms_commands(self):
        """A command the backend runs but the set lacks would be refused through sudo; one the set
        has but the backend no longer runs would be allowed for nothing. Each is the first
        argument, as the sudoers rule wants it."""
        commands = backend_commands()
        self.assertGreater(len(commands), 50)
        self.assertEqual(sorted(set(commands) - cli.CONTROL_ROOM_COMMANDS), [], "run by the backend, not in the set")
        self.assertEqual(sorted(cli.CONTROL_ROOM_COMMANDS - set(commands)), [], "in the set, not run by the backend")
        # the event helper, and the loop of POST /vms/<vm>/start etc.
        self.assertIn("watch", commands)
        self.assertIn("restart", commands)

    def test_the_set_names_commands_of_the_cli(self):
        self.assertLessEqual(cli.CONTROL_ROOM_COMMANDS, set(cli.COMMANDS))
        # what only the owner at the Host runs
        for command in ("setup", "import", "regenerate", "migrate-lists", "unset-env", "build-run"):
            self.assertNotIn(command, cli.CONTROL_ROOM_COMMANDS)

    def test_the_backend_never_passes_force(self):
        with open(SERVER, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        self.assertEqual([node.lineno for node in ast.walk(tree) if isinstance(node, ast.Constant)
                          and isinstance(node.value, str) and node.value.startswith("--force")], [])


class SudoersTest(unittest.TestCase):
    def test_the_rule_lists_exactly_the_control_rooms_commands(self):
        listed, _ = sudoers_rule()
        self.assertEqual(listed, sorted(listed), "sorted, so that a change shows as one word")
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(set(listed), cli.CONTROL_ROOM_COMMANDS)

    def test_it_takes_what_the_backend_passes(self):
        for command in sorted(cli.CONTROL_ROOM_COMMANDS):
            with self.subTest(command=command):
                self.assertTrue(sudo_allows(command))
                self.assertTrue(sudo_allows(command, f"--actor={FIREFOX}", "work", "--note=a, b: c = d # e\nf",
                                            "--json"))

    def test_it_takes_nothing_with_another_command_or_an_option_first(self):
        for args in (["setup"], ["import", "work", "/etc"], ["regenerate"], ["--version"], ["startx"],
                     ["--actor=x", "start", "work"], [f"--actor={FIREFOX}", "setup"],
                     [f"--actor={FIREFOX} start", "setup"], ["-h"], ["", "start"], [" start"], ["watch-addx"],
                     ["start\nsetup"]):
            with self.subTest(args=args):
                self.assertFalse(sudo_allows(*args))
        # a first argument that only begins with a command and a space passes; agentbox knows no
        # such command (CallerTest.test_a_first_argument_the_rule_lets_through_is_no_command)
        self.assertTrue(sudo_allows("start setup"))
        self.assertTrue(sudo_allows("start --force"))


class CallerTest(ActivityTest):
    def through_sudo(self, user, *args):
        """agentbox <args> as `sudo -n agentbox` run by user: SUDO_USER and SUDO_UID set by sudo."""
        return self.run_agentbox(*args, SUDO_USER=user, SUDO_UID="990" if user == "agentbox-web" else "1000")

    def refused(self, command, *args):
        """The --json failure of agentbox <command> --actor=… <args> through sudo by agentbox-web,
        as the backend passes them; nothing ran."""
        code, out, err = self.through_sudo("agentbox-web", command, f"--actor={FIREFOX}", *args, "--json")
        self.assertEqual((code, out), (1, ""), err)
        said = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(said["error"], "control_room_refused")
        self.assertEqual(self.calls(), [], "a program ran")
        self.assertEqual(self.entries(), [], "recorded in Activity")
        return said

    def test_commands_outside_the_control_rooms_set_are_refused(self):
        for command in sorted(set(cli.COMMANDS) - cli.CONTROL_ROOM_COMMANDS):
            with self.subTest(command=command):
                said = self.refused(command)
                self.assertEqual(said["params"], {"command": command})
                self.assertEqual(said["message"], f"{command} is not for the Control Room: only the CLI at the "
                                                  "Host runs it")
        # with arguments that would do something, and with --actor before the command in both
        # forms (which the sudoers rule would not let through)
        for args in (["import", "work", REPO], ["unset-env", "work", "A"], ["regenerate"], ["setup", "--force"],
                     ["version"], ["--version"]):
            for actor in ([f"--actor={FIREFOX}"], ["--actor", FIREFOX]):
                with self.subTest(args=args, actor=actor):
                    code, _, err = self.through_sudo("agentbox-web", *actor, *args, "--json")
                    self.assertEqual(code, 1, err)
                    self.assertEqual(json.loads(err.strip().splitlines()[-1])["error"], "control_room_refused")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.entries(), [])

    def test_a_first_argument_the_rule_lets_through_is_no_command(self):
        """sudo joins the arguments, so "start setup" as one argument passes the rule; agentbox
        takes it as the command it is not, and only shows its usage."""
        for first in ("start setup", "start --force", "allow work evil.example --force"):
            with self.subTest(first=first):
                code, out, err = self.through_sudo("agentbox-web", first, f"--actor={FIREFOX}", "--json")
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, cli.USAGE + "\n")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.entries(), [])
        self.assertFalse(self.allowlist_exists())

    def test_force_is_refused(self):
        """ADR 0009: the Control Room never lifts strong Signals, here not even if its backend
        were taken over."""
        for args in (["allow", "work", "evil.example", "--force"], ["allow", "work", "evil.example", "--force=1"],
                     ["extend", "all", "evil.example", "--for=8h", "--force"],
                     ["make-permanent", "work", "evil.example", "--force"],
                     ["watch-remove", "webhook.site", "--force"],
                     ["watch-add", "webhook.site", "--category=paste", "--force"],
                     ["legitimate-add", "evil.example", "--force"],
                     # also where the command would read it as the value of an option
                     ["allow", "work", "a.example", "--note", "--force"]):
            with self.subTest(args=args):
                said = self.refused(*args)
                self.assertEqual(said["params"], {"command": args[0], "option": "--force"})
        self.assertFalse(self.allowlist_exists())

    def test_the_control_rooms_commands_run(self):
        code, out, err = self.through_sudo("agentbox-web", "allow", f"--actor={FIREFOX}", "work", "a.example",
                                           "--note=--force", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["note"], "--force")
        self.assertEqual(self.through_sudo("agentbox-web", "close-web", f"--actor={FIREFOX}", "work")[0], 0)
        self.assertEqual([(e["AGENTBOX_ACTION"], e["AGENTBOX_ACTOR"]) for e in self.entries()],
                         [("allow", FIREFOX), ("close-web", FIREFOX)])
        # without arguments: status
        self.assertEqual(self.through_sudo("agentbox-web")[0], 0)

    def test_anyone_else_through_sudo_is_not_limited(self):
        code, _, err = self.through_sudo("alice", "allow", "work", "evil.example", "--force")
        self.assertEqual(code, 0, err)
        code, _, err = self.through_sudo("alice", "version")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
