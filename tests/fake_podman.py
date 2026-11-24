#!/usr/bin/env python3
"""Stand-in for `podman` in tests of the Builds (agentbox/images.py).

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "podman", "argv": [...], "env": {...}};
             container images live in podman/<tag>/ (rootfs/ and layers.json)

  podman build [--pull=..] [--no-cache] [--build-arg K=V].. [-v ..].. -t TAG -f FILE CONTEXT
      builds TAG by reading FILE as a Containerfile, as far as tests need it:
        FROM ${BASE} / $BASE   the rootfs and layers of the image named by --build-arg BASE
        FROM ${NAME}           the reference in --build-arg NAME (none: fails, as podman does)
        FROM <anything else>   an empty rootfs (a distribution image) with the files run.json
                               names under a key in its reference ("fedora-bootc")
        COPY <src>... <dest>   from CONTEXT, as podman does (a directory's content, files into a
                               directory ending in /, a single file to a path)
        RUN ...                lines of the Containerfile joined; a RUN naming package-safeguards
                               runs that script of the rootfs against the rootfs with the value
                               of --build-arg PACKAGE_MIN_AGE_DAYS, and one running
                               /tmp/login-environment runs it against the rootfs; `rm -f|-rf <path>...` is
                               applied, and so are `systemctl disable <unit>...` (its links in
                               /etc/systemd/system/*.wants and *.requires go), `systemctl mask
                               <unit>...` and `ln -s /dev/null <path>` (links to /dev/null); every
                               other RUN takes effect only through run.json
        ARG, others            ignored
      Every instruction adds a layer. As podman, it writes "STEP n/m: <instruction>" as it begins
      each instruction from the first FROM on.
  podman image mount TAG       prints the rootfs directory
  podman image unmount TAG
  podman image inspect TAG     [{"Id": ..., "RootFS": {"Layers": [...]}}]
  podman image exists TAG      exit 0 or 1
  podman rmi [--ignore] TAG..  removes them; fails for TAG prefixes in podman_fails "rmi"
                               (as when another image is built on it) and keeps those
  podman pull [--quiet] REF    makes REF exist (an empty image; of fedora-bootc, one with the
                               /usr/lib/os-release of Fedora fedora_bootc_release); fails for
                               REF prefixes in podman_fails "pull"
  podman run ... bootc-image-builder ... TAG
                               writes <dir of -v ..:/output>/qcow2/disk.qcow2, a fake disk naming TAG
  podman run --rm --name=N ... TAG dnf -q check-update
                               the package updates for TAG (agentbox check-versions): as
                               check_update in scenario.json says, else none (exit 0); an
                               unknown TAG fails (exit 125). With "sleep" it takes that long,
                               and ends at once with exit 137 when TAG is removed meanwhile
                               (podman rmi --force ends the containers of an image)
  podman rm --force --ignore N exit 0
  podman save --format oci-archive -o FILE TAG
                               writes FILE: a tar of TAG's directory (image/) and its name (name)
  podman load -i FILE          the image of such a tar (or one fake_skopeo.py wrote) under its name;
                               prints "Loaded image: <name>"
  podman tag SRC DST           DST becomes a copy of SRC

run.json (optional): {"<text in a RUN line>": {"<path in the image>": "<content>" or
                       {"symlink": "<target>"} or {"content": "...", "mode": 493} or
                       {"base64": "<binary content>"}}}

scenario.json:
  podman_fails       {"build": [TAG prefixes], "run": [...], "mount": [...], "pull": [...],
                      "rmi": [...]}: those calls fail
  build_waits        {"step": n, "file": "<name in FAKE_DIR>"}: podman build waits after step n (its
                     STEP line and its output) until that file exists (at most 30 seconds)
  run_prints         {"<text in a RUN line>": "<output>"}: what that RUN step writes
  check_update       {"<TAG prefix>": {"exit": 100, "stdout": "<dnf's lines>", "stderr": "...", "sleep": <seconds>}}
  fedora_bootc_release  the VERSION_ID a pulled fedora-bootc names (default "44", what
                     quay.io/fedora/fedora-bootc:44 is)
"""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
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


def stdin_context(call):
    """The tar a Recipe Build pipes to podman; record it and unpack it only in the fake."""
    data = sys.stdin.buffer.read()
    temporary = tempfile.TemporaryDirectory(prefix="build-context-", dir=DIR)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        containerfile = None
        for member in members:
            name = os.path.normpath(member.name)
            path = os.path.join(temporary.name, name)
            if member.isdir():
                os.makedirs(path, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with archive.extractfile(member) as source, open(path, "wb") as target:
                    shutil.copyfileobj(source, target)
                if name == "Containerfile":
                    containerfile = read_text(path)
        call["context"] = {"sha256": hashlib.sha256(data).hexdigest(),
                           "members": [{"name": m.name, "size": m.size} for m in members],
                           "containerfile": containerfile}
    return temporary.name, temporary


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def wait_for(path):
    end = time.monotonic() + 30
    while not os.path.exists(path) and time.monotonic() < end:
        time.sleep(0.05)


def build(argv, call):
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
        elif a == "-":
            context, i = a, i + 1
        elif a.startswith("-"):
            i += 1
        else:
            context, i = a, i + 1
    if context == "-":
        context, temporary = stdin_context(call)       # cleaned up when this call returns
    if file is None:
        file = os.path.join(context, "Containerfile")
    if fails("build", tag):
        print(f"Error: building at STEP \"RUN dnf -y install\": exit status 1", file=sys.stderr)
        return 1
    out = image_dir(tag)
    shutil.rmtree(out, ignore_errors=True)
    rootfs = os.path.join(out, "rootfs")
    os.makedirs(rootfs)
    layers = []
    steps = instructions(file)
    # as podman: an ARG before the first FROM is no step
    first = next((n for n, (verb, _) in enumerate(steps) if verb == "FROM"), 0)
    waits = scenario().get("build_waits") or {}
    for number, (verb, rest) in enumerate(steps):
        if number >= first:
            print(f"STEP {number - first + 1}/{len(steps) - first}: {verb} {rest}", flush=True)
        if verb == "FROM":
            ref = rest.split()[0]
            m = re.fullmatch(r"\$\{?(\w+)\}?", ref)
            if m and m.group(1) != "BASE":
                if not args.get(m.group(1)):
                    print(f"Error: base name ({ref}) should not be blank", file=sys.stderr)
                    return 125
                ref = args[m.group(1)]
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
                effects(ref, rootfs)
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
            for text, output in (scenario().get("run_prints") or {}).items():
                if text in rest:
                    print(output, end="", flush=True)
            m = re.search(r"(/usr/lib/agentbox/package-safeguards)\s", rest + " ")
            if m:
                r = subprocess.run(["sh", inside(rootfs, m.group(1)), args.get("PACKAGE_MIN_AGE_DAYS", "2"), rootfs])
                if r.returncode != 0:
                    return r.returncode
            m = re.search(r"python3 -I (/tmp/login-environment)\b", rest)
            if m:
                r = subprocess.run([sys.executable, "-I", inside(rootfs, m.group(1)), rootfs])
                if r.returncode != 0:
                    return r.returncode
            for m in re.finditer(r"\brm -r?f ((?:/\S+ ?)+)", rest):
                for path in m.group(1).split():
                    p = inside(rootfs, path)
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    elif os.path.lexists(p):
                        os.remove(p)
            for m in re.finditer(r"\bsystemctl disable ((?:[\w@.\\-]+ ?)+)", rest):
                etc = inside(rootfs, "/etc/systemd/system")
                for unit in m.group(1).split():
                    for d in (os.listdir(etc) if os.path.isdir(etc) else []):
                        link = os.path.join(etc, d, unit)
                        if d.endswith((".wants", ".requires")) and os.path.lexists(link):
                            os.remove(link)
            masks = [f"/etc/systemd/system/{u}" for m in re.finditer(r"\bsystemctl mask ((?:[\w@.\\-]+ ?)+)", rest)
                     for u in m.group(1).split()]
            masks += re.findall(r"\bln -sf? /dev/null (/\S+)", rest)
            for path in masks:
                p = inside(rootfs, path)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                if os.path.lexists(p):
                    os.remove(p)
                os.symlink("/dev/null", p)
            effects(rest, rootfs)
        if verb != "ARG":
            layers.append("sha256:" + hashlib.sha256(f"{tag} {len(layers)} {verb} {rest}".encode()).hexdigest())
        if number >= first and waits.get("step") == number - first + 1:
            wait_for(os.path.join(DIR, waits["file"]))
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


def write_archive(tag, path, source=None):
    """An image archive of the fakes: the directory of the image `source` (default: tag) and the
    name `tag`, as a tar."""
    with tarfile.open(path, "w") as tar:
        data = tag.encode()
        info = tarfile.TarInfo("name")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        tar.add(image_dir(source or tag), arcname="image")


def load(path):
    """The image of an archive of the fakes into the store: its name."""
    with tarfile.open(path) as tar:
        name = tar.extractfile("name").read().decode()
        with tempfile.TemporaryDirectory(dir=DIR) as tmp:
            tar.extractall(tmp, filter="tar")
            shutil.rmtree(image_dir(name), ignore_errors=True)
            os.makedirs(STORE, exist_ok=True)
            shutil.move(os.path.join(tmp, "image"), image_dir(name))
    return name


def record(call):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps(call) + "\n")


def main(argv):
    call = {"cmd": "podman", "argv": argv, "env": dict(os.environ)}
    if argv[:1] == ["build"]:
        code = build(argv[1:], call)
        record(call)
        return code
    record(call)
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
    if argv[:2] == ["image", "inspect"] and argv[2:4] == ["--format", "{{json .RepoDigests}}"]:
        # a reference pulled by a manifest list's digest also carries its platform image's
        # (scenario "manifest_lists": {list digest: platform digest})
        ref = argv[4]
        if not os.path.isdir(image_dir(ref)):
            print(f"Error: {ref}: image not known", file=sys.stderr)
            return 125
        repo, _, digest = ref.partition("@")
        platform = scenario().get("manifest_lists", {}).get(digest)
        entries = [platform] if isinstance(platform, str) else (platform or [])
        print(json.dumps([ref] + [e if "@" in e else f"{repo}@{e}" for e in entries]))
        return 0
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
    if argv[:1] == ["save"]:
        tag, out = argv[-1], argv[argv.index("-o") + 1]
        if fails("save", tag) or not os.path.isdir(image_dir(tag)):
            print(f"Error: {tag}: image not known", file=sys.stderr)
            return 125
        write_archive(tag, out)
        return 0
    if argv[:1] == ["load"]:
        print(f"Loaded image: {load(argv[argv.index('-i') + 1])}")
        return 0
    if argv[:1] == ["tag"]:
        if not os.path.isdir(image_dir(argv[1])):
            print(f"Error: {argv[1]}: image not known", file=sys.stderr)
            return 125
        shutil.rmtree(image_dir(argv[2]), ignore_errors=True)
        shutil.copytree(image_dir(argv[1]), image_dir(argv[2]), symlinks=True)
        return 0
    if argv[:1] == ["pull"]:
        ref = argv[-1]
        if fails("pull", ref):
            print(f"Error: initializing source docker://{ref}: reading manifest: manifest unknown", file=sys.stderr)
            return 125
        os.makedirs(os.path.join(image_dir(ref), "rootfs"), exist_ok=True)
        if "/fedora-bootc@" in ref:
            release = scenario().get("fedora_bootc_release", "44")
            os.makedirs(os.path.join(image_dir(ref), "rootfs", "usr", "lib"), exist_ok=True)
            with open(os.path.join(image_dir(ref), "rootfs", "usr", "lib", "os-release"), "w") as f:
                f.write(f'NAME="Fedora Linux"\nID=fedora\nVERSION_ID={release}\n')
        with open(os.path.join(image_dir(ref), "layers.json"), "w") as f:
            json.dump(["sha256:" + hashlib.sha256(ref.encode()).hexdigest()], f)
        print("sha256:" + hashlib.sha256(ref.encode()).hexdigest())
        return 0
    if argv[:1] == ["rmi"]:
        code = 0
        for tag in (a for a in argv[1:] if not a.startswith("-")):
            if fails("rmi", tag):
                print(f"Error: image used by another image: {tag}: image has dependent child images",
                      file=sys.stderr)
                code = 2
                continue
            shutil.rmtree(image_dir(tag), ignore_errors=True)
        return code
    print("podman (fake): unexpected arguments", file=sys.stderr)
    return 125


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
