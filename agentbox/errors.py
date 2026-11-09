"""Failures end the command with a message on stderr and an exit code.

Every failure carries a stable error code (`error`), set where it is raised, and the
parameters that matter to a program (ADR 0012). Codes are groups, not one per call
site: those the Control Room can trigger are listed in docs/control-room-api.md and
translated by its UI; a failure only the Host can cause (setup, regenerate, timers, a
broken file under /etc/agentbox) uses the generic `action_failed`. With --json the CLI
prints the code, the message and the parameters as one JSON object on stderr (cli.py);
otherwise only the message.
"""


class Fail(Exception):
    """message: English, for people. exit: the exit code. error: the stable code.
    params: values for programs (a VM, a domain, a setting), never a secret.
    raw: the message is another program's own error output, printed unchanged
    (without the "agentbox:" prefix)."""

    def __init__(self, message, exit=1, *, error, raw=False, **params):
        super().__init__(message)
        self.message = message
        self.exit = exit
        self.error = error
        self.raw = raw
        self.params = params


def die(message, exit=1, *, error, **params):
    raise Fail(message, exit, error=error, **params)
