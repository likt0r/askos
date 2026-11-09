"""libvirt through virsh, with a time limit on every call."""
import os
import subprocess

from . import paths
from .registry import dom

# Each call has its own limit so a hanging guest agent or libvirtd cannot block
# agentbox (guest-agent commands get 10 s on top of that inside virsh).
TIMEOUT = paths.tunable("AGENTBOX_VIRSH_TIMEOUT", 15)
# state() of a domain libvirt does not know (or cannot tell)
MISSING = "missing"


def run(*args, timeout=None):
    """virsh with these arguments; timeout: a shorter limit than TIMEOUT for this call."""
    limit = TIMEOUT if timeout is None else min(timeout, TIMEOUT)
    try:
        return subprocess.run(["virsh", "-q", "-c", "qemu:///system", *args], capture_output=True,
                              text=True, errors="replace", env=dict(os.environ, LC_ALL="C"),
                              timeout=limit)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"virsh does not answer within {limit:g} s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "virsh is missing")


def state(ctx):
    """First line of `virsh domstate`, e.g. "running" or "shut off"; MISSING when unknown."""
    r = run("domstate", dom(ctx))
    first = r.stdout.split("\n")[0].strip() if r.returncode == 0 else ""
    return first or MISSING


def start(ctx):
    return run("start", dom(ctx)).returncode == 0
