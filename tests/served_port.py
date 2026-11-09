"""The port of the real Control Room backend (`web/agentbox-web`) in a test.

Tests start the server with AGENTBOX_WEB_PORT=0: the kernel picks a free port while the
server binds it, so no other test running at the same time (tests/run_parallel.py) can
take it between choosing and binding. The server names the port in its start line.
"""
import re

START = re.compile(r"^Control Room running on https://\S+:(\d+)$", re.M)


def port(log):
    """The port from the server's stderr, or None while it does not listen yet."""
    m = START.search(log)
    return int(m.group(1)) if m else None
