#!/usr/bin/env python3
"""Stand-in for `qemu-img` in tests. A fake disk is a JSON file holding its
qemu-img metadata, e.g. {"backing-filename": "/.../base-20261101.qcow2"}.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "qemu-img", "argv": [...]}
  FAKE_QEMU_IMG_CREATE_SECONDS   `create` with a backing file takes this long before it
             writes the disk (default 0)
  FAKE_QEMU_IMG_CREATE_FAILS     `create` with a backing file writes nothing, prints this to
             stderr and exits 1

  qemu-img info -U --output=json <disk>
  qemu-img info -f qcow2 --output=json <disk>   with the format named, as qemu-img 10 answers
             it; the fake disk may hold "backing-filename", "data-file", "encrypted": true,
             "corrupt": true, "format" (another than qcow2 fails) and "virtual-size". The call
             records the limits it ran under: "limits": {"as": bytes, "cpu": seconds};
             "compat": "0.10" makes it a version 2 qcow2, which has no "corrupt" flag
  qemu-img create -q -f qcow2 -F qcow2 -b <base> <disk> [<size>]   writes the fake disk
  qemu-img create -q -f qcow2 <disk> <size>                a fake disk without backing file
  qemu-img resize -q -f qcow2 <disk> <size>                the fake disk gets that "size"
"""
import json
import os
import resource
import sys
import time

DIR = os.environ["FAKE_DIR"]


def info_qcow2(disk):
    """qemu-img info -f qcow2 --output=json of a fake disk: exit code."""
    try:
        with open(disk) as f:
            meta = json.loads(f.read())
    except OSError as e:
        print(f"qemu-img: Could not open '{disk}': Could not open '{disk}': {e.strerror}", file=sys.stderr)
        return 1
    except ValueError:
        meta = {}
    if not isinstance(meta, dict) or meta.get("format", "qcow2") != "qcow2":
        print(f"qemu-img: Could not open '{disk}': Image is not in qcow2 format", file=sys.stderr)
        return 1
    data = {"compat": "1.1", "compression-type": "zlib", "lazy-refcounts": False, "refcount-bits": 16,
            "corrupt": bool(meta.get("corrupt")), "extended-l2": False}
    if meta.get("compat") == "0.10":        # a version 2 header has none of the version 3 fields
        data = {"compat": "0.10", "compression-type": "zlib", "refcount-bits": 16}
    info = {"children": [{"name": "file", "info": {"filename": disk, "format": "file"}}],
            "virtual-size": meta.get("virtual-size", 5368709120), "filename": disk, "cluster-size": 65536,
            "format": "qcow2", "actual-size": os.path.getsize(disk),
            "format-specific": {"type": "qcow2", "data": data}, "dirty-flag": False}
    if "backing-filename" in meta:
        info.update({"backing-filename-format": "qcow2", "full-backing-filename": meta["backing-filename"],
                     "backing-filename": meta["backing-filename"]})
    if "data-file" in meta:
        data.update({"data-file": meta["data-file"], "data-file-raw": False})
    if meta.get("encrypted"):
        data["encrypt"] = {"format": "luks"}
        info["encrypted"] = True
    print(json.dumps(info, indent=4))
    return 0


def main(args):
    limits = {"as": resource.getrlimit(resource.RLIMIT_AS)[0], "cpu": resource.getrlimit(resource.RLIMIT_CPU)[0]}
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "qemu-img", "argv": args, "limits": limits}) + "\n")
    disk = args[-1]
    if args[:7] == ["create", "-q", "-f", "qcow2", "-F", "qcow2", "-b"] and len(args) == 10:
        disk = args[8]
    if args[:4] == ["info", "-f", "qcow2", "--output=json"] and len(args) == 5:
        return info_qcow2(disk)
    if args[:3] == ["info", "-U", "--output=json"]:
        try:
            with open(disk) as f:
                text = f.read()
        except OSError as e:
            print(f"qemu-img: Could not open '{disk}': Could not open '{disk}': {e.strerror}", file=sys.stderr)
            return 1
        try:
            meta = json.loads(text)
        except ValueError:
            meta = {}
        info = {"virtual-size": 21474836480, "filename": disk, "cluster-size": 65536, "format": "qcow2",
                "actual-size": 200704, "dirty-flag": False}
        if "backing-filename" in meta:
            info.update({"full-backing-filename": meta["backing-filename"],
                         "backing-filename": meta["backing-filename"], "backing-filename-format": "qcow2"})
        print(json.dumps(info, indent=4))
        return 0
    if args[:6] == ["create", "-q", "-f", "qcow2", "-F", "qcow2"] and args[6] == "-b" and len(args) in (9, 10):
        time.sleep(float(os.environ.get("FAKE_QEMU_IMG_CREATE_SECONDS", "0")))
        if os.environ.get("FAKE_QEMU_IMG_CREATE_FAILS"):
            print(os.environ["FAKE_QEMU_IMG_CREATE_FAILS"], file=sys.stderr)
            return 1
        try:
            with open(disk, "w") as f:
                json.dump({"backing-filename": args[7], **({"size": args[9]} if len(args) == 10 else {})}, f)
        except OSError as e:
            print(f"qemu-img: {disk}: Could not create '{disk}': {e.strerror}", file=sys.stderr)
            return 1
        return 0
    if args[:4] == ["resize", "-q", "-f", "qcow2"] and len(args) == 6:
        with open(args[4]) as f:
            meta = json.load(f)
        with open(args[4], "w") as f:
            json.dump({**meta, "size": args[5]}, f)
        return 0
    if args[:4] == ["create", "-q", "-f", "qcow2"] and len(args) == 6:
        with open(args[4], "w") as f:
            json.dump({"size": args[5]}, f)
        return 0
    print("qemu-img (fake): unexpected arguments", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
