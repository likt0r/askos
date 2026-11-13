"""Credentials a VM gets from the Host: its GitHub owners, its Claude token and its
OpenCode key.

owners / set-owners manage the GitHub owners in the VM's registry record, which
the GitHub token service reads. set-claude-token checks a token from `claude
setup-token` and stores it in <CONF>/secrets/claude-<vm>, from where the VM
reads it at start (fw_cfg) as CLAUDE_CODE_OAUTH_TOKEN. set-opencode-key does the
same for an API key from the OpenCode console (opencode.ai, for OpenCode Go or Zen):
<CONF>/secrets/opencode-<vm>, in the VM OPENCODE_API_KEY.

Checking a token: its form (sk-ant-..., only the characters such tokens have),
then one minimal request to Anthropic's Messages API with it. Anthropic
documents no endpoint for checking such OAuth tokens, so the error type of the
answer decides (https://platform.claude.com/docs/en/api/errors): an
authentication, permission or billing error means the token does not work; an
answer, or an error that only comes once the token was accepted (invalid
request, not found, rate limit), means it does; anything else (no answer,
overloaded, an HTML page) proves nothing, and nothing is stored. Only then is
the old token replaced, with one rename that keeps the file's mode and group.
The token never goes onto a command line (curl reads the header from stdin),
into output or into Activity.

Checking an OpenCode key: its form (sk-..., the same characters; Claude tokens and
keys refused), then GET /models of OpenCode Go's API with it, which costs nothing:
a list of models means opencode.ai accepts the key, an authentication or permission
error that it does not, anything else proves nothing and stores nothing. Whether the
account has a Go subscription is not checked. The rest is the Claude token's way.
"""
import json
import os
import re
import stat
import subprocess
import sys

from . import registry, state_files
from .errors import die
from .registry import check
from .sanitize import clean
from .state_files import claude_token_file, opencode_key_file

OWNER = re.compile(r"[A-Za-z0-9-]{1,39}")
ANTHROPIC = "https://api.anthropic.com/v1/messages"
CHECK_BODY = '{"model":"claude-haiku-5-5","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}'
# what `tr -d '[:space:]'` removes; NUL because a shell variable cannot hold it
WHITESPACE = b" \t\n\r\v\f\0"
# sk-ant-oat01-... from `claude setup-token`: nothing that could end a header line
CLAUDE_TOKEN = re.compile(rb"sk-ant-[A-Za-z0-9_-]{1,500}")
OPENCODE_MODELS = "https://opencode.ai/inference/go/openai/v1/models"
# an API key from the OpenCode console: the same characters, so nothing can end a header line
OPENCODE_KEY = re.compile(rb"sk-[A-Za-z0-9_-]{1,500}")
OPENCODE_REFUSED = {"authentication_error", "permission_error"}
# Anthropic's error types: the token does not work ...
TOKEN_REFUSED = {"authentication_error", "permission_error", "billing_error"}
# ... or it was accepted, and only the request itself or its rate was not
TOKEN_ACCEPTED = {"invalid_request_error", "not_found_error", "rate_limit_error", "request_too_large"}


def owners(args):
    """agentbox owners <vm> [--json]"""
    names = registry.load(args[0] if args else "")["github_owners"]
    if "--json" in args[1:]:
        print(json.dumps(names, ensure_ascii=False))
    else:
        print("".join(o + "\n" for o in names), end="")


def set_owners(args):
    """agentbox set-owners <vm> <owner>... [--json]"""
    ctx = check(args[0] if args else "")
    names = [a for a in args[1:] if a != "--json"]
    if not names:
        die("at least one GitHub owner", error="github_owner_required", vm=ctx)
    for o in names:
        if not OWNER.fullmatch(o):
            die(f"invalid GitHub name: {o}", error="invalid_github_owner", owner=o)
    with state_files.locked(registry.record_file(ctx)):
        registry.save({**registry.load(ctx), "github_owners": names})
    if "--json" in args[1:]:
        print(json.dumps({"vm": ctx, "owners": names}, ensure_ascii=False))
    else:
        print(f"{ctx}: GitHub owners set: {' '.join(names)}")


def anthropic_answer(token):
    """Body of Anthropic's answer to a minimal request with this token; empty when
    curl fails, so a broken connection never counts as a valid token."""
    argv = ["curl", "-s", "-m", "20", ANTHROPIC,
            "-H", "@-",                  # the Authorization header, from stdin
            "-H", "anthropic-version: 2023-06-01",
            "-H", "anthropic-beta: oauth-2025-04-20",
            "-H", "content-type: application/json",
            "-d", CHECK_BODY]
    header = b"Authorization: Bearer " + token + b"\n"
    try:
        r = subprocess.run(argv, input=header, stdout=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return r.stdout if r.returncode == 0 else b""


def token_verdict(answer):
    """"valid", "refused" or "unknown" for Anthropic's answer (see the module notes)."""
    try:
        obj = json.loads(answer, strict=False)
    except ValueError:
        return "unknown"
    if not isinstance(obj, dict):
        return "unknown"
    if obj.get("type") == "message":
        return "valid"
    error = obj.get("error")
    kind = error.get("type") if isinstance(error, dict) else None
    if kind in TOKEN_REFUSED:
        return "refused"
    return "valid" if kind in TOKEN_ACCEPTED else "unknown"


def _secret_file(ctx, f):
    """The stat of the VM's secret file, which agentbox regenerate creates."""
    try:
        return os.stat(f)
    except FileNotFoundError:
        die(f"{f} is missing, run agentbox setup first", error="not_set_up", vm=ctx)
    except OSError as e:
        die(f"{f}: {e.strerror}", error="action_failed")


def _read_secret(ctx, as_json, args, prompt):
    if "--no-prompt" not in args[1:]:
        # with --json stdout carries only the result
        print(f"{prompt} for {ctx}, then Enter and Ctrl+D:", file=sys.stderr if as_json else sys.stdout, flush=True)
    # read every line: long tokens often arrive wrapped after copying
    return sys.stdin.buffer.read().translate(None, WHITESPACE)


def _shown(answer, token):
    """The answer for people: at most 200 characters, never the token, no control characters."""
    shown = answer.replace(token, b"[token]")[:200] if answer else b"no answer"
    return clean(shown.decode("utf-8", "replace"), False)


def _store(ctx, f, before, token, as_json, what):
    with state_files.locked(f):
        # one rename: a VM starting meanwhile reads the old or the new one, never half of one
        state_files.write_atomic(f, token + b"\n", stat.S_IMODE(before.st_mode),
                                 before.st_gid if os.geteuid() == 0 else -1)
    length = len(token.decode("utf-8", "surrogateescape"))
    if as_json:
        print(json.dumps({"vm": ctx, "stored": True, "length": length}))
    else:
        print(f"{what} valid and stored ({length} characters). Takes effect after: agentbox restart {ctx}")


def set_claude_token(args):
    """agentbox set-claude-token <vm> [--no-prompt] [--json]  (token on stdin)"""
    ctx = check(args[0] if args else "")
    as_json = "--json" in args[1:]
    f = claude_token_file(ctx)
    before = _secret_file(ctx, f)
    token = _read_secret(ctx, as_json, args, "Paste the token from 'claude setup-token'")
    if not token.startswith(b"sk-ant-"):
        die("this does not look like a Claude token (sk-ant-...)", error="invalid_claude_token")
    if token.startswith(b"sk-ant-api"):
        die("this is an API key; the VM needs a token from 'claude setup-token' (sk-ant-oat...)",
            error="invalid_claude_token")
    if not CLAUDE_TOKEN.fullmatch(token):
        die("the token holds unexpected characters or is too long", error="invalid_claude_token")
    # check with Anthropic before storing: the old token stays unless the new one works
    answer = anthropic_answer(token).replace(b"\0", b"").rstrip(b"\n")
    verdict = token_verdict(answer) if answer else "refused"
    if verdict != "valid":
        shown = _shown(answer, token)
        if verdict == "refused":
            die("Anthropic rejects the token: " + shown, error="claude_token_rejected", vm=ctx)
        die("Anthropic answered unexpectedly, token not stored (try again later): " + shown,
            error="anthropic_unavailable", vm=ctx)
    _store(ctx, f, before, token, as_json, "Token")

def opencode_answer(key):
    """Body of opencode.ai's answer to GET /models with this key; empty when curl fails."""
    argv = ["curl", "-s", "-m", "20", OPENCODE_MODELS, "-H", "@-"]    # the Authorization header, from stdin
    try:
        r = subprocess.run(argv, input=b"Authorization: Bearer " + key + b"\n", stdout=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return r.stdout if r.returncode == 0 else b""


def key_verdict(answer):
    """"valid", "refused" or "unknown" for opencode.ai's answer (see the module notes)."""
    try:
        obj = json.loads(answer, strict=False)
    except ValueError:
        return "unknown"
    if not isinstance(obj, dict):
        return "unknown"
    if obj.get("object") == "list" and isinstance(obj.get("data"), list):
        return "valid"
    error = obj.get("error")
    return "refused" if isinstance(error, dict) and error.get("type") in OPENCODE_REFUSED else "unknown"


def set_opencode_key(args):
    """agentbox set-opencode-key <vm> [--no-prompt] [--json]  (key on stdin)"""
    ctx = check(args[0] if args else "")
    as_json = "--json" in args[1:]
    f = opencode_key_file(ctx)
    before = _secret_file(ctx, f)
    key = _read_secret(ctx, as_json, args, "Paste the API key from opencode.ai")
    if key.startswith(b"sk-ant-"):
        die("this is a Claude token or key; OpenCode needs an API key from opencode.ai",
            error="invalid_opencode_key")
    if not key.startswith(b"sk-"):
        die("this does not look like an OpenCode key (sk-...)", error="invalid_opencode_key")
    if not OPENCODE_KEY.fullmatch(key):
        die("the key holds unexpected characters or is too long", error="invalid_opencode_key")
    # check with opencode.ai before storing: the old key stays unless the new one works
    answer = opencode_answer(key).replace(b"\0", b"").rstrip(b"\n")
    verdict = key_verdict(answer) if answer else "refused"
    if verdict != "valid":
        shown = _shown(answer, key)
        if verdict == "refused":
            die("opencode.ai rejects the key: " + shown, error="opencode_key_rejected", vm=ctx)
        die("opencode.ai answered unexpectedly, key not stored (try again later): " + shown,
            error="opencode_unavailable", vm=ctx)
    _store(ctx, f, before, key, as_json, "Key")


COMMANDS = {"owners": owners, "set-owners": set_owners, "set-claude-token": set_claude_token,
            "set-opencode-key": set_opencode_key}
