"""The GitHub App behind the token service: replacing its ID and key, where it
is installed and which VMs rely on that, and the tokens each VM got.

set-github-app checks a new App ID and private key with GitHub before they
replace the working ones: it signs a JWT with the new key (RS256, like the token
service) and asks GET /app, whose answer must name the same App. Only then are
app.pem (owner agentbox-gh, mode 0400) and app.conf (group agentbox-gh, 0640)
replaced, each with one rename. The token service starts a new process for
every request and reads both files then, so the next request uses them without
a restart.

The key comes in on stdin and reaches openssl through a pipe (/dev/fd), every
JWT reaches curl on stdin: neither ever stands on a command line, in output, in
an error message or in Activity.

github-app compares the App's installations (GET /app/installations) with the
GitHub owners of every VM; github-tokens shows what a VM asked the token service
for, read from its log the way Activity reads it.
"""
import base64
import binascii
import datetime
import json
import os
import pwd
import re
import subprocess
import sys
import time

from . import activity, paths, registry, state_files
from .errors import die
from .sanitize import clean, redact

API = "https://api.github.com"
CONF_DIR = os.path.join(paths.CONF, "github")
KEY_FILE = os.path.join(CONF_DIR, "app.pem")
CONF_FILE = os.path.join(CONF_DIR, "app.conf")
SERVICE_USER = "agentbox-gh"         # the token service; only it reads the key (rpm/agentbox.sysusers)
APP_ID = re.compile(r"[1-9][0-9]{0,11}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
MAX_KEY = 16 * 1024                  # a 4096-bit RSA key in PEM is about 3.3 KiB
RSA = bytes.fromhex("06092a864886f70d010101")   # DER of the OID rsaEncryption
PER_PAGE, MAX_PAGES = 100, 20
DEFAULT_LINES = 1000


# --- GitHub ------------------------------------------------------------------

def _openssl(argv, data, pass_fds=()):
    """openssl's output, or None when it fails. Its messages are not shown: they
    say nothing the caller's message does not."""
    try:
        r = subprocess.run(["openssl", *argv], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=30, pass_fds=pass_fds)
    except FileNotFoundError:
        die("openssl is missing", 127, error="action_failed", program="openssl")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def _sign_with_key(key):
    """A signer using this key (PEM bytes), handed to openssl through a pipe."""
    def sign(data):
        r, w = os.pipe()
        try:
            os.write(w, key)             # fits into the pipe's buffer (MAX_KEY)
            os.close(w)
            w = -1
            return _openssl(["dgst", "-sha256", "-sign", f"/dev/fd/{r}", "-passin", "pass:"], data, (r,))
        finally:
            os.close(r)
            if w != -1:
                os.close(w)
    return sign


def _sign_with_file(data):
    return _openssl(["dgst", "-sha256", "-sign", KEY_FILE, "-passin", "pass:"], data)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _jwt(app_id, sign):
    """The App's JWT for GitHub (as the token service makes it), or None."""
    now = int(time.time())
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signature = sign(head + b"." + claims)
    return head + b"." + claims + b"." + _b64url(signature) if signature else None


def _get(path, jwt):
    """(HTTP status, JSON or None) of GET <API><path> with the JWT; status 0 when
    GitHub was not reached. The JWT reaches curl on stdin, never its arguments."""
    argv = ["curl", "-s", "-m", "20", "--proto", "=https", "--max-filesize", "4194304",
            "-H", "@-", "-w", "\\n%{http_code}", API + path]
    headers = (b"Authorization: Bearer " + jwt + b"\n"
               b"Accept: application/vnd.github+json\nX-GitHub-Api-Version: 2022-11-28\nUser-Agent: agentbox\n")
    try:
        r = subprocess.run(argv, input=headers, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except FileNotFoundError:
        die("curl is missing", 127, error="action_failed", program="curl")
    except (OSError, subprocess.TimeoutExpired):
        return 0, None
    body, _, status = r.stdout.rpartition(b"\n")
    if r.returncode != 0 or not re.fullmatch(rb"[1-5][0-9][0-9]", status):
        return 0, None
    try:
        return int(status), json.loads(body)
    except ValueError:
        return int(status), None


def _message(answer, jwt):
    """GitHub's error message for people: one clean line, nothing secret."""
    m = answer.get("message") if isinstance(answer, dict) else None
    if not isinstance(m, str) or not m.strip():
        return "no usable answer"
    return redact(m.replace(jwt.decode(), "[JWT]"))[:200]


def _text(v, limit=100):
    return clean(v, False)[:limit] if isinstance(v, str) else None


def _app(answer, app_id):
    """The App as GitHub describes it, with the link for installing it elsewhere."""
    slug = answer.get("slug") if isinstance(answer.get("slug"), str) and SLUG.fullmatch(answer["slug"]) else None
    owner = answer.get("owner") if isinstance(answer.get("owner"), dict) else {}
    return {"id": int(app_id), "slug": slug, "name": _text(answer.get("name")), "owner": _text(owner.get("login")),
            "html_url": f"https://github.com/apps/{slug}" if slug else None,
            "install_url": f"https://github.com/apps/{slug}/installations/new" if slug else None}


# --- set-github-app ----------------------------------------------------------

def _service_user():
    """(uid, gid) of the token service's user when running as root, else (-1, -1)."""
    if os.geteuid() != 0:
        return -1, -1
    try:
        pw = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        die(f"user {SERVICE_USER} is missing (the package creates it)", error="action_failed", user=SERVICE_USER)
    return pw.pw_uid, pw.pw_gid


def _private_key(raw):
    """The pasted key as openssl writes it (PKCS#8 PEM), or die. Line ends and
    indentation from copying do not matter."""
    text = b"\n".join(line.strip() for line in raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"))
    if b"ENCRYPTED" in text:
        die("the key is encrypted; it takes the .pem file from GitHub as it is", error="invalid_private_key")
    pem = _openssl(["pkey", "-passin", "pass:"], text)
    m = re.fullmatch(rb"-----BEGIN PRIVATE KEY-----\n([A-Za-z0-9+/=\n]+)-----END PRIVATE KEY-----\n", pem or b"")
    try:
        der = base64.b64decode(m.group(1).replace(b"\n", b""), validate=True) if m else b""
    except (binascii.Error, ValueError):
        der = b""
    if not der:
        die("this is not a private key in PEM format (the GitHub App's .pem file)", error="invalid_private_key")
    if RSA not in der[:32]:
        die("this is not an RSA key; GitHub Apps sign with RSA", error="invalid_private_key")
    return pem


def _conf_with(app_id):
    """app.conf with this APP_ID; any other lines stay."""
    try:
        with open(CONF_FILE, "rb") as f:
            lines = [l for l in state_files.lines(f.read()) if not l.startswith(b"APP_ID=")]
    except FileNotFoundError:
        lines = []
    except OSError as e:
        die(f"{CONF_FILE}: {e.strerror}", error="action_failed", path=CONF_FILE)
    return b"".join(l + b"\n" for l in [b"APP_ID=" + app_id.encode(), *lines])


def set_github_app(args):
    """agentbox set-github-app <app-id> [--no-prompt] [--json]  (private key on stdin)"""
    flags = {"--no-prompt", "--json"}
    given = [a for a in args if a not in flags]
    # nothing of the arguments is echoed: one of them might be a key pasted by mistake
    if len(given) != 1:
        die("usage: agentbox set-github-app <app-id> [--no-prompt] [--json], the key on stdin", 2,
            error="invalid_argument")
    app_id = given[0]
    if not APP_ID.fullmatch(app_id):
        die("invalid App ID: a number, as GitHub shows it under App ID in the App's settings", 2,
            error="invalid_app_id")
    as_json = "--json" in args
    if not os.path.isdir(CONF_DIR):
        die(f"{CONF_DIR} is missing (the package creates it)", error="action_failed", path=CONF_DIR)
    uid, gid = _service_user()
    if "--no-prompt" not in args:
        # with --json stdout carries only the result
        print("Paste the GitHub App's private key (.pem), then press Enter and Ctrl+D:",
              file=sys.stderr if as_json else sys.stdout, flush=True)
    raw = sys.stdin.buffer.read(MAX_KEY + 1)
    if len(raw) > MAX_KEY:
        die("too long for a private key", error="invalid_private_key")
    key = _private_key(raw)
    jwt = _jwt(app_id, _sign_with_key(key))
    if jwt is None:
        die("the key cannot sign; nothing changed", error="invalid_private_key")
    status, answer = _get("/app", jwt)
    if status == 0:
        die("GitHub is not reachable; nothing changed", error="github_unavailable")
    if status != 200 or not isinstance(answer, dict):
        die(f"GitHub rejects the App ID or key (HTTP {status}: {_message(answer, jwt)}); nothing changed",
            error="github_app_rejected", status=status)
    if answer.get("id") != int(app_id):
        die("GitHub names another App for this key; nothing changed", error="github_app_rejected")
    app = _app(answer, app_id)
    with state_files.locked(KEY_FILE):
        # the key first: a request in between gets the new key with the old ID, if
        # the ID changed at all, and is refused by GitHub; the next one works
        state_files.write_atomic(KEY_FILE, key, 0o400, gid, uid)
        state_files.write_atomic(CONF_FILE, _conf_with(app_id), 0o640, gid)
    if as_json:
        print(json.dumps({**app, "stored": True}, ensure_ascii=False))
    else:
        print(f"GitHub App {app_id} ({app['slug'] or '?'}) checked and saved. "
              f"The token service uses it from the next request on.")


# --- github-app --------------------------------------------------------------

def _stored_app_id():
    try:
        with open(CONF_FILE, "rb") as f:
            conf = dict(l.split(b"=", 1) for l in state_files.lines(f.read()) if b"=" in l)
    except FileNotFoundError:
        conf = {}
    except OSError as e:
        die(f"{CONF_FILE}: {e.strerror}", error="action_failed", path=CONF_FILE)
    app_id = conf.get(b"APP_ID", b"").strip().decode("ascii", "replace")
    if not APP_ID.fullmatch(app_id) or not os.path.exists(KEY_FILE):
        die("no GitHub App is set up: agentbox set-github-app", error="github_app_missing")
    return app_id


def _call(path, jwt):
    status, answer = _get(path, jwt)
    if status == 0:
        die("GitHub is not reachable", error="github_unavailable")
    if status != 200:
        die(f"GitHub answers {path.split('?')[0]} with HTTP {status}: {_message(answer, jwt)}",
            error="github_unavailable", path=path.split("?")[0], status=status)
    return answer


def _installation(i):
    account = i.get("account") if isinstance(i.get("account"), dict) else {}
    url = i.get("html_url")
    return {"id": i.get("id") if isinstance(i.get("id"), int) and not isinstance(i.get("id"), bool) else None,
            "account": _text(account.get("login")), "type": _text(account.get("type"), 20),
            "repository_selection": _text(i.get("repository_selection"), 20),
            "suspended": bool(i.get("suspended_at")),
            "html_url": _text(url, 300) if isinstance(url, str) and url.startswith("https://github.com/") else None}


def overview():
    """The App, its installations with the VMs relying on each, and the GitHub
    owners of VMs without an installation."""
    app_id = _stored_app_id()
    jwt = _jwt(app_id, _sign_with_file)
    if jwt is None:
        die(f"{KEY_FILE}: cannot sign with it (a new key with agentbox set-github-app)",
            error="invalid_private_key")
    app = _call("/app", jwt)
    if not isinstance(app, dict) or app.get("id") != int(app_id):
        die("GitHub names another App than app.conf", error="github_app_rejected")
    found = []
    for page in range(1, MAX_PAGES + 1):
        batch = _call(f"/app/installations?per_page={PER_PAGE}&page={page}", jwt)
        if not isinstance(batch, list):
            die("unexpected answer from GitHub to /app/installations", error="github_unavailable",
                path="/app/installations")
        found += [_installation(i) for i in batch if isinstance(i, dict)]
        if len(batch) < PER_PAGE:
            break
    installed = {i["account"].lower(): i for i in found if i["account"]}
    for i in found:
        i["vms"] = []
    missing = {}
    records, problems = registry.scan()
    for name, problem in problems:
        print(f"agentbox: {problem}", file=sys.stderr, flush=True)
    for rec in sorted(records, key=lambda r: r["name"]):
        for owner in rec["github_owners"]:
            if owner.lower() in installed:
                vms = installed[owner.lower()]["vms"]
            else:
                vms = missing.setdefault(owner.lower(), {"owner": owner, "vms": []})["vms"]
            if rec["name"] not in vms:
                vms.append(rec["name"])
    return {"app": _app(app, app_id), "installations": sorted(found, key=lambda i: (i["account"] or "").lower()),
            "missing": sorted(missing.values(), key=lambda m: m["owner"].lower())}


def github_app(args):
    """agentbox github-app [--json]: the App, where it is installed, which VMs rely on that."""
    if any(a != "--json" for a in args):
        die("usage: agentbox github-app [--json]", 2, error="invalid_argument")
    result = overview()
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False))
        return
    app = result["app"]
    print(f"GitHub App {app['id']} {app['slug'] or '?'} ({app['name'] or '?'}), owned by {app['owner'] or '?'}")
    print(f"Install link: {app['install_url'] or '-'}")
    print("Installed on:" if result["installations"] else "Not installed anywhere.")
    selection = {"all": "all repos", "selected": "selected repos"}
    for i in result["installations"]:
        print(f"  {i['account'] or '?':<20} {i['type'] or '?':<13} "
              f"{selection.get(i['repository_selection'], i['repository_selection'] or '?'):<15} "
              f"VMs: {', '.join(i['vms']) or '-'}" + ("  [suspended]" if i["suspended"] else ""))
    if result["missing"]:
        print("Missing Installations (these VMs get no tokens for these GitHub owners):")
        for m in result["missing"]:
            print(f"  {m['owner']:<20} VMs: {', '.join(m['vms'])}")


# --- github-tokens -------------------------------------------------------------

OUTCOMES = {"github.token": "issued", "github.refused": "refused", "github.error": "error"}


def _token_options(args):
    """--since, --until and --lines as for `agentbox activity`, and --json."""
    opts, as_json, i = {}, False, 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--json":
            as_json = True
            continue
        name, eq, val = a.partition("=")
        if name not in ("--since", "--until", "--lines"):
            die("unknown option: " + clean(a, False)[:60], 2, error="invalid_argument", option=clean(a, False)[:60])
        if not eq:
            if i >= len(args):
                die(f"{name} needs a value", 2, error="invalid_argument", option=name)
            val, i = args[i], i + 1
        if not activity.CHECKS[name[2:]](val):
            die(f"invalid value for {name}: " + clean(val, False)[:60], 2, error="invalid_argument", option=name)
        opts[name[2:]] = val
    return opts, as_json


def tokens(vm, opts):
    """What the VM asked the token service for, newest first, and per repository
    how many tokens it got and how often it was refused."""
    entries, truncated, days = activity.timeline({"lines": str(DEFAULT_LINES), **opts, "kind": "github", "vm": vm})
    requests, repos = [], {}
    for e in entries:
        if e["action"] not in OUTCOMES:
            continue
        outcome = OUTCOMES[e["action"]]
        r = {"time": e["time"], "outcome": outcome, "repository": e.get("_repository"),
             "request": e.get("_request"), "reason": e.get("_reason"), "reason_code": e.get("_code"),
             "reason_params": e.get("_code_params") or {}, "expires_at": e.get("_expires")}
        requests.append(r)
        if r["repository"]:
            # entries come newest first: the first spelling seen is the latest
            s = repos.setdefault(r["repository"].lower(), {"repository": r["repository"], "tokens": 0,
                                                            "last_token": None, "refused": 0, "last_refused": None})
            key = "tokens" if outcome == "issued" else "refused"
            s[key] += 1
            last = "last_token" if outcome == "issued" else "last_refused"
            s[last] = s[last] or r["time"]
    summary = sorted(repos.values(), key=lambda s: max(s["last_token"] or "", s["last_refused"] or ""), reverse=True)
    return {"vm": vm, "repositories": summary, "requests": requests, "truncated": truncated, "retention_days": days}


def _local(iso):
    """An entry's time (UTC, ISO 8601) in the Host's local time, for text output."""
    try:
        return datetime.datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return iso or "?"


def github_tokens(args):
    """agentbox github-tokens <vm> [--since T] [--until T] [--lines N] [--json]"""
    vm = registry.check(args[0] if args else "")
    opts, as_json = _token_options(args[1:])
    result = tokens(vm, opts)
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    issued = [s for s in result["repositories"] if s["tokens"]]
    print(f"{vm}: tokens for:" if issued else f"{vm}: no tokens issued")
    for s in issued:
        print(f"  {s['repository']:<40} {s['tokens']}x, last {_local(s['last_token'])}")
    refused = [r for r in result["requests"] if r["outcome"] != "issued"]
    if refused:
        print("Refused:")
    for r in refused:
        what = r["request"] if r["request"] is not None else "?"
        print(f"  {_local(r['time'])}  {what}: {r['reason'] or ''}")
    if result["truncated"]:
        print("agentbox: there are more entries (raise --lines or narrow --since)", file=sys.stderr)


COMMANDS = {"set-github-app": set_github_app, "github-app": github_app, "github-tokens": github_tokens}
