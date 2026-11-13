"""`agentbox owners`, `set-owners`, `set-claude-token` and `set-opencode-key` against a
temporary root and a fake `curl` that stands in for Anthropic and opencode.ai.

Run from the repo root: python3 -m unittest discover -s tests
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTBOX = os.path.join(ROOT, "bin", "agentbox")

# Stand-in for curl: answers from scenario.json ({"answer": "...", "exit": 0}) and
# records each call in calls.jsonl, with headers from `-H @-` read from stdin.
FAKE_CURL = '''#!{python}
import json, os, sys
DIR = {dir!r}
argv = sys.argv[1:]
stdin = sys.stdin.read()
headers, data, url, max_time, i = [], None, None, None, 0
while i < len(argv):
    a = argv[i]
    if a == "-H":
        v = argv[i + 1]
        headers += [l for l in stdin.splitlines() if l] if v == "@-" else [v]
        i += 2
    elif a == "-d":
        data, i = argv[i + 1], i + 2
    elif a == "-m":
        max_time, i = argv[i + 1], i + 2
    elif a.startswith("-"):
        i += 1
    else:
        url, i = a, i + 1
with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
    f.write(json.dumps({{"argv": argv, "stdin": stdin, "url": url, "max_time": max_time,
                        "headers": headers, "data": data}}) + "\\n")
with open(os.path.join(DIR, "scenario.json")) as f:
    s = json.load(f)
sys.stdout.write(s.get("answer", ""))
sys.exit(s.get("exit", 0))
'''

TOKEN = "sk-ant-oat01-AbCdEf-123ghi_JKL"
# as pasted: wrapped over several lines, with blanks, tabs and CRLF
PASTED = "  sk-ant-oat01-AbC\r\ndEf-123\n\tghi_JKL \n"
OK_ANSWER = '{"id":"msg_01","type":"message","role":"assistant","content":[{"type":"text","text":"Hi"}]}'
AUTH_ERROR = '{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'
PERMISSION_ERROR = '{"type":"error","error":{"type":"permission_error","message":"not allowed"}}'
RATE_LIMIT = '{"type":"error","error":{"type":"rate_limit_error","message":"Number of requests has exceeded your rate limit"}}'
BILLING_ERROR = '{"type":"error","error":{"type":"billing_error","message":"Your credit balance is too low"}}'
CLAUDE_CODE_ONLY = ('{"type":"error","error":{"type":"invalid_request_error","message":"This credential is only '
                    'authorized for use with Claude Code and cannot be used for other API requests."}}')
OLD_TOKEN = "sk-ant-oat01-Old-working-token_000"
PROMPT = "Paste the token from 'claude setup-token' for priv, then Enter and Ctrl+D:\n"
STORED = "Token valid and stored (30 characters). Takes effect after: agentbox restart priv\n"


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.conf = os.path.join(self.dir, "etc", "agentbox")
        os.makedirs(os.path.join(self.conf, "github"))
        os.makedirs(os.path.join(self.conf, "secrets"))
        vm_records.write(self.dir)
        os.mkdir(os.path.join(self.dir, "bin"))
        curl = os.path.join(self.dir, "bin", "curl")
        with open(curl, "w") as f:
            f.write(FAKE_CURL.format(python=sys.executable, dir=self.dir))
        os.chmod(curl, 0o755)
        self.scenario(answer=OK_ANSWER)

    def tearDown(self):
        self.tmp.cleanup()

    def scenario(self, **s):
        with open(os.path.join(self.dir, "scenario.json"), "w") as f:
            json.dump(s, f)

    def run_agentbox(self, *args, stdin=""):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "AGENTBOX_ROOT": self.dir, "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], input=stdin, capture_output=True, text=True,
                           env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def curl_calls(self):
        try:
            with open(os.path.join(self.dir, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def path(self, *parts):
        return os.path.join(self.conf, *parts)

    def write(self, rel, text, mode=0o640):
        with open(self.path(*rel.split("/")), "w") as f:
            f.write(text)
        os.chmod(self.path(*rel.split("/")), mode)

    def read(self, rel):
        with open(self.path(*rel.split("/"))) as f:
            return f.read()

    def mode(self, rel):
        return stat.S_IMODE(os.stat(self.path(*rel.split("/"))).st_mode)


class OwnersTest(CredentialsTest):
    def owners(self, vm, owners):
        vm_records.write(self.dir, vm_records.record(vm, {"work": 1, "priv": 2}[vm], github_owners=owners))

    def test_lists_the_owners_of_the_record(self):
        self.owners("work", ["alice", "my-org"])
        self.assertEqual(self.run_agentbox("owners", "work"), (0, "alice\nmy-org\n", ""))

    def test_json_is_a_list(self):
        self.owners("work", ["alice", "my-org"])
        code, out, err = self.run_agentbox("owners", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), ["alice", "my-org"])

    def test_no_owners(self):
        self.assertEqual(self.run_agentbox("owners", "priv"), (0, "", ""))
        code, out, _ = self.run_agentbox("owners", "priv", "--json")
        self.assertEqual((code, json.loads(out)), (0, []))

    def test_legacy_owners_file_is_ignored(self):
        self.write("github/owners-work", "# alt\nold-owner\n")
        self.assertEqual(self.run_agentbox("owners", "work"), (0, "", ""))

    def test_unknown_vm(self):
        self.assertEqual(self.run_agentbox("owners", "test"), (1, "", "agentbox: No such VM: test\n"))
        self.assertEqual(self.run_agentbox("owners"), (1, "", "agentbox: no VM given\n"))


class SetOwnersTest(CredentialsTest):
    def record(self, vm):
        return vm_records.read(self.dir, vm)

    def test_sets_the_owners_in_the_record(self):
        before = self.record("work")
        self.assertEqual(self.run_agentbox("set-owners", "work", "alice", "my-org"),
                         (0, "work: GitHub owners set: alice my-org\n", ""))
        self.assertEqual(self.record("work"), {**before, "github_owners": ["alice", "my-org"]})
        self.assertEqual(self.mode("vms/work.json"), 0o644)
        self.assertEqual(sorted(os.listdir(self.path("vms"))), ["priv.json", "work.json"])
        self.assertEqual(self.run_agentbox("owners", "work"), (0, "alice\nmy-org\n", ""))

    def test_replaces_previous_owners(self):
        vm_records.write(self.dir, vm_records.record("priv", 2, github_owners=["old-owner"]))
        self.assertEqual(self.run_agentbox("set-owners", "priv", "Bob-42"),
                         (0, "priv: GitHub owners set: Bob-42\n", ""))
        self.assertEqual(self.record("priv")["github_owners"], ["Bob-42"])

    def test_json(self):
        code, out, err = self.run_agentbox("set-owners", "work", "alice", "my-org", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"vm": "work", "owners": ["alice", "my-org"]})
        self.assertEqual(self.record("work")["github_owners"], ["alice", "my-org"])

    def test_name_of_39_characters_is_valid(self):
        name = "a" * 39
        self.assertEqual(self.run_agentbox("set-owners", "work", name),
                         (0, f"work: GitHub owners set: {name}\n", ""))

    def test_rejects_invalid_names_and_keeps_the_record(self):
        vm_records.write(self.dir, vm_records.record("work", 1, github_owners=["old-owner"]))
        before = self.read("vms/work.json")
        for bad in ("bad_name", "a" * 40, "org/repo", "", "alice\nmallory", "ä"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_agentbox("set-owners", "work", "alice", bad),
                                 (1, "", f"agentbox: invalid GitHub name: {bad}\n"))
                self.assertEqual(self.read("vms/work.json"), before)

    def test_needs_at_least_one_owner(self):
        before = self.read("vms/work.json")
        self.assertEqual(self.run_agentbox("set-owners", "work"), (1, "", "agentbox: at least one GitHub owner\n"))
        code, out, err = self.run_agentbox("set-owners", "work", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err), {"error": "github_owner_required", "message": "at least one GitHub owner",
                                           "params": {"vm": "work"}})
        self.assertEqual(self.read("vms/work.json"), before)

    def test_unknown_vm(self):
        self.assertEqual(self.run_agentbox("set-owners", "test", "alice"),
                         (1, "", "agentbox: No such VM: test\n"))
        self.assertEqual(sorted(os.listdir(self.path("vms"))), ["priv.json", "work.json"])


class SetClaudeTokenTest(CredentialsTest):
    def setUp(self):
        super().setUp()
        self.write("secrets/claude-priv", "none\n", mode=0o640)

    def assert_not_printed(self, out, err):
        self.assertNotIn(TOKEN, out + err)
        self.assertNotIn("AbCdEf", out + err)

    def test_stores_token_without_whitespace(self):
        code, out, err = self.run_agentbox("set-claude-token", "priv", stdin=PASTED)
        self.assertEqual((code, out, err), (0, PROMPT + STORED, ""))
        self.assertEqual(self.read("secrets/claude-priv"), TOKEN + "\n")
        self.assertEqual(self.mode("secrets/claude-priv"), 0o640)

    def test_checks_token_with_anthropic(self):
        self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED)
        [call] = self.curl_calls()
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["max_time"], "20")
        self.assertIn("-s", call["argv"])
        self.assertEqual(sorted(call["headers"]), sorted([
            f"Authorization: Bearer {TOKEN}",
            "anthropic-version: 2023-06-01",
            "anthropic-beta: oauth-2025-04-20",
            "content-type: application/json",
        ]))
        self.assertEqual(json.loads(call["data"]), {
            "model": "claude-haiku-5-5", "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}]})

    def test_token_is_not_on_the_command_line(self):
        self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED)
        [call] = self.curl_calls()
        self.assertFalse([a for a in call["argv"] if "sk-ant-" in a or "AbCdEf" in a], call["argv"])
        self.assertIn(TOKEN, call["stdin"])

    def test_no_prompt(self):
        self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED),
                         (0, STORED, ""))
        self.assertEqual(self.read("secrets/claude-priv"), TOKEN + "\n")

    def test_json(self):
        code, out, err = self.run_agentbox("set-claude-token", "priv", "--no-prompt", "--json", stdin=PASTED)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"vm": "priv", "stored": True, "length": 30})
        self.assertEqual(self.read("secrets/claude-priv"), TOKEN + "\n")

    def test_json_keeps_prompt_off_stdout(self):
        code, out, err = self.run_agentbox("set-claude-token", "priv", "--json", stdin=PASTED)
        self.assertEqual((code, err), (0, PROMPT))
        self.assertEqual(json.loads(out), {"vm": "priv", "stored": True, "length": 30})

    def test_rejects_text_without_prefix(self):
        for pasted in ("abc-sk-ant-123", "", "  \n", "sk-ant"):
            with self.subTest(pasted=pasted):
                self.assertEqual(
                    self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=pasted),
                    (1, "", "agentbox: this does not look like a Claude token (sk-ant-...)\n"))
        self.assertEqual(self.curl_calls(), [])
        self.assertEqual(self.read("secrets/claude-priv"), "none\n")

    def test_rejects_authentication_and_permission_errors(self):
        for answer in (AUTH_ERROR, PERMISSION_ERROR):
            with self.subTest(answer=answer):
                self.scenario(answer=answer)
                code, out, err = self.run_agentbox("set-claude-token", "priv", stdin=PASTED)
                self.assertEqual((code, out, err),
                                 (1, PROMPT, f"agentbox: Anthropic rejects the token: {answer}\n"))
                self.assertEqual(self.read("secrets/claude-priv"), "none\n")

    def test_rejection_shows_at_most_200_characters(self):
        answer = '{"type":"error","error":{"type":"authentication_error","message":"' + "x" * 300 + '"}}'
        self.scenario(answer=answer + "\n")
        code, _, err = self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED)
        self.assertEqual((code, err), (1, "agentbox: Anthropic rejects the token: " + answer[:200] + "\n"))

    def test_rejects_empty_answer(self):
        for answer in ("", "\n\n"):
            with self.subTest(answer=answer):
                self.scenario(answer=answer)
                self.assertEqual(
                    self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED),
                    (1, "", "agentbox: Anthropic rejects the token: no answer\n"))
                self.assertEqual(self.read("secrets/claude-priv"), "none\n")

    def test_failed_request_is_a_rejection(self):
        # curl aborted (time limit, no connection), possibly after part of an answer
        for answer in ("", '{"type":"mess'):
            with self.subTest(answer=answer):
                self.scenario(answer=answer, exit=28)
                self.assertEqual(
                    self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED),
                    (1, "", "agentbox: Anthropic rejects the token: no answer\n"))
                self.assertEqual(self.read("secrets/claude-priv"), "none\n")

    def test_secret_file_must_exist(self):
        os.remove(self.path("secrets", "claude-priv"))
        missing = os.path.join(self.dir, "etc/agentbox/secrets/claude-priv")
        self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED),
                         (1, "", f"agentbox: {missing} is missing, run agentbox setup first\n"))
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(self.curl_calls(), [])

    def test_renewing_replaces_the_working_token_in_one_rename(self):
        self.write("secrets/claude-priv", OLD_TOKEN + "\n", mode=0o640)
        self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED), (0, STORED, ""))
        self.assertEqual(self.read("secrets/claude-priv"), TOKEN + "\n")
        self.assertEqual(self.mode("secrets/claude-priv"), 0o640)
        self.assertEqual(os.listdir(self.path("secrets")), ["claude-priv"])

    def test_a_refused_token_keeps_the_working_one(self):
        self.write("secrets/claude-priv", OLD_TOKEN + "\n", mode=0o640)
        for answer in (AUTH_ERROR, PERMISSION_ERROR, BILLING_ERROR):
            with self.subTest(answer=answer):
                self.scenario(answer=answer)
                self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED),
                                 (1, "", f"agentbox: Anthropic rejects the token: {answer}\n"))
                self.assertEqual(self.read("secrets/claude-priv"), OLD_TOKEN + "\n")
                self.assertEqual(self.mode("secrets/claude-priv"), 0o640)

    def test_errors_that_come_after_the_token_was_accepted_mean_valid(self):
        """Anthropic checks the token first: refusing the request itself (as it does for
        subscription tokens outside Claude Code), its model or its rate says the token works."""
        for answer in (CLAUDE_CODE_ONLY, '{"type":"error","error":{"type":"not_found_error","message":"model"}}',
                       RATE_LIMIT):
            with self.subTest(answer=answer):
                self.write("secrets/claude-priv", OLD_TOKEN + "\n", mode=0o640)
                self.scenario(answer=answer)
                self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED),
                                 (0, STORED, ""))
                self.assertEqual(self.read("secrets/claude-priv"), TOKEN + "\n")

    def test_answers_that_prove_nothing_store_nothing(self):
        self.write("secrets/claude-priv", OLD_TOKEN + "\n", mode=0o640)
        for answer in ('{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
                       '{"type":"error","error":{"type":"api_error","message":"Internal server error"}}',
                       "<html><body>502 Bad Gateway</body></html>", "[]", '{"type":"mess', '{"error":"x"}'):
            with self.subTest(answer=answer):
                self.scenario(answer=answer)
                self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED),
                                 (1, "", "agentbox: Anthropic answered unexpectedly, token not stored "
                                         f"(try again later): {answer}\n"))
                self.assertEqual(self.read("secrets/claude-priv"), OLD_TOKEN + "\n")

    def test_control_characters_in_the_answer_are_removed(self):
        self.scenario(answer='{"type":"error","error":{"type":"authentication_error","message":"a\x1b[31m‮b"}}')
        _, _, err = self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=PASTED)
        self.assertEqual(err, 'agentbox: Anthropic rejects the token: {"type":"error","error":'
                              '{"type":"authentication_error","message":"a[31mb"}}\n')

    def test_api_key_is_not_a_token_for_the_vm(self):
        self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin="sk-ant-api03-Abc_12-x\n"),
                         (1, "", "agentbox: this is an API key; the VM needs a token from "
                                 "'claude setup-token' (sk-ant-oat...)\n"))
        self.assertEqual(self.curl_calls(), [])
        self.assertEqual(self.read("secrets/claude-priv"), "none\n")

    def test_rejects_unexpected_characters_without_echoing_them(self):
        for pasted in ("sk-ant-oat01-abc\x01def", "sk-ant-oat01-ab:cd", "sk-ant-oat01-aäb", "sk-ant-",
                       "sk-ant-oat01-" + "a" * 500, "sk-ant-oat01-ab\x00cd%0d%0aX-Evil: 1"):
            with self.subTest(pasted=pasted):
                self.assertEqual(self.run_agentbox("set-claude-token", "priv", "--no-prompt", stdin=pasted),
                                 (1, "", "agentbox: the token holds unexpected characters or is too long\n"))
        self.assertEqual(self.curl_calls(), [])
        self.assertEqual(self.read("secrets/claude-priv"), "none\n")

    def test_never_prints_the_token(self):
        cases = [(OK_ANSWER, ()), (OK_ANSWER, ("--json",)), (AUTH_ERROR, ()),
                 # even when an error answer repeats it
                 ('{"error":{"type":"authentication_error","message":"bad token ' + TOKEN + '"}}', ())]
        for answer, extra in cases:
            with self.subTest(answer=answer, extra=extra):
                self.scenario(answer=answer)
                _, out, err = self.run_agentbox("set-claude-token", "priv", *extra, stdin=PASTED)
                self.assert_not_printed(out, err)
        os.remove(self.path("secrets", "claude-priv"))
        self.scenario(answer=OK_ANSWER)
        _, out, err = self.run_agentbox("set-claude-token", "priv", stdin=PASTED)
        self.assert_not_printed(out, err)

    def test_unknown_vm(self):
        self.assertEqual(self.run_agentbox("set-claude-token", "test", "--no-prompt", stdin=PASTED),
                         (1, "", "agentbox: No such VM: test\n"))
        self.assertEqual(self.curl_calls(), [])


OC_KEY = "sk-Oc12AbCdEf_34-ghiJKLmn"
OC_PASTED = "  sk-Oc12AbC\r\ndEf_34-\n\tghiJKLmn \n"
OC_MODELS = '{"object":"list","data":[{"id":"glm-5","object":"model","owned_by":"opencode"}]}'
OC_INVALID = '{"error":{"type":"authentication_error","message":"Invalid credential"}}'
OC_OLD = "sk-Old-working-key_000"
OC_PROMPT = "Paste the API key from opencode.ai for priv, then Enter and Ctrl+D:\n"
OC_STORED = "Key valid and stored (25 characters). Takes effect after: agentbox restart priv\n"


class SetOpencodeKeyTest(CredentialsTest):
    def setUp(self):
        super().setUp()
        self.write("secrets/opencode-priv", "none\n", mode=0o640)
        self.scenario(answer=OC_MODELS)

    def set_key(self, *extra, stdin=OC_PASTED):
        return self.run_agentbox("set-opencode-key", "priv", *extra, stdin=stdin)

    def test_stores_the_key_without_whitespace(self):
        self.assertEqual(self.set_key(), (0, OC_PROMPT + OC_STORED, ""))
        self.assertEqual(self.read("secrets/opencode-priv"), OC_KEY + "\n")
        self.assertEqual(self.mode("secrets/opencode-priv"), 0o640)

    def test_checks_the_key_with_opencode_go_models(self):
        self.set_key("--no-prompt")
        [call] = self.curl_calls()
        self.assertEqual(call["url"], "https://opencode.ai/inference/go/openai/v1/models")
        self.assertEqual(call["max_time"], "20")
        self.assertIsNone(call["data"])          # a GET: listing the models costs nothing
        self.assertEqual(call["headers"], [f"Authorization: Bearer {OC_KEY}"])
        self.assertFalse([a for a in call["argv"] if "Oc12" in a], call["argv"])

    def test_json(self):
        code, out, err = self.set_key("--no-prompt", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"vm": "priv", "stored": True, "length": 25})

    def test_json_keeps_prompt_off_stdout(self):
        code, out, err = self.set_key("--json")
        self.assertEqual((code, err), (0, OC_PROMPT))
        self.assertEqual(json.loads(out)["stored"], True)

    def test_rejects_what_is_no_opencode_key(self):
        for pasted, said in (("", "this does not look like an OpenCode key (sk-...)"),
                             ("abc-sk-123", "this does not look like an OpenCode key (sk-...)"),
                             ("sk-ant-oat01-Abc", "this is a Claude token or key; OpenCode needs an API key from opencode.ai"),
                             ("sk-ant-api03-Abc", "this is a Claude token or key; OpenCode needs an API key from opencode.ai"),
                             ("sk-", "the key holds unexpected characters or is too long"),
                             ("sk-ab:cd", "the key holds unexpected characters or is too long"),
                             ("sk-ab\x00cd%0d%0aX-Evil: 1", "the key holds unexpected characters or is too long"),
                             ("sk-" + "a" * 501, "the key holds unexpected characters or is too long")):
            with self.subTest(pasted=pasted):
                self.assertEqual(self.set_key("--no-prompt", stdin=pasted), (1, "", f"agentbox: {said}\n"))
        self.assertEqual(self.curl_calls(), [])
        self.assertEqual(self.read("secrets/opencode-priv"), "none\n")

    def test_a_refused_key_keeps_the_working_one(self):
        self.write("secrets/opencode-priv", OC_OLD + "\n", mode=0o640)
        for answer in (OC_INVALID, '{"error":{"type":"permission_error","message":"no"}}'):
            with self.subTest(answer=answer):
                self.scenario(answer=answer)
                self.assertEqual(self.set_key("--no-prompt"),
                                 (1, "", f"agentbox: opencode.ai rejects the key: {answer}\n"))
                self.assertEqual(self.read("secrets/opencode-priv"), OC_OLD + "\n")
                self.assertEqual(self.mode("secrets/opencode-priv"), 0o640)

    def test_no_answer_proves_nothing(self):
        """No connection, a time limit or half an answer say nothing about the key."""
        self.write("secrets/opencode-priv", OC_OLD + "\n", mode=0o640)
        for answer, exit_code, shown in (("", 0, "no answer"), ("", 28, "no answer"), ('{"object":"li', 28, "no answer")):
            with self.subTest(answer=answer, exit=exit_code):
                self.scenario(answer=answer, exit=exit_code)
                self.assertEqual(self.set_key("--no-prompt"),
                                 (1, "", "agentbox: opencode.ai answered unexpectedly, key not stored "
                                         f"(try again later): {shown}\n"))
                self.assertEqual(self.read("secrets/opencode-priv"), OC_OLD + "\n")

    def test_answers_that_prove_nothing_store_nothing(self):
        self.write("secrets/opencode-priv", OC_OLD + "\n", mode=0o640)
        for answer in ('{"type":"error","error":{"type":"MissingSessionID","message":"x"}}',
                       '{"object":"list"}', "<html>502 Bad Gateway</html>", "[]", '{"error":"x"}'):
            with self.subTest(answer=answer):
                self.scenario(answer=answer)
                self.assertEqual(self.set_key("--no-prompt"),
                                 (1, "", "agentbox: opencode.ai answered unexpectedly, key not stored "
                                         f"(try again later): {answer}\n"))
                self.assertEqual(self.read("secrets/opencode-priv"), OC_OLD + "\n")

    def test_renewing_replaces_the_working_key_in_one_rename(self):
        self.write("secrets/opencode-priv", OC_OLD + "\n", mode=0o640)
        self.assertEqual(self.set_key("--no-prompt"), (0, OC_STORED, ""))
        self.assertEqual(self.read("secrets/opencode-priv"), OC_KEY + "\n")
        self.assertEqual(os.listdir(self.path("secrets")), ["opencode-priv"])

    def test_never_prints_the_key(self):
        for answer, extra in ((OC_MODELS, ()), (OC_MODELS, ("--json",)), (OC_INVALID, ()),
                              ('{"error":{"type":"authentication_error","message":"bad ' + OC_KEY + '"}}', ())):
            with self.subTest(answer=answer, extra=extra):
                self.scenario(answer=answer)
                _, out, err = self.set_key(*extra)
                self.assertNotIn(OC_KEY, out + err)
                self.assertNotIn("Oc12", out + err)

    def test_secret_file_must_exist(self):
        os.remove(self.path("secrets", "opencode-priv"))
        missing = os.path.join(self.dir, "etc/agentbox/secrets/opencode-priv")
        self.assertEqual(self.set_key("--no-prompt"),
                         (1, "", f"agentbox: {missing} is missing, run sudo agentbox regenerate first\n"))
        self.assertEqual(self.curl_calls(), [])

    def test_the_claude_token_stays_as_it_is(self):
        self.write("secrets/claude-priv", OLD_TOKEN + "\n", mode=0o640)
        self.assertEqual(self.set_key("--no-prompt"), (0, OC_STORED, ""))
        self.assertEqual(self.read("secrets/claude-priv"), OLD_TOKEN + "\n")

    def test_unknown_vm(self):
        self.assertEqual(self.run_agentbox("set-opencode-key", "test", "--no-prompt", stdin=OC_PASTED),
                         (1, "", "agentbox: No such VM: test\n"))
        self.assertEqual(self.curl_calls(), [])


if __name__ == "__main__":
    unittest.main()
