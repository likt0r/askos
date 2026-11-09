#!/usr/bin/env python3
"""Stand-in for `qemu-img` in tests. A fake disk is a JSON file holding its
qemu-img metadata, e.g. {"backing-filename": "/.../base-20261101.qcow2"}.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "qemu-img", "argv": [...]}
  FAKE_QEMU_IMG_CREATE_SECONDS   `create` with a backing file takes this long before it
             writes the disk (default 0)

  qemu-img info -U --output=json <disk>
  qemu-img create -q -f qcow2 -F qcow2 -b <base> <disk>   writes the fake disk
  qemu-img create -q -f qcow2 <disk> <size>                a fake disk without backing file
"""
import json
import os
import sys
import time

DIR = os.environ["FAKE_DIR"]


def main(args):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "qemu-img", "argv": args}) + "\n")
    disk = args[-1]
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
    if args[:6] == ["create", "-q", "-f", "qcow2", "-F", "qcow2"] and args[6] == "-b" and len(args) == 9:
        time.sleep(float(os.environ.get("FAKE_QEMU_IMG_CREATE_SECONDS", "0")))
        try:
            with open(disk, "w") as f:
                json.dump({"backing-filename": args[7]}, f)
        except OSError as e:
            print(f"qemu-img: {disk}: Could not create '{disk}': {e.strerror}", file=sys.stderr)
            return 1
        return 0
    if args[:4] == ["create", "-q", "-f", "qcow2"] and len(args) == 6:
        with open(args[4], "w") as f:
            json.dump({"size": args[5]}, f)
        return 0
    print("qemu-img (fake): unexpected arguments", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
