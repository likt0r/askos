"""Paired devices of a VM's Orca: list them, revoke all of them (#19).

What Orca supports (checked in its CLI and bundled code, see ARCHITECTURE.md):
the CLI and the runtime RPC have no command to list or remove Paired devices;
only the desktop GUI does (Settings > Mobile). Orca keeps them in
orca-devices.json in its data directory in the VM, its identity in
orca-e2ee-keypair.json next to it. So:

- Listing reads orca-devices.json inside the VM through a fixed wrapper in the
  Image (agentbox-devices, ADR 0004; no shell, time limit), never a disk. The
  wrapper leaves out tokens. The Observer is the device the Host's own Orca
  data directory names as its paired device; it is marked and not removable.
- Removing a single device is not possible without stopping Orca and editing
  its registry, which Orca overwrites while it runs; `unpair` therefore only
  explains that. Revoking all is: the wrapper stops Orca, deletes registry and
  key and starts it again, so Orca has a new identity and every device must
  pair again. The Host re-pairs itself as Observer when it was one.

Pairing codes never leave this module: the new code is only used to re-pair the
Observer, and every message passes through sanitize.clean.

Exit codes: those of guest.py; 2 invalid usage.
"""
import datetime
import json
import os
import re
import subprocess

from . import guest, lifecycle, options, paths
from .errors import die
from .registry import check
from .sanitize import clean

DEVICES = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-devices"
LIST_LIMIT = 20
REVOKE_LIMIT = 45                    # two systemctl calls of 20 s in the wrapper
DEVICE_ID = r"[A-Za-z0-9_-]{1,64}"


def _iso(ms):
    if not ms:
        return None
    try:
        t = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def observer_ids(ctx):
    """Device IDs the Host's own Orca data directory names as its paired device(s).
    None when the Host is no Observer of this VM (no data directory)."""
    d = lifecycle.observer_dir(ctx)
    if not os.path.isdir(d):
        return None
    ids = set()

    def walk(v):
        if isinstance(v, dict):
            p = v.get("pairedDeviceId")
            if isinstance(p, str):
                ids.add(p)
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    try:
        with open(os.path.join(d, "orca-environments.json")) as f:
            walk(json.load(f))
    except (OSError, ValueError):
        pass
    return ids


def devices(ctx):
    """The VM's Paired devices as public dicts, newest pairing first."""
    out, _ = guest.run_in_vm(ctx, ["list"], DEVICES, LIST_LIMIT)
    try:
        rows = json.loads(out)["devices"]
        if not isinstance(rows, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        die(f"{ctx}: unexpected answer from the device list in the VM", 6, error="action_failed", vm=ctx)
    observer = observer_ids(ctx)
    result = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str):
            continue
        result.append({
            "id": clean(r["id"], False)[:64],
            "name": clean(str(r.get("name") or ""), False)[:128],
            "scope": "runtime" if r.get("scope") == "runtime" else "mobile",
            "paired_at": _iso(r.get("paired_ms")),
            "last_seen_at": _iso(r.get("last_seen_ms")),
            "observer": bool(observer) and r["id"] in observer,
            "removable": False,      # Orca offers no removal of a single device to us (module docstring)
        })
    result.sort(key=lambda d: d["paired_at"] or "", reverse=True)
    return result, observer


def pairings(args):
    """agentbox pairings <vm> [--json]"""
    pos, opts = options.parse(args, {"json": False})
    if len(pos) != 1:
        die("pairings <vm> [--json]", 2, error="invalid_argument")
    ctx = check(pos[0])
    rows, observer = devices(ctx)
    if "json" in opts:
        print(json.dumps({"vm": ctx, "devices": rows, "observer_known": observer is not None,
                          "single_removal": False}, ensure_ascii=False))
        return
    if not rows:
        print(f"{ctx}: no Paired devices")
    for d in rows:
        mark = "  [Observer, not removable]" if d["observer"] else ""
        print(f"{d['id']}  {d['name'] or '-'}  paired {d['paired_at'] or '?'}  "
              f"last seen {d['last_seen_at'] or 'never'}{mark}")


def unpair(args):
    """agentbox unpair <vm> <device-id> [--json]: always a refusal, Orca cannot do it."""
    pos, _ = options.parse(args, {"json": False})
    if len(pos) != 2:
        die("unpair <vm> <device>", 2, error="invalid_argument")
    ctx = check(pos[0])
    if not re.fullmatch(DEVICE_ID, pos[1]):
        die(f"{ctx}: invalid device ID", 2, error="device_not_found", vm=ctx)
    rows, _ = devices(ctx)
    hit = next((d for d in rows if d["id"] == pos[1]), None)
    if hit is None:
        die(f"{ctx}: no such device: {pos[1]}", 2, error="device_not_found", vm=ctx, device=pos[1])
    if hit["observer"]:
        die(f"{ctx}: this is the Observer (the Host), it cannot be removed", 2, error="observer_not_removable",
            vm=ctx, device=pos[1])
    die(f"{ctx}: Orca cannot remove a single device; revoke all pairings: "
        f"agentbox revoke-pairings {ctx} --confirm {ctx}", 2, error="unpair_unsupported", vm=ctx, device=pos[1])


def revoke_all(args):
    """agentbox revoke-pairings <vm> --confirm <vm> [--json]"""
    pos, opts = options.parse(args, {"confirm": True, "json": False})
    if len(pos) != 1:
        die("revoke-pairings <vm> --confirm <vm> [--json]", 2, error="invalid_argument")
    ctx = check(pos[0])
    if opts.get("confirm") != ctx:
        die(f"revoking only with --confirm {ctx}", 2, error="confirmation_required", vm=ctx)
    say, lines = lifecycle._reporter("json" in opts)
    observer = lifecycle.observer_dir(ctx)
    was_observer = os.path.isdir(observer)
    old = guest.boot_codes(ctx)            # codes of the old identity are no use afterwards
    out, _ = guest.run_in_vm(ctx, ["revoke-all"], DEVICES, REVOKE_LIMIT)
    try:
        removed = json.loads(out)["removed"]
    except (ValueError, KeyError, TypeError):
        die(f"{ctx}: unexpected answer to revoking in the VM", 6, error="action_failed", vm=ctx)
    if not removed:
        die(f"{ctx}: Orca has no pairing data (nothing revoked); Orca was restarted", 6, error="no_pairings", vm=ctx)
    say(f"{ctx}: all pairings revoked, Orca has a new identity; every device must pair again")
    repaired = None
    if was_observer:
        code = guest.find_code(ctx, exclude=old)
        lifecycle.orca_cli(ctx, "environment", "rm", "--environment", ctx,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = lifecycle.orca_cli(ctx, "environment", "add", "--name", ctx, "--pairing-code", code,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        repaired = r.returncode == 0
        say(f"{ctx}: the Host is Observer again" if repaired
            else f"{ctx}: the Host could not pair again, repeat agentbox observe {ctx}")
    if "json" in opts:
        print(json.dumps({"vm": ctx, "observer_repaired": repaired, "message": lifecycle._message(lines)}))
    if repaired is False:
        die("Observer not paired again", 8, error="action_failed", vm=ctx)


COMMANDS = {"pairings": pairings, "unpair": unpair, "revoke-pairings": revoke_all}
NO_ROOT = set()
