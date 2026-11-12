#!/usr/bin/env python3
"""Stand-in for the agent's rootless `podman` inside a VM, for agentbox-container-images
(the Container image cleanup and its usage recorder). Not the Host's podman (fake_podman.py).

Environment:
  FAKE_GUEST_ROOT  the VM's root; its Podman lives in podman.json there, every call is
                   recorded in podman-calls.jsonl as {"argv": [...], "uid": N}

podman.json:
  images       [{"Id": "<64 hex>", "Names": [...], "Size": N, "Created": N, "ReadOnly": false}]
  containers   [{"Id": "<64 hex>", "Names": [...], "ImageID": "<64 hex>", "State": "running"|"exited"}]
  external     containers of a build (podman ps --external), as containers
  volumes      ["<name>", ...]: only there to see they stay
  events       lines `podman events --format json` prints before it ends
  rmi_refuses  ids `podman rmi` refuses (as for an image a newer one is built on)
  fails        commands that fail with exit 125: "images", "ps", "rmi"
  answer       {"images": "<stdout>"}: printed instead of the real listing

  podman images --format json                 the images, with Containers counted
  podman ps --all --external --format json    containers and build containers
  podman rmi <ref>...                         by id: removes it, refused with more than one name;
                                              by name: untags it, the image goes with its last name;
                                              refused while a container uses it; --force is refused
                                              outright (a test would notice)
  podman image inspect --format {{.Id}} <name>
  podman container inspect --format {{.Image}} <id>
  podman events --format json                 prints the events, then ends
"""
import json
import os
import sys

ROOT = os.environ["FAKE_GUEST_ROOT"]
STATE = os.path.join(ROOT, "podman.json")


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(state):
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(STATE + ".tmp", STATE)


def find(state, ref):
    for image in state.get("images", []):
        if image["Id"] == ref or image["Id"].startswith(ref) and len(ref) >= 12 or ref in image.get("Names", []):
            return image
    return None


def users(state, image):
    return [c for c in state.get("containers", []) + state.get("external", []) if c.get("ImageID") == image["Id"]]


def rmi(state, refs):
    if any(r.startswith("-") for r in refs):
        print(f"Error: options are not expected here: {refs}", file=sys.stderr)
        return 125
    code = 0
    for ref in refs:
        image = find(state, ref)
        if image is None:
            print(f"Error: {ref}: image not known", file=sys.stderr)
            code = 1
            continue
        if image["Id"] in state.get("rmi_refuses", []):
            print(f"Error: image {image['Id']} has dependent children", file=sys.stderr)
            code = 2
            continue
        names = image.get("Names", [])
        if ref in names and len(names) > 1:
            names.remove(ref)
            continue
        if ref not in names and len(names) > 1:
            print(f"Error: unable to delete image {image['Id']} by ID with more than one tag ({names}): please force "
                  "removal", file=sys.stderr)
            code = 2
            continue
        if users(state, image):
            print(f"Error: image used by {users(state, image)[0]['Id']}: image is in use by a container",
                  file=sys.stderr)
            code = 2
            continue
        state["images"].remove(image)
        print(f"Deleted: {image['Id']}")
    save(state)
    return code


def main(argv):
    with open(os.path.join(ROOT, "podman-calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": argv, "uid": os.geteuid()}) + "\n")
    state = load()
    if "--force" in argv or "-f" in argv:
        print("Error: the fake refuses --force", file=sys.stderr)
        return 125
    if argv[:1] and argv[0] in state.get("fails", []):
        print(f"Error: {argv[0]} fails", file=sys.stderr)
        return 125
    if argv == ["images", "--format", "json"]:
        if "images" in state.get("answer", {}):
            print(state["answer"]["images"])
            return 0
        print(json.dumps([{**i, "Containers": len(users(state, i))} for i in state.get("images", [])]))
        return 0
    if argv == ["ps", "--all", "--external", "--format", "json"]:
        print(json.dumps(state.get("containers", []) + state.get("external", [])))
        return 0
    if argv[:1] == ["rmi"]:
        return rmi(state, argv[1:])
    if argv[:4] == ["image", "inspect", "--format", "{{.Id}}"] and len(argv) == 5:
        image = find(state, argv[4])
        if image is None:
            print(f"Error: {argv[4]}: image not known", file=sys.stderr)
            return 125
        print(image["Id"])
        return 0
    if argv[:4] == ["container", "inspect", "--format", "{{.Image}}"] and len(argv) == 5:
        for c in state.get("containers", []):
            if c["Id"] == argv[4] or argv[4] in c.get("Names", []):
                print(c["ImageID"])
                return 0
        print(f"Error: {argv[4]}: no such container", file=sys.stderr)
        return 125
    if argv == ["events", "--format", "json"]:
        for line in state.get("events", []):
            print(line if isinstance(line, str) else json.dumps(line), flush=True)
        return 0
    print(f"Error: the fake does not know: {argv}", file=sys.stderr)
    return 125


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
