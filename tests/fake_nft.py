#!/usr/bin/env python3
"""Stand-in for `nft` in tests: the ruleset in the kernel, as the scenario says it is loaded.

  nft list chain inet agentbox_vm <chain>
      prints the scenario's "nft_chains" entry for the chain (nft's own listing, as a test
      copies it from a real nft), or fails as nft does for a chain that is not loaded

Environment: FAKE_DIR (calls in calls.jsonl as {"cmd": "nft", "argv": [...]}, scenario.json).
"""
import json
import os
import sys


def main(argv):
    with open(os.path.join(os.environ["FAKE_DIR"], "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "nft", "argv": argv}) + "\n")
    try:
        with open(os.path.join(os.environ["FAKE_DIR"], "scenario.json")) as f:
            chains = json.load(f).get("nft_chains") or {}
    except FileNotFoundError:
        chains = {}
    if argv[:4] != ["list", "chain", "inet", "agentbox_vm"] or len(argv) != 5:
        print(f"nft (fake): unsupported: {argv}", file=sys.stderr)
        return 1
    if argv[4] not in chains:
        print(f"Error: No such file or directory\nlist chain inet agentbox_vm {argv[4]}", file=sys.stderr)
        return 1
    print(chains[argv[4]], end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
