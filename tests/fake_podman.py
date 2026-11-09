#!/usr/bin/env python3
"""Stand-in for `podman` in tests of the Builds (agentbox/images.py).

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "podman", "argv": [...], "env": {...}};
             container images live in podman/<tag>/ (rootfs/ and layers.json)

  podman build [--pull=..] [--no-cache] [--build-arg K=V].. [-v ..].. -t TAG -f FILE CONTEXT
      builds TAG by reading FILE as a Containerfile, as far as tests need it:
        FROM ${BASE} / $BASE   the rootfs and layers of the image named by --build-arg BASE
        FROM <anything else>   an empty rootfs (a distribution image)
        COPY <src>... <dest>   from CONTEXT, as podman does (a directory's content, files into a
                               directory ending in /, a single file to a path)
        RUN ...                lines of the Containerfile joined; a RUN naming package-safeguards
                               runs that script of the rootfs against the rootfs with the value
                               of --build-arg PACKAGE_MIN_AGE_DAYS; `rm -f|-rf <path>...` is
                               applied; every other RUN takes effect only through run.json
        ARG, others            ignored
      Every instruction adds a layer.
  podman image mount TAG       prints the rootfs directory
  podman image unmount TAG
  podman image inspect TAG     [{"Id": ..., "RootFS": {"Layers": [...]}}]
  podman image exists TAG      exit 0 or 1
  podman rmi [--ignore] TAG..  removes them
  podman run ... bootc-image-builder ... TAG
                               writes <dir of -v ..:/output>/qcow2/disk.qcow2, a fake disk naming TAG
  podman run --rm --name=N ... TAG dnf -q check-update
                               the package updates for TAG (agentbox check-versions): as
                               check_update in scenario.json says, else none (exit 0); an
                               unknown TAG fails (exit 125). With "sleep" it takes that long,
                               and ends at once with exit 137 when TAG is removed meanwhile
                               (podman rmi --force ends the containers of an image)
  podman rm --force --ignore N exit 0

run.json (optional): {"<text in a RUN line>": {"<path in the image>": "<content>" or
                       {"symlink": "<target>"} or {"content": "...", "mode": 493} or
                       {"base64": "<binary content>"}}}

scenario.json:
  podman_fails       {"build": [TAG prefixes], "run": [...], "mount": [...]}: those calls fail
  check_update       {"<TAG prefix>": {"exit": 100, "stdout": "<dnf's lines>", "stderr": "...", "sleep": <seconds>}}
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

DIR = os.environ["FAKE_DIR"]
STORE = os.path.join(DIR, "podman")


def scenario():
    try:
        with open(os.path.join(DIR, "scenario.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def fails(verb, tag):
    return any(tag.startswith(p) for p in scenario().get("podman_fails", {}).get(verb, []))


def image_dir(tag):
    return os.path.join(STORE, re.sub(r"[^A-Za-z0-9_.-]", "_", tag))


def copy(src, dest):
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.lexists(dest) and not os.path.isdir(dest):
            os.remove(dest)
        shutil.copy2(src, dest, follow_symlinks=False)


def inside(rootfs, path):
    return os.path.join(rootfs, path.lstrip("/"))


def instructions(containerfile):
    lines, current = [], ""
    with open(containerfile) as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not current and (not line.strip() or line.lstrip().startswith("#")):
                continue
            if line.endswith("\\"):
                current += line[:-1] + " "
                continue
            lines.append((current + line).strip())
            current = ""
    return [(l.split(None, 1)[0].upper(), l.split(None, 1)[1] if " " in l else "") for l in lines]


def effects(run, rootfs):
    try:
        with open(os.path.join(DIR, "run.json")) as f:
            table = json.load(f)
    except FileNotFoundError:
        table = {}
    for text, files in table.items():
        if text not in run:
            continue
        for path, what in files.items():
            dest = inside(rootfs, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.lexists(dest):
                os.remove(dest)
            if isinstance(what, dict) and "symlink" in what:
                os.symlink(what["symlink"], dest)
                continue
            if isinstance(what, dict) and "base64" in what:
                with open(dest, "wb") as f:
                    f.write(base64.b64decode(what["base64"]))
                os.chmod(dest, what.get("mode", 0o644))
                continue
            content = what["content"] if isinstance(what, dict) else what
            with open(dest, "w") as f:
                f.write(content)
            os.chmod(dest, what.get("mode", 0o644) if isinstance(what, dict) else 0o644)


def build(argv):
    args, tag, file, context, i = {}, None, None, None, 0
    while i < len(argv):
        a = argv[i]
        if a == "--build-arg":
            k, _, v = argv[i + 1].partition("=")
            args[k] = v
            i += 2
        elif a in ("-v", "--volume"):
            i += 2
        elif a == "-t":
            tag, i = argv[i + 1], i + 2
        elif a == "-f":
            file, i = argv[i + 1], i + 2
        elif a.startswith("-"):
            i += 1
        else:
            context, i = a, i + 1
    if fails("build", tag):
        print(f"Error: building at STEP \"RUN dnf -y install\": exit status 1", file=sys.stderr)
        return 1
    out = image_dir(tag)
    shutil.rmtree(out, ignore_errors=True)
    rootfs = os.path.join(out, "rootfs")
    os.makedirs(rootfs)
    layers = []
    for verb, rest in instructions(file):
        if verb == "FROM":
            ref = rest.split()[0]
            if ref in ("${BASE}", "$BASE"):
                base = image_dir(args["BASE"])
                if not os.path.isdir(base):
                    print(f"Error: {args['BASE']}: image not known", file=sys.stderr)
                    return 125
                shutil.rmtree(rootfs)
                shutil.copytree(os.path.join(base, "rootfs"), rootfs, symlinks=True)
                with open(os.path.join(base, "layers.json")) as f:
                    layers = json.load(f)
            else:
                layers = ["sha256:" + hashlib.sha256(ref.encode()).hexdigest()]
        elif verb == "COPY":
            *sources, dest = rest.split()
            for s in sources:
                src = os.path.join(context, s.rstrip("/"))
                if os.path.isdir(src):
                    copy(src, inside(rootfs, dest))
                elif dest.endswith("/"):
                    copy(src, os.path.join(inside(rootfs, dest), os.path.basename(src)))
                else:
                    copy(src, inside(rootfs, dest))
        elif verb == "RUN":
            m = re.search(r"(/usr/lib/agentbox/package-safeguards)\s", rest + " ")
            if m:
                r = subprocess.run(["sh", inside(rootfs, m.group(1)), args.get("PACKAGE_MIN_AGE_DAYS", "2"), rootfs])
                if r.returncode != 0:
                    return r.returncode
            for m in re.finditer(r"\brm -r?f ((?:/\S+ ?)+)", rest):
                for path in m.group(1).split():
                    p = inside(rootfs, path)
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    elif os.path.lexists(p):
                        os.remove(p)
            effects(rest, rootfs)
        if verb != "ARG":
            layers.append("sha256:" + hashlib.sha256(f"{tag} {len(layers)} {verb} {rest}".encode()).hexdigest())
    with open(os.path.join(out, "layers.json"), "w") as f:
        json.dump(layers, f)
    print(f"COMMIT {tag}")
    return 0


def check_update(argv):
    tag = argv[argv.index("dnf") - 1]
    if fails("run", tag):
        print("Error: crun: executable file not found", file=sys.stderr)
        return 127
    if not os.path.isdir(image_dir(tag)):
        print(f"Error: {tag}: image not known", file=sys.stderr)
        return 125
    for prefix, answer in scenario().get("check_update", {}).items():
        if tag.startswith(prefix):
            end = time.monotonic() + answer.get("sleep", 0)
            while time.monotonic() < end:
                if not os.path.isdir(image_dir(tag)):
                    print("Error: container was stopped: its image was removed", file=sys.stderr)
                    return 137
                time.sleep(0.05)
            sys.stdout.write(answer.get("stdout", ""))
            sys.stderr.write(answer.get("stderr", ""))
            return answer.get("exit", 0)
    return 0


def builder(argv):
    output = next(v.split(":")[0] for v in argv if v.endswith(":/output"))
    tag = argv[-1]
    if fails("run", tag):
        print("error: osbuild failed", file=sys.stderr)
        return 1
    os.makedirs(os.path.join(output, "qcow2"), exist_ok=True)
    with open(os.path.join(output, "qcow2", "disk.qcow2"), "w") as f:
        json.dump({"built-from": tag}, f)
    return 0


def main(argv):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "podman", "argv": argv, "env": dict(os.environ)}) + "\n")
    if argv[:1] == ["build"]:
        return build(argv[1:])
    if argv[:1] == ["run"]:
        return check_update(argv) if "dnf" in argv else builder(argv[1:])
    if argv[:2] == ["image", "mount"]:
        if fails("mount", argv[2]) or not os.path.isdir(image_dir(argv[2])):
            print(f"Error: {argv[2]}: image not known", file=sys.stderr)
            return 125
        print(os.path.join(image_dir(argv[2]), "rootfs"))
        return 0
    if argv[:2] == ["image", "unmount"]:
        return 0
    if argv[:2] == ["image", "exists"]:
        return 0 if os.path.isdir(image_dir(argv[2])) else 1
    if argv[:2] == ["image", "inspect"]:
        try:
            with open(os.path.join(image_dir(argv[2]), "layers.json")) as f:
                layers = json.load(f)
        except FileNotFoundError:
            print(f"Error: {argv[2]}: image not known", file=sys.stderr)
            return 125
        print(json.dumps([{"Id": hashlib.sha256(argv[2].encode()).hexdigest(), "RootFS": {"Layers": layers}}]))
        return 0
    if argv[:1] == ["rm"]:
        return 0
    if argv[:1] == ["rmi"]:
        for tag in (a for a in argv[1:] if not a.startswith("-")):
            shutil.rmtree(image_dir(tag), ignore_errors=True)
        return 0
    print("podman (fake): unexpected arguments", file=sys.stderr)
    return 125


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
