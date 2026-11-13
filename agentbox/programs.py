"""Programs of the Host, each run with a time limit of its own; a failure ends the command.

run() is the one way agentbox starts a program it waits for and stops on: a program that is
missing, cannot be started, does not end within its time limit or ends with an exit code that
is no answer raises Fail (action_failed unless the caller names another code) with one wording
and `program=` in its parameters. Programs inside a VM go through the guest agent instead
(guest.execute).
"""
import os
import subprocess

from .errors import Fail
from .sanitize import clean

STDERR = 300          # characters of a failed program's error output in its message, the last ones


def summary(r):
    """What a failed program said: its error output on one line, cleaned, the last
    STDERR characters; its exit code when it said nothing."""
    return clean(" ".join((r.stderr or "").split()), False)[-STDERR:] or f"exit {r.returncode}"


def fail(r, what, *, program, error="action_failed", **params):
    """Fail for the CompletedProcess of a program that ended with an exit code that is no answer."""
    raise Fail(f"{what} failed: {summary(r)}", error=error, program=program, **params)


def run(argv, timeout, *, what=None, ok=(0,), c_locale=False, env=None, input=None, error="action_failed",
        **params):
    """argv to its end with its output captured: the CompletedProcess (text; bytes that are not
    UTF-8 replaced).

    timeout: seconds, None for no limit. what: the call in messages (default: the command
    line). ok: the exit codes that count as an answer, None for every one. c_locale: with
    LC_ALL=C, so the program's messages are not translated. env: the environment instead of
    agentbox's. input: text on stdin. error and params: the code and further
    parameters of the failure; `program=` (argv[0]'s name) is always one.

    Fails with exit code 127 when the program is missing, with 1 when it cannot be started,
    does not answer in time or ends with an exit code not in ok."""
    what = what or " ".join(argv)
    params = {"program": os.path.basename(argv[0]), **params}
    if c_locale:
        env = dict(os.environ if env is None else env, LC_ALL="C")
    try:
        r = subprocess.run(argv, input=input, capture_output=True, text=True, errors="replace", env=env,
                           timeout=timeout)
    except FileNotFoundError:
        raise Fail(f"{argv[0]} is missing", 127, error=error, **params) from None
    except OSError as e:
        raise Fail(f"{what} cannot be started: {e.strerror or e}", error=error, **params) from None
    except subprocess.TimeoutExpired:
        raise Fail(f"{what} does not answer within {timeout:g} s", error=error, **params) from None
    if ok is not None and r.returncode not in ok:
        fail(r, what, error=error, **params)
    return r


def quietly(argv, timeout):
    """argv to its end, without its output; whatever goes wrong is ignored (a cleanup that
    may find nothing to clean up)."""
    try:
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass


def systemctl(*args, timeout=120, ok=(0,), **params):
    """systemctl with these arguments, in the C locale (run()): its CompletedProcess."""
    return run(["systemctl", *args], timeout, ok=ok, c_locale=True, **params)
