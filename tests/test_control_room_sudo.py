"""What the Control Room may run through sudo (#50): its sudoers rule lets agentbox-web run
agentbox with any arguments, so agentbox itself, called through sudo by agentbox-web, runs only
the Control Room's commands (cli.CONTROL_ROOM_COMMANDS) and never --force, before anything
runs.

BackendTest reads web/agentbox-web and finds every agentbox command it runs, which must be
exactly that set. CallerTest runs the real CLI as test_activity.py does, with SUDO_USER as
sudo sets it.

Run from the repo root: python3 -m unittest discover -s tests
"""
import ast
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_activity import FIREFOX, REPO, ActivityTest  # noqa: E402

sys.path.insert(0, REPO)
from agentbox import cli  # noqa: E402

SERVER = os.path.join(REPO, "web", "agentbox-web")
# the backend's functions that run agentbox with the command as their first argument
RUNNERS = ("agentbox", "agentbox_json", "run", "run_json")


def backend_commands():
    """{command: [line, ...]} of every agentbox command web/agentbox-web runs. A call whose
    command cannot be told from the source fails the test instead of being left out."""
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
        """*args of a runner, handed on unchanged (agentbox_json -> agentbox, run_json -> run)."""
        f = function(node)
        return (isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name) and f is not None
                and f.name in RUNNERS and f.args.vararg is not None and arg.value.id == f.args.vararg.arg)

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
            # [*AGENTBOX, <the actor>, <command>, ...]: the argv itself
            starred, argv = parent.get(node), parent.get(parent.get(node))
            if not (isinstance(starred, ast.Starred) and isinstance(argv, ast.List)):
                unknown.append(node.lineno)
                continue
            rest = [e for e in argv.elts[argv.elts.index(starred) + 1:] if "--actor=" not in ast.unparse(e)]
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


class BackendTest(unittest.TestCase):
    def test_the_backend_runs_exactly_the_control_rooms_commands(self):
        """A command the backend runs but the set lacks would be refused through sudo; one the set
        has but the backend no longer runs would be allowed for nothing."""
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


class CallerTest(ActivityTest):
    def through_sudo(self, user, *args):
        """agentbox <args> as `sudo -n agentbox` run by user: SUDO_USER and SUDO_UID set by sudo."""
        return self.run_agentbox(*args, SUDO_USER=user, SUDO_UID="990" if user == "agentbox-web" else "1000")

    def refused(self, *args):
        """The --json failure of agentbox <args> through sudo by agentbox-web; nothing ran."""
        code, out, err = self.through_sudo("agentbox-web", f"--actor={FIREFOX}", *args, "--json")
        self.assertEqual((code, out), (1, ""), err)
        said = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(said["error"], "sudo_failed")
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
        # with arguments that would do something, and with --actor as two arguments
        for args in (["import", "work", REPO], ["unset-env", "work", "A"],
                     ["regenerate"], ["setup", "--force"], ["version"], ["--version"]):
            with self.subTest(args=args):
                code, _, err = self.through_sudo("agentbox-web", "--actor", FIREFOX, *args, "--json")
                self.assertEqual(code, 1, err)
                self.assertEqual(json.loads(err.strip().splitlines()[-1])["error"], "sudo_failed")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.entries(), [])

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
        code, out, err = self.through_sudo("agentbox-web", f"--actor={FIREFOX}", "allow", "work", "a.example",
                                           "--note=--force", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["note"], "--force")
        self.assertEqual(self.through_sudo("agentbox-web", f"--actor={FIREFOX}", "close-web", "work")[0], 0)
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
