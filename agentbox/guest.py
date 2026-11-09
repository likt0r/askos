"""A VM's journal through its guest agent (ADR 0004).

journalctl runs inside the VM via guest-exec, through a wrapper in the Image
that only starts journalctl with a time limit (no shell). The Host neither
reads the VM's disk nor freezes it.

Exit codes: 2 invalid option, 3 VM not running, 4 guest agent not answering,
5 time limit, 6 journalctl failing in the VM, 7 no pairing code.
"""
import base64
import datetime
import json
import os
import re
import sys
import time

from . import paths, state_files, virsh
from .registry import check, dom
from .errors import Fail, die
from .sanitize import clean, entry, pairing_codes

JOURNAL = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-journal"
VM_TIMEOUT = 20                      # time limit of the wrapper inside the VM
HOST_TIMEOUT = VM_TIMEOUT + 10
PRIORITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
TIME = re.compile(r"(today|yesterday|now|-\d{1,6}(s|min|h|d|w)|\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?)")
CHECKS = {
    "unit": lambda v: re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,127}", v),
    "priority": lambda v: v in PRIORITIES or re.fullmatch(r"[0-7]", v),
    "since": TIME.fullmatch,
    "until": TIME.fullmatch,
    "boot": lambda v: v in ("current", "all"),
    "grep": lambda v: 0 < len(v) <= 200 and clean(v, False) == v,
    "lines": lambda v: re.fullmatch(r"\d{1,4}", v) and 1 <= int(v) <= 5000,
    "after-cursor": lambda v: re.fullmatch(r"[A-Za-z0-9=;_-]{1,512}", v),
}
# What orders journal records, and within what its numbers compare: the
# sequence number journald counts up across its files (and boots), the time
# since boot, the wall-clock time (which can go back).
ORDER = (("__SEQNUM", "__SEQNUM_ID"), ("__MONOTONIC_TIMESTAMP", "_BOOT_ID"), ("__REALTIME_TIMESTAMP", None))


def to_vm_time(v):
    """Absolute time (Host local time) goes to the VM as UTC; relative stays as is."""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?", v)
    if not m:
        return v
    t = m.group(2) or "00:00"
    fmt = "%Y-%m-%d %H:%M:%S" if t.count(":") == 2 else "%Y-%m-%d %H:%M"
    local = datetime.datetime.strptime(f"{m.group(1)} {t}", fmt).astimezone()
    return local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def agent(ctx, cmd):
    r = virsh.run("qemu-agent-command", dom(ctx), json.dumps(cmd), "--timeout", "10")
    if r.returncode != 0:
        die(f"{ctx}: the guest agent does not answer (" + clean(r.stderr.strip(), False)[:200] + ")", 4,
            error="guest_agent_unavailable", vm=ctx)
    try:
        return json.loads(r.stdout)["return"]
    except (ValueError, KeyError, TypeError):
        die(f"{ctx}: unexpected answer from the guest agent", 4, error="guest_agent_unavailable", vm=ctx)


def ping(ctx, seconds=3):
    """Whether the guest agent answers a guest-ping within `seconds` (#54: a VM whose
    Orca does not answer either did not boot, or runs without Orca). virsh itself gets a
    few seconds more, not its usual limit: the ping runs under the wakes file's lock and in
    watch's round of looks (responding.py), so a hanging libvirtd holds up nobody for long."""
    r = virsh.run("qemu-agent-command", dom(ctx), json.dumps({"execute": "guest-ping"}), "--timeout",
                  str(max(1, seconds)), timeout=max(1, seconds) + 3)
    if r.returncode != 0:
        return False
    try:
        answer = json.loads(r.stdout)
    except ValueError:
        return False
    return isinstance(answer, dict) and "return" in answer


def run_in_vm(ctx, argv, wrapper=JOURNAL, limit=VM_TIMEOUT):
    """Run a wrapper of the Image in the VM (journalctl by default) with these
    arguments; returns (stdout, truncated)."""
    if virsh.state(ctx) != "running":
        die(f"{ctx}: the VM is not running (agentbox start {ctx})", 3, error="vm_not_running", vm=ctx)
    started = agent(ctx, {"execute": "guest-exec", "arguments": {
        "path": wrapper, "arg": argv, "capture-output": True}})
    pid = started.get("pid") if isinstance(started, dict) else None
    if not isinstance(pid, int):
        die(f"{ctx}: unexpected answer from the guest agent", 4, error="guest_agent_unavailable", vm=ctx)
    deadline = time.monotonic() + limit + 10
    while True:
        res = agent(ctx, {"execute": "guest-exec-status", "arguments": {"pid": pid}})
        if isinstance(res, dict) and res.get("exited"):
            break
        if time.monotonic() > deadline:
            die(f"{ctx}: the query takes too long", 5, error="vm_query_timeout", vm=ctx)
        time.sleep(0.2)
    try:
        out = base64.b64decode(res.get("out-data", "")).decode("utf-8", "replace")
        err = base64.b64decode(res.get("err-data", "")).decode("utf-8", "replace")
    except (ValueError, TypeError):
        die(f"{ctx}: unexpected answer from the guest agent", 4, error="guest_agent_unavailable", vm=ctx)
    code = res.get("exitcode")
    if code == 124 or res.get("signal") is not None:
        die(f"{ctx}: query in the VM stopped after {limit} s, narrow the filters", 5, error="vm_query_timeout",
            vm=ctx, seconds=limit)
    # journalctl --grep without a match ends with 1 and no output
    if code != 0 and not (code == 1 and not out.strip() and not err.strip() and wrapper == JOURNAL):
        program = "journalctl" if wrapper == JOURNAL else os.path.basename(wrapper)
        die(f"{ctx}: {program} fails in the VM: " + (clean(err.strip(), False)[:300] or f"exit {code}"), 6,
            error="vm_journal_failed" if wrapper == JOURNAL else "action_failed", vm=ctx)
    return out, bool(res.get("out-truncated") or res.get("err-truncated"))


def _number(v):
    return int(v) if isinstance(v, str) and re.fullmatch(r"[0-9]{1,20}", v) else None


def records(out):
    """The records of `journalctl --output=json` output, oldest first.

    journalctl does not always write them in that order: in the VM it wrote the
    newest match first with --grep and --lines. So they are sorted by the first
    field of ORDER that every record has, where its numbers are comparable: of
    one sequence (or boot); left as written when no field fits, and where two
    records tie. The fields come from the VM: a compromised one can fake the
    order as it can fake the records.
    """
    found = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except (ValueError, RecursionError):       # RecursionError: nested too deep
            continue
        if isinstance(e, dict):
            found.append(e)
    for key, scope in ORDER:
        keys = [_number(e.get(key)) for e in found]
        if None in keys or scope and len({json.dumps(e.get(scope)) for e in found}) > 1:
            continue
        return [e for _, e in sorted(zip(keys, found), key=lambda p: p[0])]
    return found


def parse(ctx, args):
    opts, as_json, i = {"boot": "current", "lines": "200"}, False, 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--json":
            as_json = True
            continue
        name, eq, val = a.partition("=")
        if not name.startswith("--") or name[2:] not in CHECKS:
            die(f"{ctx}: unknown option: " + clean(a, False)[:60], 2, error="invalid_argument",
                option=clean(a, False)[:60])
        if not eq:
            if i >= len(args):
                die(f"{ctx}: {name} needs a value", 2, error="invalid_argument", option=name)
            val, i = args[i], i + 1
        if not CHECKS[name[2:]](val):
            die(f"{ctx}: invalid value for {name}: " + clean(val, False)[:60], 2, error="invalid_argument", option=name)
        opts[name[2:]] = val
    return opts, as_json


def logs(args):
    """agentbox logs <vm> [filters] [--json]"""
    ctx = check(args[0] if args else "")
    opts, as_json = parse(ctx, args[1:])
    argv = ["--output=json", "--all"]
    if opts["boot"] == "current":
        argv.append("--boot=0")
    for k in ("unit", "priority", "since", "until", "grep", "after-cursor"):
        if k in opts:
            v = to_vm_time(opts[k]) if k in ("since", "until") else opts[k]
            argv.append(f"--{k}={v}")
    limit = int(opts["lines"])
    # after a cursor every newer entry in order, capped on the Host
    if "after-cursor" not in opts:
        argv.append(f"--lines={limit}")
    out, truncated = run_in_vm(ctx, argv)
    entries = [entry(e) for e in records(out)]
    if len(entries) > limit:
        entries, truncated = entries[:limit], True
    if as_json:
        public = [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]
        print(json.dumps({"vm": ctx, "entries": public, "cursor": public[-1]["cursor"] if public else None,
                          "truncated": truncated}, ensure_ascii=False))
    else:
        for e in entries:
            try:
                t = datetime.datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone()
                when = t.strftime("%b %d %H:%M:%S")
            except (AttributeError, ValueError):
                when = "?"
            pid = f"[{e['_pid']}]" if e["_pid"] else ""
            print(f"{when} {e['_host'] or '-'} {e['identifier'] or '-'}{pid}: {e['message'] or ''}")
    if truncated:
        print("agentbox: output truncated, narrow the filters", file=sys.stderr)


def boot_codes(ctx):
    """Every pairing code Orca printed in the current boot, oldest first (raises Fail on errors)."""
    out, _ = run_in_vm(ctx, ["--output=json", "--boot=0", "--unit=agentbox-orca.service",
                             "--grep=orca://pair", "--lines=50"])
    return pairing_codes(records(out))


def newest_code(ctx, exclude=()):
    """The newest pairing code Orca printed in the current boot (not one of `exclude`), or None."""
    codes = [c for c in boot_codes(ctx) if c not in exclude]
    return codes[-1] if codes else None


def find_code(ctx, exclude=()):
    """Pairing code of a VM, starting it if needed. Orca writes the code to its
    journal on start; until then (and while the guest agent does not answer yet)
    ask again, for about a minute, longer after a cold start."""
    budget = paths.tunable("AGENTBOX_PAIR_SECONDS", 60)
    from . import responding             # responding imports this module (guest-ping)
    with state_files.vm_lock(ctx):       # not in the middle of a Reset (#52)
        if virsh.state(ctx) != "running":
            responding.clear(ctx)        # the failed wakes of its last start are over
            virsh.start(ctx)
            budget += 120                # boot, guest agent and Orca need time
    deadline = time.time() + budget
    err = ""
    while True:
        try:
            code = newest_code(ctx, exclude)
            err = ""
        except Fail as f:
            code, err = None, f.message
        if code:
            return code
        if not time.time() + 5 < deadline:
            break
        time.sleep(5)
    die(f"{ctx}: no Pairing code found (Orca may still be starting)" + (f"; last: {err}" if err else ""),
        error="pairing_code_unavailable", vm=ctx)


def pair(args):
    """agentbox pair <vm> [--json]"""
    ctx = check(args[0] if args else "")
    code = find_code(ctx)
    if "--json" in args[1:]:
        print(json.dumps({"vm": ctx, "code": code}))
    else:
        print(code)


COMMANDS = {"logs": logs, "pair": pair}
NO_ROOT = {"logs", "pair", "version"}    # need libvirt access, or nothing of the Host at all
