#!/usr/bin/env python3
"""Stand-in for `curl` asking the npm registry and GitHub what is available
(agentbox/versions.py, `agentbox fetch-versions`) and for the downloads of the Build's
vm/build/orca-release, in tests.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "curl", "argv": [...]};
             what the servers know is registry.json there

registry.json:
  npm        {"<package>": the metadata GET https://registry.npmjs.org/<package> answers
              (scoped names escaped as @scope%2fname), or {"status": 500} for an error}
  github     {"<owner>/<repo>": the releases of the repository, newest first, as GitHub's API
              lists them (GET https://api.github.com/repos/<owner>/<repo>/releases?per_page=N&page=P,
              pages of N), or {"status": 500} for an error. GET .../releases/tags/<tag> answers
              the release with that tag_name unless it is a draft, else 404}
  downloads  {"<url>": "<path of a file with its content>" or {"status": 404}}: what curl -o
              writes for that URL (-f: an error status fails with exit 22)
             {"flood": true} instead: a body without length that never ends, whatever
             --max-filesize says; -o - writes to stdout (a pipe) instead of a file
  exit       end with this exit code and no answer (no connection)
Packages, repositories and URLs not named there get 404. Understands what agentbox passes:
-s, -S, -f, -L, -m, --connect-timeout, --max-redirs, --proto and --proto-redir (which must
be =https), --max-filesize (a download larger than that fails with exit 63), -H, -o, -w with
\\n and %{http_code}, and the URL.
"""
import json
import os
import shutil
import sys
from urllib.parse import parse_qs, unquote, urlparse

DIR = os.environ["FAKE_DIR"]
WITH_VALUE = {"-H", "-w", "-m", "-o", "--proto", "--proto-redir", "--max-filesize", "--connect-timeout",
              "--max-redirs"}


def releases(u, known):
    """(status, body) of GitHub's releases API, None for another path."""
    parts = u.path.split("/")
    if len(parts) < 5 or parts[:2] != ["", "repos"] or parts[4] != "releases":
        return None
    found = known.get("github", {}).get(f"{parts[2]}/{parts[3]}")
    if found is None:
        return 404, {"message": "Not Found"}
    if isinstance(found, dict) and set(found) == {"status"}:
        return found["status"], {"message": "failed"}
    if len(parts) == 5:
        query = parse_qs(u.query)
        per_page = int(query.get("per_page", ["30"])[0])
        page = int(query.get("page", ["1"])[0])
        return 200, found[(page - 1) * per_page:page * per_page]
    if len(parts) == 7 and parts[5] == "tags":
        tag = unquote(parts[6])
        match = [r for r in found if r.get("tag_name") == tag and not r.get("draft")]
        return (200, match[0]) if match else (404, {"message": "Not Found"})
    return 404, {"message": "Not Found"}


def answer(url, known):
    u = urlparse(url)
    if u.scheme != "https":
        return None
    if u.netloc == "registry.npmjs.org":
        found = known.get("npm", {}).get(unquote(u.path[1:]))
    elif u.netloc == "api.github.com":
        return releases(u, known)
    else:
        return None
    if found is None:
        return 404, {"error": "Not found"}
    if set(found) == {"status"}:
        return found["status"], {"error": "failed"}
    return 200, found


def download(url, dest, opts, known):
    """curl -o dest url: exit code."""
    if urlparse(url).scheme != "https":
        return 1                # unsupported protocol (--proto =https)
    found = known.get("downloads", {}).get(url)
    if found is None:
        found = {"status": 404}
    if isinstance(found, dict) and found.get("flood"):
        try:
            with (sys.stdout.buffer if dest == "-" else open(dest, "wb")) as out:
                while True:
                    out.write(b"x" * (1 << 20))
                    out.flush()
        except (BrokenPipeError, OSError):
            return 23
    if isinstance(found, dict):
        if "-f" in opts:
            print(f"curl: (22) The requested URL returned error: {found['status']}", file=sys.stderr)
            return 22
        with open(dest, "w") as f:
            f.write("Not Found")
        return 0
    cap = int(opts["--max-filesize"][0]) if "--max-filesize" in opts else None
    if cap is not None and os.path.getsize(found) > cap:
        print("curl: (63) Exceeded the maximum allowed file size", file=sys.stderr)
        return 63
    if dest == "-":
        with open(found, "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    else:
        shutil.copyfile(found, dest)
    return 0


def main(argv):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "curl", "argv": argv}) + "\n")
    try:
        with open(os.path.join(DIR, "registry.json")) as f:
            known = json.load(f)
    except FileNotFoundError:
        known = {}
    opts, url, i = {}, None, 0
    while i < len(argv):
        if argv[i] in WITH_VALUE:
            opts.setdefault(argv[i], []).append(argv[i + 1])
            i += 2
        elif argv[i].startswith("-"):
            opts[argv[i]] = []
            i += 1
        else:
            url, i = argv[i], i + 1
    if known.get("exit"):
        return known["exit"]
    if opts.get("--proto-redir") != ["=https"]:
        print("curl (fake): agentbox must allow only HTTPS redirects", file=sys.stderr)
        return 2
    if known.get("flood"):             # a chunked body that never ends, whatever --max-filesize says
        try:
            while True:
                sys.stdout.write("x" * (1 << 20))
                sys.stdout.flush()
        except (BrokenPipeError, OSError):
            return 23
    if opts.get("--proto") != ["=https"]:
        print("curl (fake): agentbox must allow only HTTPS", file=sys.stderr)
        return 2
    if "-o" in opts:
        return download(url or "", opts["-o"][0], opts, known)
    found = answer(url or "", known)
    if found is None:
        return 6             # could not resolve host
    status, body = found
    sys.stdout.write(json.dumps(body))
    if opts.get("-w"):
        sys.stdout.write(opts["-w"][0].replace("\\n", "\n").replace("%{http_code}", str(status)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
