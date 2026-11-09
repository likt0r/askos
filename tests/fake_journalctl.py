#!/usr/bin/env python3
"""Stand-in for `journalctl` reading the Host's journal as JSON in tests.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "journalctl", "argv": [...]};
             the journal is journal.json there: a list of entries as `journalctl -o json`
             prints them (__REALTIME_TIMESTAMP in microseconds, as a string)

Understands what agentbox uses: --identifier/-t (alternatives), FIELD=VALUE matches
(different fields must all match, values of one field are alternatives),
--since/--until=@EPOCH, --grep (PCRE-like, case-insensitive when the pattern has no
capitals; no match ends with 1 like the real one), --reverse, --lines, --field,
--after-cursor (the entries after the one with that __CURSOR, in time order),
--unit/-u (glob on _SYSTEMD_UNIT); --no-pager, --output and --output-fields change
nothing here. An entry with "_FAKE_NAMESPACE" belongs to that journald namespace (null:
the default journal) and is only seen by calls reading it (--namespace=name, or
--namespace=+name for both); entries without it are seen by every call.

--follow/-f: after printing, watches journal.json (written whole, by os.replace) and
prints every entry appended later that matches, until its parent process ends.

scenario.json:
  journalctl_exit   exit with this code and a message on stderr
"""
import fnmatch
import json
import os
import re
import sys
import time

DIR = os.environ["FAKE_DIR"]


def main(argv):
    # the journal as it is when the call starts, read before the call is recorded: a test
    # that waits for the recorded call of a follower (--follow --lines=0) and then writes
    # an entry gets it reported, not taken for one that was there before
    try:
        with open(os.path.join(DIR, "journal.json")) as f:
            journal = json.load(f)
    except FileNotFoundError:
        journal = []
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "journalctl", "argv": argv}) + "\n")
    try:
        with open(os.path.join(DIR, "scenario.json")) as f:
            code = json.load(f).get("journalctl_exit")
    except FileNotFoundError:
        code = None
    if code:
        print("Failed to open journal: Permission denied", file=sys.stderr)
        return code
    idents, matches, since, until, grep, reverse, lines, field = [], {}, None, None, None, False, None, None
    after, units, namespace, follow = None, [], None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a == "-t":
            idents.append(argv[i])
            i += 1
        elif a.startswith("--identifier="):
            idents.append(a.split("=", 1)[1])
        elif a.startswith("--since=@"):
            since = int(a[len("--since=@"):]) * 10**6
        elif a.startswith("--until=@"):
            until = int(a[len("--until=@"):]) * 10**6
        elif a.startswith("--grep="):
            grep = a.split("=", 1)[1]
        elif a in ("--reverse", "-r"):
            reverse = True
        elif a.startswith("--lines="):
            lines = int(a.split("=", 1)[1])
        elif a.startswith("--field="):
            field = a.split("=", 1)[1]
        elif a.startswith("--after-cursor="):
            after = a.split("=", 1)[1]
        elif a.startswith("--unit="):
            units.append(a.split("=", 1)[1])
        elif a == "-u":
            units.append(argv[i])
            i += 1
        elif a.startswith("--namespace="):
            value = a.split("=", 1)[1]
            namespace = {value.lstrip("+"), None} if value.startswith("+") else {value}
        elif a in ("--follow", "-f"):
            follow = True
        elif a.startswith("-"):
            continue
        else:
            key, _, value = a.partition("=")
            matches.setdefault(key, []).append(value)
    if field:
        for v in sorted({e[field] for e in journal if field in e}):
            print(v)
        return 0
    def wanted(e):
        t = int(e["__REALTIME_TIMESTAMP"])
        if "_FAKE_NAMESPACE" in e and e["_FAKE_NAMESPACE"] not in (namespace or {None}):
            return False
        if units and not any(fnmatch.fnmatchcase(e.get("_SYSTEMD_UNIT", ""), u) for u in units):
            return False
        if idents and e.get("SYSLOG_IDENTIFIER") not in idents:
            return False
        if any(e.get(k) not in vs for k, vs in matches.items()):
            return False
        if since is not None and t < since or until is not None and t > until:
            return False
        if grep and not re.search(grep, e.get("MESSAGE", ""), 0 if re.search("[A-Z]", grep) else re.I):
            return False
        return True

    out = [e for e in journal if wanted(e)]
    if after is not None:
        ordered = sorted(journal, key=lambda e: int(e["__REALTIME_TIMESTAMP"]))
        position = next((i for i, e in enumerate(ordered) if e.get("__CURSOR") == after), None)
        if position is None:
            print("Failed to seek to cursor: Invalid argument", file=sys.stderr)
            return 1
        later = {id(e) for e in ordered[position + 1:]}
        out = [e for e in out if id(e) in later]
    out.sort(key=lambda e: int(e["__REALTIME_TIMESTAMP"]), reverse=reverse)
    if lines is not None:
        out = out[:lines] if reverse else out[len(out) - lines:]
    for e in out:
        print(json.dumps(e))
    if follow:
        sys.stdout.flush()
        parent, seen = os.getppid(), len(journal)
        while os.getppid() == parent:
            time.sleep(0.05)
            try:
                with open(os.path.join(DIR, "journal.json")) as f:
                    journal = json.load(f)
            except (FileNotFoundError, ValueError):
                continue
            for e in journal[seen:]:
                if wanted(e):
                    print(json.dumps(e), flush=True)
            seen = len(journal)
        return 0
    return 1 if grep and not out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
