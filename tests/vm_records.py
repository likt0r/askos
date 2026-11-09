"""VM records for tests: the registry as the migration leaves it for work and priv."""
import json
import os


def record(name, network, **fields):
    return {"name": name, "network": network, "orca_port": 6800 + network, "image": "base", "build": None,
            "memory_mib": 26624, "data_disk_gib": 300, "github_owners": [],
            "created": "2026-11-03T12:00:00Z", **fields}


def write(root, *records):
    """Write these records (default: work and priv) under the test root."""
    registry = os.path.join(root, "etc", "agentbox", "vms")
    os.makedirs(registry, exist_ok=True)
    for r in records or (record("work", 1), record("priv", 2)):
        with open(os.path.join(registry, r["name"] + ".json"), "w") as f:
            json.dump(r, f, indent=2)
            f.write("\n")


def read(root, name):
    with open(os.path.join(root, "etc", "agentbox", "vms", name + ".json")) as f:
        return json.load(f)


# A Host in Network mode, as titan is set up today.
NETWORK_HOST = {"mode": "network", "bind": "192.0.2.8", "web_port": 8800, "home_network": "192.0.2.0/24",
                "cert_name": "titan", "owner_user": "bewr"}


def write_host_setup(root, setup=None, **fields):
    """The Host setup under the test root (Network mode of NETWORK_HOST unless `setup` is given)."""
    path = os.path.join(root, "etc", "agentbox", "host.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({**(NETWORK_HOST if setup is None else setup), **fields}, f, indent=2)
        f.write("\n")
