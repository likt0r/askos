"""Credentials a VM gets from the Host: its GitHub owners and its Claude token.

owners / set-owners manage the GitHub owners in the VM's registry record, which
the GitHub token service reads. set-claude-token checks a token from `claude
setup-token` and stores it in <CONF>/secrets/claude-<vm>, from where the VM
reads it at start (fw_cfg) as CLAUDE_CODE_OAUTH_TOKEN.

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
"""
import json
import os
import re
import stat
import subprocess
import sys

from . import activity, registry, state_files
from .errors import die
from .registry import check
from .sanitize import clean
from .state_files import claude_token_file

OWNER = re.compile(r"[A-Za-z0-9-]{1,39}")
ANTHROPIC = "https://api.anthropic.com/v1/messages"
CHECK_BODY = '{"model":"claude-haiku-5-5","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}'
# what `tr -d '[:space:]'` removes; NUL because a shell variable cannot hold it
WHITESPACE = b" \t\n\r\v\f\0"
# sk-ant-oat01-... from `claude setup-token`: nothing that could end a header line
CLAUDE_TOKEN = re.compile(rb"sk-ant-[A-Za-z0-9_-]{1,500}")
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


def set_claude_token(args):
    """agentbox set-claude-token <vm> [--no-prompt] [--json]  (token on stdin)"""
    ctx = check(args[0] if args else "")
    as_json = "--json" in args[1:]
    f = claude_token_file(ctx)
    try:
        before = os.stat(f)
    except FileNotFoundError:
        die(f"{f} is missing, run agentbox setup first", error="not_set_up", vm=ctx)
    except OSError as e:
        die(f"{f}: {e.strerror}", error="action_failed")
    if "--no-prompt" not in args[1:]:
        # with --json stdout carries only the result
        print(f"Paste the token from 'claude setup-token' for {ctx}, then Enter and Ctrl+D:",
              file=sys.stderr if as_json else sys.stdout, flush=True)
    # read every line: long tokens often arrive wrapped after copying
    token = sys.stdin.buffer.read().translate(None, WHITESPACE)
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
        shown = answer.replace(token, b"[token]")[:200] if answer else b"no answer"
        shown = clean(shown.decode("utf-8", "replace"), False)
        if verdict == "refused":
            die("Anthropic rejects the token: " + shown, error="claude_token_rejected", vm=ctx)
        die("Anthropic answered unexpectedly, token not stored (try again later): " + shown,
            error="anthropic_unavailable", vm=ctx)
    with state_files.locked(f):
        # one rename: a VM starting meanwhile reads the old or the new token, never half of one
        state_files.write_atomic(f, token + b"\n", stat.S_IMODE(before.st_mode),
                                 before.st_gid if os.geteuid() == 0 else -1)
    length = len(token.decode("utf-8", "surrogateescape"))
    if as_json:
        print(json.dumps({"vm": ctx, "stored": True, "length": length}))
    else:
        print(f"Token valid and stored ({length} characters). Takes effect after: agentbox restart {ctx}")


from .commands import declare

DECLARATIONS = {
    "owners": declare(owners, "agentbox owners <vm> [--json]", options={"json": False}, read_only=True, order=0),
    "set-owners": declare(set_owners, "agentbox set-owners <vm> <owner>... [--json]",
                          options={"json": False}, control_room=True, kind="credentials",
                          describe=activity._owners, order=109),
    "set-claude-token": declare(set_claude_token, "agentbox set-claude-token <vm> [--no-prompt] [--json]",
                                 options={"no-prompt": False, "json": False}, control_room=True,
                                 kind="credentials", describe=lambda a: activity._said("Set the Claude token"),
                                 order=26),
}
