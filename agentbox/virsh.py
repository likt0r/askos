"""libvirt through virsh, with a time limit on every call."""
import os
import subprocess
import tempfile

from . import paths
from .registry import dom

# Each call has its own limit so a hanging guest agent or libvirtd cannot block
# agentbox (guest-agent commands get 10 s on top of that inside virsh).
TIMEOUT = paths.tunable("AGENTBOX_VIRSH_TIMEOUT", 15)
# state() of a domain libvirt does not know (or cannot tell)
MISSING = "missing"


def _virsh(args, timeout, **kw):
    limit = TIMEOUT if timeout is None else min(timeout, TIMEOUT)
    env = dict(os.environ, LC_ALL="C", **kw.pop("env", {}))
    try:
        return subprocess.run(["virsh", "-q", "-c", "qemu:///system", *args], capture_output=True, text=True,
                              errors="replace", env=env, timeout=limit, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"virsh does not answer within {limit:g} s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "virsh is missing")


def run(*args, timeout=None):
    """virsh with these arguments; timeout: a shorter limit than TIMEOUT for this call."""
    return _virsh(args, timeout)


def quote(word):
    """One word for virsh's own command parser: in single quotes, which keep everything as it
    is; a single quote closes them, follows escaped and opens them again."""
    return "'" + word.replace("'", "'\\''") + "'"


def run_unseen(*args, timeout=None):
    """virsh with this command on its standard input instead of in its arguments, for a command
    that carries a secret: the arguments of every process are there for every local user to read
    (/proc/<pid>/cmdline), its standard input is not. Only for a command whose output is one
    line (qemu-agent-command), as run gives it.

    virsh without a command reads commands from its standard input as its interactive shell
    does, through readline: it shows the line it reads and its prompt on its standard output,
    keeps a history file and ends with 0 also when the command failed. So its terminal is a
    dumb one, without the user's readline settings, its history goes to a directory of its own
    that is removed afterwards and keeps no line (VIRSH_HISTSIZE=0), a failure is an error line
    on its standard error (exit code 1), and the output is the line before its last prompt."""
    line = " ".join(quote(a) for a in args)
    if any(c < " " or c == "\x7f" for c in line):          # readline would act on them
        raise ValueError("a command for virsh's standard input is one line of printable characters")
    with tempfile.TemporaryDirectory(prefix="agentbox-virsh-") as cache:
        r = _virsh((), timeout, input=line + "\n", env={
            "TERM": "dumb", "INPUTRC": "/dev/null", "XDG_CACHE_HOME": cache, "VIRSH_HISTSIZE": "0"})
    if r.returncode != 0:
        return r
    lines = r.stdout.split("\n")
    if any(e.startswith("error:") for e in r.stderr.splitlines()):
        return subprocess.CompletedProcess(args, 1, "", r.stderr)
    if len(lines) < 4 or lines[-1] or not lines[-2].startswith("virsh"):
        return subprocess.CompletedProcess(args, 1, "", r.stderr + "virsh gave no output for the command\n")
    return subprocess.CompletedProcess(args, 0, lines[-3] + "\n", r.stderr)


def state(ctx):
    """First line of `virsh domstate`, e.g. "running" or "shut off"; MISSING when unknown."""
    r = run("domstate", dom(ctx))
    first = r.stdout.split("\n")[0].strip() if r.returncode == 0 else ""
    return first or MISSING


def start(ctx):
    return run("start", dom(ctx)).returncode == 0
