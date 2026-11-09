#!/usr/bin/env python3
"""Stand-in for `curl` talking to GitHub's REST API (agentbox/github.py) in tests.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "curl", "argv": [...], "stdin": "..."};
             the App GitHub knows is github.json there

github.json:
  app_id          the App's ID: a JWT must name it as iss
  public_key      path of the PEM public key GitHub has for the App: a JWT must be signed
                  with its private key (checked with the real openssl)
  app             the answer to GET /app (default: an App with that ID)
  installations   what GET /app/installations pages through (default [])
  status, answer  answer every request with this status and body instead (JSON; a string as it is)
  exit            end with this exit code and no answer (no connection)

Like GitHub, a JWT that is malformed, not RS256, issued in the future, expired, valid
for more than 10 minutes, for another App or not signed with the App's key gets 401.
Understands what agentbox passes: -H @- (headers from stdin), -H, -w with \\n and
%{http_code}, -s, -m, --proto, --max-filesize, and the URL.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse

DIR = os.environ["FAKE_DIR"]
WITH_VALUE = {"-H", "-w", "-m", "--proto", "--max-filesize"}


def b64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def jwt_problem(jwt, gh):
    """Why GitHub would refuse this JWT, or None."""
    try:
        head, claims, signature = jwt.split(".")
        header, payload, sig = json.loads(b64(head)), json.loads(b64(claims)), b64(signature)
    except (ValueError, TypeError):
        return "A JSON web token could not be decoded"
    if header.get("alg") != "RS256":
        return "A JSON web token could not be decoded"
    now = time.time()
    iat, exp = payload.get("iat"), payload.get("exp")
    if not isinstance(iat, int) or iat > now + 1:
        return "'Issued at' claim ('iat') must be an Integer representing a time in the past."
    if not isinstance(exp, int) or exp <= now or exp - iat > 600:
        return "'Expiration time' claim ('exp') is too far in the future."
    if str(payload.get("iss")) != str(gh["app_id"]):
        return "Integration not found"
    with tempfile.NamedTemporaryFile(dir=DIR) as f:
        f.write(sig)
        f.flush()
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", gh["public_key"], "-signature", f.name],
                           input=f"{head}.{claims}".encode(), capture_output=True)
    return None if r.returncode == 0 else "A JSON web token could not be decoded"


def answer(url, headers, gh):
    if "status" in gh:
        return gh["status"], gh.get("answer")
    auth = [h for h in headers if h.lower().startswith("authorization: bearer ")]
    if len(auth) != 1:
        return 401, {"message": "Requires authentication"}
    problem = jwt_problem(auth[0].split(" ", 2)[2], gh)
    if problem:
        return 401, {"message": problem, "documentation_url": "https://docs.github.com/rest"}
    u = urlparse(url)
    if u.scheme != "https" or u.netloc != "api.github.com":
        return 404, {"message": "Not Found"}
    if u.path == "/app":
        return 200, gh.get("app", {"id": gh["app_id"], "slug": "agentbox-test", "name": "Agent Box Test",
                                   "owner": {"login": "likt0r", "type": "User"},
                                   "html_url": "https://github.com/apps/agentbox-test"})
    if u.path == "/app/installations":
        q = parse_qs(u.query)
        per_page, page = int(q.get("per_page", ["30"])[0]), int(q.get("page", ["1"])[0])
        found = gh.get("installations", [])
        return 200, found[(page - 1) * per_page:page * per_page]
    return 404, {"message": "Not Found"}


def main(argv):
    stdin = sys.stdin.read()
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "curl", "argv": argv, "stdin": stdin}) + "\n")
    with open(os.path.join(DIR, "github.json")) as f:
        gh = json.load(f)
    if gh.get("exit"):
        return gh["exit"]
    headers, write_out, url, i = [], "", None, 0
    while i < len(argv):
        a = argv[i]
        if a in WITH_VALUE:
            v = argv[i + 1]
            if a == "-H":
                headers += [l for l in stdin.splitlines() if l] if v == "@-" else [v]
            elif a == "-w":
                write_out = v
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            url, i = a, i + 1
    status, body = answer(url, headers, gh)
    # a string is sent as it is (an HTML page), anything else as JSON
    sys.stdout.write(body if isinstance(body, str) else json.dumps(body) if body is not None else "")
    sys.stdout.write(write_out.replace("\\n", "\n").replace("%{http_code}", str(status)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
