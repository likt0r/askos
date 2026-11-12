"""The Container image cleanup of the VMs (ADR 0017): `agentbox cleanup-container-images`,
run every hour by agentbox-container-image-cleanup.timer (rpm/units).

Each VM's record says whether its cleanup is on (container_image_cleanup) and after how many
days (container_image_cleanup_days); a record without them means on and 7 days. The command
goes through the VM registry and skips, without an Activity entry, the VMs whose cleanup is
off and those that do not run (off, paused, missing in libvirt; a VM being deleted has no
record here any more). It never starts or wakes a VM. For each other VM it takes the VM's
lifecycle lock without waiting (a VM whose lock is held, by a Reset, a Pause or an import, is
skipped this hour) and holds it while the guest agent runs agentbox-container-images in the
Image (`prune <days>`), as it runs the journal wrapper (ADR 0004). The wrapper drops to the
agent and removes the Container images no container uses that were not used for that many
calendar days; it never removes containers or volumes. The run has 5 minutes.

What comes back from the VM is untrusted: only a small JSON object with two whole numbers
(removed, freed_bytes) is taken from it, and nothing else it prints, its error output
neither, reaches the output or Activity. Anything else is container_image_cleanup_failed.

Activity: container-image-cleanup.removed for a VM where something was removed,
container-image-cleanup.failed with the error code for one that failed, nothing for a run
that removed nothing or a VM that was skipped. The command records these entries itself; the
dispatcher records nothing for it (activity.own_entry()). One VM failing never stops the
others, and the command fails only when the VM registry cannot be read.
"""
import base64
import json
import os
import sys
import time

from . import activity, generate, guest, paths, registry, state_files, virsh
from .errors import Fail, die
from .registry import dom
from .sanitize import clean

WRAPPER = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-container-images"
LIMIT = 300                   # seconds for the wrapper in one VM (it stops itself a little before)
MAX_ANSWER = 1024             # characters of the wrapper's answer taken at all
MAX_NUMBER = 1 << 63


def _size(n):
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def _missing(ctx):
    die(f"{ctx}: the VM's Build has no Container image cleanup yet; build its Image and reset the VM first "
        f"(agentbox build <image> --reset {ctx})", error="container_image_cleanup_missing", vm=ctx)


def _failed(ctx, why):
    die(f"{ctx}: the Container image cleanup in the VM failed ({why})", error="container_image_cleanup_failed", vm=ctx)


def _run(ctx, days):
    """Run the wrapper in the VM: its output, once it ended within the limit and well."""
    r = virsh.run("qemu-agent-command", dom(ctx), json.dumps({"execute": "guest-exec", "arguments": {
        "path": WRAPPER, "arg": ["prune", str(days)], "capture-output": True}}), "--timeout", "10")
    if r.returncode != 0:
        if "No such file or directory" in r.stderr:
            _missing(ctx)
        die(f"{ctx}: the guest agent does not answer (" + clean(r.stderr.strip(), False)[:200] + ")", 4,
            error="guest_agent_unavailable", vm=ctx)
    try:
        pid = json.loads(r.stdout)["return"]["pid"]
    except (ValueError, KeyError, TypeError, RecursionError):
        pid = None
    if type(pid) is not int:
        die(f"{ctx}: unexpected answer from the guest agent", 4, error="guest_agent_unavailable", vm=ctx)
    limit = paths.tunable("AGENTBOX_CONTAINER_IMAGE_CLEANUP_SECONDS", LIMIT)
    deadline, pause = time.monotonic() + limit, 0.2
    while True:
        res = guest.agent(ctx, {"execute": "guest-exec-status", "arguments": {"pid": pid}})
        if isinstance(res, dict) and res.get("exited") is True:
            break
        if time.monotonic() > deadline:
            die(f"{ctx}: the Container image cleanup in the VM takes longer than {limit} s", 5,
                error="vm_query_timeout", vm=ctx, seconds=limit)
        time.sleep(pause)
        pause = min(pause * 2, 2)
    if res.get("signal") is not None or res.get("exitcode") == 124:
        die(f"{ctx}: the Container image cleanup in the VM stopped after its time limit", 5,
            error="vm_query_timeout", vm=ctx, seconds=limit)
    code = res.get("exitcode")
    if code != 0:
        _failed(ctx, f"exit {code}" if type(code) is int else "no exit code")
    data = res.get("out-data", "")
    if not isinstance(data, str) or len(data) > MAX_ANSWER * 2 or res.get("out-truncated"):
        _failed(ctx, "unexpected answer")
    try:
        return base64.b64decode(data, validate=True).decode("utf-8")
    except (ValueError, TypeError):
        _failed(ctx, "unexpected answer")


def _answer(ctx, out):
    """(removed, freed_bytes) from the wrapper's answer, which comes from the VM: untrusted."""
    try:
        answer = json.loads(out) if len(out) <= MAX_ANSWER else None
        numbers = [answer[k] for k in ("removed", "freed_bytes")] if isinstance(answer, dict) else None
    except (ValueError, KeyError, RecursionError):
        numbers = None
    if not numbers or not all(type(n) is int and 0 <= n < MAX_NUMBER for n in numbers):
        _failed(ctx, "unexpected answer")
    return numbers[0], numbers[1]


def _result(ctx, outcome, reason=None, removed=None, freed=None, error=None):
    return {"vm": ctx, "outcome": outcome, "reason": reason, "removed": removed, "freed_bytes": freed,
            "error": error}


def _one(rec, say):
    """Clean up one VM: its line of the JSON output."""
    ctx = rec["name"]
    enabled, days = registry.cleanup(rec)
    if not enabled:
        say(f"{ctx}: skipped, the Container image cleanup is off")
        return _result(ctx, "skipped", "off")
    if virsh.state(ctx) != "running":
        say(f"{ctx}: skipped, the VM is not running")
        return _result(ctx, "skipped", "not_running")
    with state_files.locked_unless_held(f"lifecycle-{ctx}") as taken:   # vm_lock, never waiting
        if not taken:
            say(f"{ctx}: skipped, another action holds the VM; the next hour tries again")
            return _result(ctx, "skipped", "locked")
        if virsh.state(ctx) != "running":             # stopped while the lock was taken
            say(f"{ctx}: skipped, the VM is not running")
            return _result(ctx, "skipped", "not_running")
        try:
            removed, freed = _answer(ctx, _run(ctx, days))
        except Fail as f:
            if os.path.exists(generate.delete_marker(ctx)) or not os.path.exists(registry.record_file(ctx)):
                say(f"{ctx}: skipped, the VM is being deleted")     # delete does not take the lock
                return _result(ctx, "skipped", "not_running")
            print(f"agentbox: {f.message}", file=sys.stderr, flush=True)
            activity.record("action", "container-image-cleanup.failed",
                            f"Container image cleanup failed: {f.message}", ctx, "failed", params={"days": days},
                            error=f.error, error_params=f.params)
            return _result(ctx, "failed", error=f.error)
    if not removed:
        say(f"{ctx}: nothing to remove")
        return _result(ctx, "nothing", removed=0, freed=freed)
    text = f"removed {removed} Container image{'s' if removed != 1 else ''} ({_size(freed)}) not used for " \
           f"more than {days} days"
    say(f"{ctx}: {text}")
    activity.record("action", "container-image-cleanup.removed", f"Container image cleanup {text}", ctx,
                    params={"removed": removed, "freed_bytes": freed, "days": days})
    return _result(ctx, "removed", removed=removed, freed=freed)


def cleanup(args):
    """agentbox cleanup-container-images [--json] (hourly timer)"""
    activity.own_entry()              # one entry per VM that removed something or failed, below
    for a in args:
        if a != "--json":
            die("unknown option: " + clean(a, False)[:60], 2, error="invalid_argument", option=clean(a, False)[:60])
    as_json = "--json" in args
    records, problems = registry.scan()
    for name, problem in problems:
        print(f"agentbox: {problem}", file=sys.stderr, flush=True)

    def say(line):
        if not as_json:
            print(line, flush=True)

    results = []
    for rec in records:
        try:
            results.append(_one(rec, say))
        except (Fail, OSError) as e:          # this VM only: the others go on
            error = e.error if isinstance(e, Fail) else "action_failed"
            print(f"agentbox: {e.message if isinstance(e, Fail) else e}", file=sys.stderr, flush=True)
            results.append(_result(rec["name"], "failed", error=error))
    if as_json:
        print(json.dumps({"vms": results}))


COMMANDS = {"cleanup-container-images": cleanup}
