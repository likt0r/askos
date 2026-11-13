"""The Host setup: how this Host is installed and exposed (GLOSSARY.md, ADR 0009).

One root-owned record, separate from the Settings: the mode, the address and port of
the Control Room, the home network, the certificate name and the
owner user. It changes only on the Host (never through the Control Room), so it cannot
widen what the Control Room is reachable from. A Host without a record is Local-only.

The Control Room backend (web/agentbox-web) takes its address and port from load() too, so
it starts on exactly the records the CLI accepts and stops on the others.
"""
import ipaddress
import json
import re

from . import paths
from .errors import die

PATH = paths.host("/etc/agentbox/host.json")

LOCAL, NETWORK = "local", "network"
LOOPBACK = "127.0.0.1"
DEFAULT_WEB_PORT = 8800
FIELDS = ("mode", "bind", "web_port", "home_network", "cert_name", "owner_user")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _bad(message):
    die(f"Host setup {PATH}: {message}", error="action_failed", path=PATH)


def _text(raw, field):
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        _bad(f"{field} is not a valid name: {value!r}")
    return value


def _address(value, field):
    try:
        return ipaddress.IPv4Address(value)
    except ValueError:
        return _bad(f"{field} is not an IPv4 address: {value!r}")


def load():
    """The Host setup with every field present; Local-only if there is no record.
    A record that cannot be read or makes no sense stops the command: guessing
    could expose more than the owner chose."""
    try:
        with open(PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        return _bad(f"not readable ({e})")
    return parse(raw)


def parse(raw):
    """The Host setup from a record (a dict) with every field present, or a clear error."""
    if not isinstance(raw, dict):
        _bad("must be a JSON object")
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        _bad(f"unknown fields: {', '.join(unknown)}")

    mode = raw.get("mode", LOCAL)
    if mode not in (LOCAL, NETWORK):
        _bad(f"mode must be local or network, not {mode!r}")
    port = raw.get("web_port", DEFAULT_WEB_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        _bad(f"web_port must be a port number, not {port!r}")
    bind = _address(raw.get("bind", LOOPBACK), "bind")
    network = raw.get("home_network")

    if mode == LOCAL:
        if not bind.is_loopback:
            _bad(f"in mode local, bind must be a loopback address, not {bind}")
        if network is not None:
            _bad("mode local has no home_network")
    else:
        if "bind" not in raw or bind.is_loopback or bind.is_unspecified:
            _bad("mode network needs bind: the Host's address in the home network")
        try:
            network = str(ipaddress.IPv4Network(network, strict=True))
        except (ValueError, TypeError):
            _bad(f"mode network needs home_network: a network like 192.0.2.0/24, not {network!r}")

    return {"mode": mode, "bind": str(bind), "web_port": port, "home_network": network,
            "cert_name": _text(raw, "cert_name"),
            "owner_user": _text(raw, "owner_user")}


def orca_listen(setup, port):
    """ListenStream of a VM's Orca port: only on this Host in Local-only, on every
    address in Network mode (the firewall lets the home network through)."""
    return f"{LOOPBACK}:{port}" if setup["mode"] == LOCAL else str(port)


def pairing_address(setup):
    """The address devices use to reach this Host's Orca ports; the VM tells Orca."""
    return setup["bind"]
