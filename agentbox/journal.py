"""Entries for the systemd journal in its native protocol (systemd's journal-native(7)).

Shared by the CLI's Activity (activity.py) and the Control Room backend, which records
sign-ins (web/agentbox-web). A leaf module: the standard library only, nothing of this
package, so the backend can import it without the rest of agentbox.
"""
import os
import socket
import struct

SENT, NOWHERE, REFUSED = "sent", "nowhere", "refused"


def encode(fields):
    """One entry: each field as KEY=value, or with its length when the value has a line break."""
    out = bytearray()
    for key, value in fields.items():
        data = value.encode("utf-8", "replace")
        if b"\n" in data:
            out += key.encode() + b"\n" + struct.pack("<Q", len(data)) + data + b"\n"
        else:
            out += key.encode() + b"=" + data + b"\n"
    return bytes(out)


def send(data, sockets):
    """An entry to the first of these sockets that exists and takes it. SENT once one took it;
    REFUSED when the first (the namespace of Activity) does not let this user write, which
    sends it nowhere else either; otherwise the last error of the others, or NOWHERE when none
    of them exists."""
    result = NOWHERE
    for i, path in enumerate(sockets):
        if not path or not os.path.exists(path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.sendto(data, path)
            return SENT
        except PermissionError as e:
            if i == 0:
                return REFUSED
            result = e
        except OSError as e:
            result = e
    return result
