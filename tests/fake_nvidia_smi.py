#!/usr/bin/env python3
"""Stand-in for `nvidia-smi` in tests of the host readings (web/agentbox-readings).

Environment:
  FAKE_NVIDIA_SMI_DIR   directory holding nvidia-smi.json; calls are recorded in nvidia-smi.jsonl

nvidia-smi.json (optional):
  stdout     printed on stdout (default: one idle TITAN RTX, as titan's driver prints it
             for --query-gpu=...  --format=csv,noheader,nounits)
  exitcode   exit status (default 0)
"""
import json
import os
import sys

DIR = os.environ["FAKE_NVIDIA_SMI_DIR"]

DEFAULT = "0, NVIDIA TITAN RTX, 30, 5, 49, 24576, 21.93, 280.00\n"


def main(args):
    with open(os.path.join(DIR, "nvidia-smi.jsonl"), "a") as f:
        f.write(json.dumps(args) + "\n")
    try:
        with open(os.path.join(DIR, "nvidia-smi.json")) as f:
            r = json.load(f)
    except FileNotFoundError:
        r = {}
    sys.stdout.write(r.get("stdout", DEFAULT))
    return r.get("exitcode", 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
