#!/usr/bin/env python3
"""Stand-in for `xorrisofs` in tests (a build VM's NoCloud seed, agentbox/build_vm_run.py).

  xorrisofs -quiet -o <iso> -J -input-charset utf8 -rational-rock -V <volume> <directory>
      writes <iso> as JSON {"volume": <volume>, "files": {name: content}} of the files of
      <directory>, as the real one puts a directory's content at the top of the image

Environment: FAKE_DIR (calls in calls.jsonl as {"cmd": "xorrisofs", "argv": [...]}).
"""
import json
import os
import sys


def main(argv):
    with open(os.path.join(os.environ["FAKE_DIR"], "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "xorrisofs", "argv": argv}) + "\n")
    if "-o" not in argv or "-V" not in argv:
        print("xorrisofs (fake): -o and -V expected", file=sys.stderr)
        return 1
    folder = argv[-1]
    files = {}
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name)) as f:
            files[name] = f.read()
    with open(argv[argv.index("-o") + 1], "w") as f:
        json.dump({"volume": argv[argv.index("-V") + 1], "files": files}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
