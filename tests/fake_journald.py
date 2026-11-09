"""Stand-in for journald's socket in tests: a datagram socket where agentbox and the
Control Room send their journal entries, decoded from the native journal protocol.

    journald = Journald(path)      # bind before the program runs
    journald.entries()             # [{"MESSAGE": ..., "AGENTBOX_KIND": ...}, ...]
    journald.raw()                 # every datagram as received, for "never contains" checks
"""
import os
import socket
import struct


def decode(data):
    """One datagram of the native journal protocol as {field: value}."""
    fields, i = {}, 0
    while i < len(data):
        nl = data.index(b"\n", i)
        line = data[i:nl]
        if b"=" in line:
            key, value = line.split(b"=", 1)
            i = nl + 1
        else:
            key = line
            size = struct.unpack("<Q", data[nl + 1:nl + 9])[0]
            value = data[nl + 9:nl + 9 + size]
            assert data[nl + 9 + size:nl + 10 + size] == b"\n", "length-prefixed field without newline"
            i = nl + 10 + size
        fields[key.decode()] = value.decode()
    return fields


class Journald:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(path)
        self.sock.setblocking(False)
        self.datagrams = []

    def close(self):
        self.sock.close()

    def raw(self):
        while True:
            try:
                self.datagrams.append(self.sock.recv(1 << 20))
            except BlockingIOError:
                return list(self.datagrams)

    def entries(self):
        return [decode(d) for d in self.raw()]
