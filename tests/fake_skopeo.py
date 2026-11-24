#!/usr/bin/env python3
"""Stand-in for `skopeo` in tests of the Builds (agentbox/digests.py).

  skopeo inspect --raw containers-storage:REF
      the raw manifest of REF in the fake container storage (fake_podman.image_dir): a manifest
      list when REF's digest is a key of the scenario's "manifest_lists", else an image manifest;
      fails when the image is not there, and for digests in the scenario's "skopeo_fails"

  skopeo copy --quiet containers-storage:REF oci-archive:PATH:NAME
      PATH: an archive of the fakes (fake_podman.write_archive) of REF in the fake container
      storage, named NAME; fails when REF is not there, and for REF prefixes in the scenario's
      "skopeo_copy_fails"

  skopeo copy --quiet oci-archive:PATH containers-storage:REF
      the image of the OCI archive PATH (fake_podman.write_archive) into the fake container
      storage as REF, whatever name the archive gives it; as skopeo with no image named in the
      oci-archive reference, it refuses an archive with more than one image. As skopeo 1.22
      does, it fetches a layer the archive does not hold from the URLs its descriptor names: the
      call records "fetched" (and FAKE_UNSHARE says whether it had a network). It fails when the
      scenario says "skopeo_import_fails"; with "import_fills": n it first allocates n bytes in
      REF's directory of the store, then waits up to 60 s, as a layer that unpacks to much more
      than its size (to be stopped by the Host)

  skopeo --tmpdir DIR ...   the global option: the call also records "tmpdir": {"exists": bool,
      "mode": its permissions}, and the fake unpacks an imported archive there, as skopeo does

Environment: FAKE_DIR (calls in calls.jsonl as {"cmd": "skopeo", "argv": [...]}, scenario.json).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_podman  # noqa: E402


def main(argv):
    call = {"cmd": "skopeo", "argv": argv, "net": os.environ.get("FAKE_UNSHARE") != "net"}
    tmpdir = None
    if argv[:1] == ["--tmpdir"]:
        tmpdir, argv = argv[1], argv[2:]
        call["tmpdir"] = {"exists": os.path.isdir(tmpdir),
                          "mode": os.stat(tmpdir).st_mode & 0o777 if os.path.isdir(tmpdir) else None}
    with open(os.path.join(fake_podman.DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps(call) + "\n")
    if argv[:2] == ["copy", "--quiet"] and len(argv) == 4 and argv[2].startswith("containers-storage:") \
            and argv[3].startswith("oci-archive:"):
        ref = argv[2][len("containers-storage:"):]
        path, _, name = argv[3][len("oci-archive:"):].partition(":")
        if any(ref.startswith(p) for p in fake_podman.scenario().get("skopeo_copy_fails", [])) \
                or not os.path.isdir(fake_podman.image_dir(ref)):
            print(f"FATA[0000] initializing source containers-storage:{ref}: reference does not exist",
                  file=sys.stderr)
            return 1
        fake_podman.write_archive(name, path, source=ref)
        return 0
    if argv[:2] == ["copy", "--quiet"] and len(argv) == 4 and argv[2].startswith("oci-archive:") \
            and argv[3].startswith("containers-storage:"):
        path, ref = argv[2][len("oci-archive:"):], argv[3][len("containers-storage:"):]
        if fake_podman.scenario().get("skopeo_import_fails"):
            print("FATA[0000] writing blob: storing blob to file: digest mismatch", file=sys.stderr)
            return 1
        import tarfile
        if tmpdir:
            with tarfile.open(path) as tar:
                tar.extractall(os.path.join(tmpdir, "oci"), filter="tar")
        index, blobs = fake_podman.read_oci(path)
        if len(index["manifests"]) != 1:
            print(f"FATA[0000] initializing source oci-archive:{path}: loading index: more than one image "
                  "in oci, choose an image", file=sys.stderr)
            return 1
        manifest = json.loads(blobs[index["manifests"][0]["digest"]])
        fetched = [u for layer in manifest["layers"] if layer["digest"] not in blobs for u in layer.get("urls", [])]
        if fetched:
            with open(os.path.join(fake_podman.DIR, "calls.jsonl"), "a") as f:
                f.write(json.dumps({"cmd": "skopeo-fetch", "urls": fetched, "net": call["net"]}) + "\n")
            return 0
        fills = fake_podman.scenario().get("import_fills")
        if fills:
            os.makedirs(fake_podman.image_dir(ref), exist_ok=True)
            fd = os.open(os.path.join(fake_podman.image_dir(ref), "fill"), os.O_WRONLY | os.O_CREAT, 0o644)
            os.posix_fallocate(fd, 0, fills)
            os.close(fd)
            import time
            time.sleep(60)
        fake_podman.load(path, ref)
        return 0
    if argv[:2] != ["inspect", "--raw"] or len(argv) != 3 or not argv[2].startswith("containers-storage:"):
        print(f"fake skopeo: unsupported: {argv}", file=sys.stderr)
        return 2
    ref = argv[2][len("containers-storage:"):]
    repo, _, digest = ref.partition("@")
    scenario = fake_podman.scenario()
    if digest in scenario.get("skopeo_fails", []):
        print("Error: reading manifest: manifest unknown", file=sys.stderr)
        return 1
    lists = scenario.get("manifest_lists", {})
    known = os.path.isdir(fake_podman.image_dir(ref)) or any(
        digest in ([p] if isinstance(p, str) else p) and os.path.isdir(fake_podman.image_dir(f"{repo}@{l}"))
        for l, p in lists.items())
    if not known:
        print(f"Error: {ref}: image not known", file=sys.stderr)
        return 1
    if digest in lists:
        print(json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}))
    else:
        print(json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json", "layers": []}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
