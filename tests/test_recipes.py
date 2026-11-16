"""Recipes (#117, ADR 0018): checked tars, reproducible packing, versions and Builds.

The real CLI runs with AGENTBOX_ROOT and the fake programs of test_images.py. Invalid
archives never reach the stored recipe.tar; Builds hand the checked bytes to fake podman
on stdin, which records the tar's members and Containerfile.
"""
import gzip
import hashlib
import io
import json
import os
import pty
import stat
import subprocess
import sys
import tarfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vm_records  # noqa: E402
from test_images import DERIVED, ImagesTest  # noqa: E402
from test_registry import AGENTBOX  # noqa: E402
from vm_records import record  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from agentbox import images, recipes  # noqa: E402
from agentbox.errors import Fail  # noqa: E402

CF = "# owner supplied\nARG BASE\nFROM ${BASE}\n"
ACTOR = "Firefox/Linux (192.0.2.23)"


def file(name, content=b"", mode=0o644, **extra):
    return {"name": name, "content": content, "mode": mode, "kind": "file", **extra}


def directory(name, mode=0o755):
    return {"name": name, "mode": mode, "kind": "directory"}


def link(name, kind=tarfile.SYMTYPE, target="elsewhere"):
    return {"name": name, "kind": kind, "target": target, "mode": 0o777}


def archive(entries=None, containerfile=CF):
    items = list(entries or [])
    if containerfile is not None and not any(os.path.normpath(item["name"]) == "Containerfile" for item in items):
        items.insert(0, file("Containerfile", containerfile.encode()))
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for item in items:
            info = tarfile.TarInfo(item["name"])
            info.mode = item.get("mode", 0o644)
            info.pax_headers = item.get("pax", {})
            kind = item.get("kind", "file")
            if kind == "directory":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
                continue
            info.type = kind if isinstance(kind, bytes) else tarfile.REGTYPE
            if kind == "file" or kind == tarfile.REGTYPE:
                content = item.get("content", b"")
                if isinstance(content, str):
                    content = content.encode()
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            else:
                info.linkname = item.get("target", "")
                tar.addfile(info)
    return out.getvalue()


class RecipeTest(ImagesTest):
    def run_bytes(self, *args, data=b""):
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "FAKE_VIRSH_DIR": self.dir, "FAKE_DIR": self.dir, "AGENTBOX_ROOT": self.root,
               "LANG": "C.UTF-8", "TZ": "UTC"}
        r = subprocess.run([AGENTBOX, *args], input=data, capture_output=True, env=env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def raw_json(self, *args, data=b"", code=0):
        result = self.run_bytes(*args, "--json", data=data)
        self.assertEqual(result[0], code, result[2])
        return json.loads(result[1] if code == 0 else result[2])

    def set_recipe(self, name="dms", data=None, actor=ACTOR):
        return self.raw_json(f"--actor={actor}", "recipe-set", name,
                             data=archive() if data is None else data)

    def clear_activity(self):
        self.journald.raw()
        self.journald.datagrams.clear()

    def tar_path(self, name="dms"):
        return self.path(f"etc/agentbox/recipes/{name}/recipe.tar")

    def stored(self, name="dms"):
        with open(self.tar_path(name), "rb") as f:
            return f.read()

    def versions(self, name="dms"):
        try:
            return sorted(os.listdir(self.path(f"etc/agentbox/recipes/{name}/versions")))
        except FileNotFoundError:
            return []

    def source_dir(self, cf=CF):
        folder = os.path.join(self.dir, "source")
        os.makedirs(os.path.join(folder, "bin"), exist_ok=True)
        for rel, content, mode in (("Containerfile", cf, 0o600), ("bin/run.sh", "#!/bin/sh\necho ok\n", 0o4711),
                                   ("plain", "text\n", 0o640)):
            path = os.path.join(folder, rel)
            with open(path, "w") as f:
                f.write(content)
            os.chmod(path, mode)
            os.utime(path, (123456789, 123456789))
        return folder


class CheckTest(RecipeTest):
    def refused(self, data, *message):
        code, out, err = self.run_bytes("recipe-set", "dms", "--json", data=data)
        self.assertEqual((code, out), (1, b""), err)
        said = json.loads(err)
        self.assertEqual(said["error"], "invalid_recipe", said)
        for text in message:
            self.assertIn(text, said["message"])
        self.assertFalse(os.path.exists(self.tar_path()))
        return said

    def test_plain_uncompressed_tar_only(self):
        self.refused(gzip.compress(archive()), "plain uncompressed tar")
        self.refused(b"not a tar", "plain uncompressed tar")

    def test_too_many_members(self):
        self.refused(archive([file(f"f{i}") for i in range(recipes.MEMBERS_MAX)]), "512", "f511")

    def test_total_member_size_limit(self):
        huge = b"x" * recipes.CONTENT_MAX
        self.refused(archive([file("huge", huge)]), "64 MiB", "huge")

    def test_input_size_limit_including_tar_overhead(self):
        data = archive() + b"x" * (recipes.INPUT_MAX + 1 - len(archive()))
        self.refused(data, "tar overhead")

    def test_only_regular_files_and_directories(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                self.refused(archive([link("odd", kind)]), "odd", "regular file or directory")

    def test_absolute_member_name(self):
        self.refused(archive([file("/absolute")]), "/absolute", "absolute")

    def test_empty_member_name(self):
        self.refused(archive([file("")]), "invalid Recipe")

    def test_parent_component(self):
        for name in ("../outside", "dir/../../outside", "dir/.."):
            with self.subTest(name=name):
                self.refused(archive([file(name)]), name, "'..'")

    def test_backslash_in_member_name(self):
        self.refused(archive([file("dir\\escape")]), "backslash")

    def test_nul_in_member_name(self):
        self.refused(archive([file("placeholder", pax={"path": "bad\0name"})]), "NUL")

    def test_duplicate_normalized_names(self):
        for first, second in (("tool", "tool"), ("tool", "./tool"), ("dir/tool", "dir//tool"),
                              ("dir/tool", "./dir/tool")):
            with self.subTest(first=first, second=second):
                self.refused(archive([file(first), file(second)]), "duplicate")

    def test_special_mode_bits(self):
        for bit in (stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX):
            with self.subTest(mode=oct(bit)):
                self.refused(archive([file("odd", mode=0o644 | bit)]), "odd", "mode bits")

    def test_top_level_regular_containerfile_required(self):
        self.refused(archive([file("other")], containerfile=None), "Containerfile")
        self.refused(archive([directory("dir/"), file("dir/Containerfile", CF.encode())], containerfile=None),
                     "Containerfile")
        self.refused(archive([link("Containerfile")]), "Containerfile")

    def test_containerfile_size_limit(self):
        content = CF.encode() + b"#" * recipes.CONTAINERFILE_MAX
        self.refused(archive([file("Containerfile", content)]), "256 KiB", "Containerfile")

    def test_containerfile_utf8(self):
        self.refused(archive([file("Containerfile", b"FROM ${BASE}\n\xff")]), "UTF-8")

    def test_first_instruction_must_be_from_base(self):
        for content in ("FROM fedora\n", "RUN true\nFROM ${BASE}\n", "FROM ${OTHER}\n", "FROM ${base}\n",
                        "FROM --platform=linux/amd64 ${BASE}\n", "FROM ${BASE} AS\n"):
            with self.subTest(content=content):
                self.refused(archive([file("Containerfile", content.encode())]), "FROM ${BASE}")

    def test_accepted_from_forms_and_arg_or_comment_lines(self):
        for content in (CF, "\n# comment\nARG BASE=x\nfrom $BASE as builder\n", "FrOm ${BASE} AS Stage\n"):
            with self.subTest(content=content):
                result = self.set_recipe(data=archive([file("Containerfile", content.encode())]))
                self.assertTrue(result["created"])
                self.raw_json("recipe-remove", "dms")

    def test_truncated_file_content_or_archive(self):
        content = CF.encode() + b"#" * 1000
        data = archive([file("Containerfile", content)])
        self.refused(data[:600], "invalid Recipe")

    def test_regular_file_hashes_are_in_the_summary(self):
        payload = b"binary\0content"
        self.set_recipe(data=archive([file("blob", payload)]))
        result = self.raw_json("recipe-show", "dms")
        blob = next(m for m in result["members"] if m["name"] == "blob")
        self.assertEqual(blob["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(blob["kind"], "file")


class PackTest(RecipeTest):
    def two_packs(self, folder):
        first = self.run_bytes("recipe-pack", folder)
        second = self.run_bytes("recipe-pack", folder)
        self.assertEqual((first[0], second[0]), (0, 0), first[2] + second[2])
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2], second[2])
        with tarfile.open(fileobj=io.BytesIO(first[1]), mode="r:") as tar:
            return first[1], tar.getmembers()

    def test_reproducible_normalized_tar(self):
        data, members = self.two_packs(self.source_dir())
        self.assertEqual(hashlib.sha256(data).hexdigest(), recipes.check(data)["sha256"])
        self.assertEqual([m.name for m in members], ["Containerfile", "bin", "bin/run.sh", "plain"])
        for member in members:
            self.assertEqual((member.uid, member.gid, member.uname, member.gname, member.mtime),
                             (0, 0, "root", "root", 0))
            self.assertFalse(member.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX))
        self.assertEqual([m.mode for m in members], [0o644, 0o755, 0o755, 0o644])
        self.assertEqual(self.run_bytes("recipe-set", "dms", data=data)[0], 0)

    def test_symlinks_and_special_files_are_refused_naming_the_path(self):
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                folder = self.source_dir()
                path = os.path.join(folder, "odd-" + kind)
                os.symlink("Containerfile", path) if kind == "symlink" else os.mkfifo(path)
                code, out, err = self.run_bytes("recipe-pack", folder)
                self.assertEqual((code, out), (1, b""))
                self.assertIn(b"invalid Recipe", err)
                self.assertIn(b"odd", err)

    def test_missing_containerfile_is_refused_before_output(self):
        folder = self.source_dir()
        os.remove(os.path.join(folder, "Containerfile"))
        code, out, err = self.run_bytes("recipe-pack", folder)
        self.assertEqual((code, out), (1, b""))
        self.assertIn(b"Containerfile", err)

    def test_recipe_pack_leaves_no_activity_or_host_files(self):
        before = self.activity()
        folder = self.source_dir()
        self.assertEqual(self.run_bytes("recipe-pack", folder)[0], 0)
        self.assertEqual(self.activity(), before)
        self.assertFalse(os.path.exists(self.path("etc/agentbox/recipes")))

    def test_binary_output_is_refused_on_a_terminal(self):
        folder = self.source_dir()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = {"PATH": os.path.join(self.dir, "bin") + ":" + os.environ["PATH"],
               "AGENTBOX_ROOT": self.root, "LANG": "C.UTF-8", "TZ": "UTC"}
        with os.fdopen(slave, "wb"):
            r = subprocess.run([AGENTBOX, "recipe-pack", folder], stdout=slave, stderr=subprocess.PIPE, env=env,
                               timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"redirect the tar to a file or pipe it into sudo agentbox recipe-set <name>", r.stderr)


class StorageTest(RecipeTest):
    def test_set_create_change_unchanged_permissions_and_activity(self):
        first = archive([file("tool", "one")])
        changed = archive([file("tool", "two")])
        result = self.set_recipe(data=first)
        self.assertEqual((result["created"], result["changed"], result["unchanged"]), (True, True, False))
        self.assertEqual(self.stored(), first)
        self.assertEqual(self.mode("etc/agentbox/recipes"), 0o700)
        self.assertEqual(self.mode("etc/agentbox/recipes/dms"), 0o700)
        self.assertEqual(self.mode("etc/agentbox/recipes/dms/recipe.tar"), 0o600)
        unchanged = self.set_recipe(data=first)
        self.assertEqual((unchanged["changed"], unchanged["unchanged"]), (False, True))
        self.assertEqual(self.versions(), [])
        self.assertEqual(len(self.activity("recipe.")), 1)
        result = self.set_recipe(data=changed)
        self.assertEqual((result["created"], result["changed"]), (False, True))
        self.assertEqual(self.stored(), changed)
        self.assertEqual(len(self.versions()), 1)
        with open(self.path(f"etc/agentbox/recipes/dms/versions/{self.versions()[0]}"), "rb") as f:
            self.assertEqual(f.read(), first)
        self.assertEqual(self.mode(f"etc/agentbox/recipes/dms/versions/{self.versions()[0]}"), 0o600)
        entries = self.activity("recipe.")
        self.assertEqual([e["AGENTBOX_ACTION"] for e in entries], ["recipe.set", "recipe.set"])
        for entry, data, created in ((entries[0], first, "true"), (entries[1], changed, None)):
            self.assertEqual((entry["AGENTBOX_KIND"], entry["AGENTBOX_ACTOR"], entry["AGENTBOX_OUTCOME"]),
                             ("build", ACTOR, "ok"))
            self.assertEqual(entry["AGENTBOX_PARAM_NAME"], "dms")
            self.assertEqual(entry["AGENTBOX_PARAM_SHA256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(entry.get("AGENTBOX_PARAM_CREATED"), created)

    def test_only_the_newest_ten_versions_stay(self):
        self.set_recipe()
        old = archive([file("old")])
        folder = self.path("etc/agentbox/recipes/dms/versions")
        os.makedirs(folder, exist_ok=True)
        for day in range(1, 11):
            with open(os.path.join(folder, f"202601{day:02d}-000000.tar"), "wb") as f:
                f.write(old)
        self.set_recipe(data=archive([file("new")]))
        versions = self.versions()
        self.assertEqual(len(versions), 10)
        self.assertNotIn("20260101-000000.tar", versions)
        self.assertIn("20260102-000000.tar", versions)

    def test_restore_moves_the_current_tar_into_versions_and_records_it(self):
        first = archive([file("tool", "one")])
        second = archive([file("tool", "two")])
        self.set_recipe(data=first)
        self.set_recipe(data=second)
        version = self.versions()[0][:-4]
        self.clear_activity()
        result = self.raw_json(f"--actor={ACTOR}", "recipe-restore", "dms", version)
        self.assertTrue(result["restored"])
        self.assertEqual(self.stored(), first)
        versioned = []
        for kept in self.versions():
            with open(self.path(f"etc/agentbox/recipes/dms/versions/{kept}"), "rb") as f:
                versioned.append(f.read())
        self.assertIn(second, versioned)
        [entry] = self.activity("recipe.restore")
        self.assertEqual((entry["AGENTBOX_ACTOR"], entry["AGENTBOX_PARAM_VERSION"],
                          entry["AGENTBOX_PARAM_SHA256"]),
                         (ACTOR, version, hashlib.sha256(first).hexdigest()))

    def test_show_members_text_file_and_kept_version(self):
        first = archive([file("notes", "old\n")])
        second = archive([file("notes", "new\n", mode=0o755)])
        self.set_recipe(data=first)
        self.set_recipe(data=second)
        version = self.versions()[0][:-4]
        shown = self.raw_json("recipe-show", "dms")
        notes = next(m for m in shown["members"] if m["name"] == "notes")
        self.assertEqual((notes["size"], notes["mode"], notes["sha256"]),
                         (4, 0o755, hashlib.sha256(b"new\n").hexdigest()))
        self.assertEqual(self.raw_json("recipe-show", "dms", "--file", "notes")["text"], "new\n")
        self.assertEqual(self.raw_json("recipe-show", "dms", "--version", version, "--file", "notes")["text"],
                         "old\n")
        code, out, _ = self.run_bytes("recipe-show", "dms", "--file", "notes")
        self.assertEqual((code, out), (0, b"new\n"))

    def test_versions_show_added_removed_and_changed_files(self):
        old = archive([file("removed"), file("changed", "old"), file("same")])
        new = archive([file("added"), file("changed", "new"), file("same")])
        first_hash = hashlib.sha256(old).hexdigest()
        self.set_recipe(data=old)
        self.set_recipe(data=new)
        result = self.raw_json("recipe-versions", "dms")
        item = result["versions"][0]
        self.assertEqual((item["added"], item["removed"], item["changed"], item["against"]),
                         (["added"], ["removed"], ["changed"], "current"))
        self.assertEqual(item["sha256"], first_hash)
        self.assertEqual(result["current_sha256"], hashlib.sha256(new).hexdigest())

    def test_remove_refused_while_a_vm_uses_the_image(self):
        self.set_recipe()
        vm_records.write(self.root, record("work", 1, image="dms"), record("priv", 2))
        said = self.raw_json("recipe-remove", "dms", code=1)
        self.assertEqual(said["error"], "image_in_use")
        self.assertTrue(os.path.exists(self.tar_path()))
        [entry] = self.activity("recipe.remove")
        self.assertEqual((entry["AGENTBOX_OUTCOME"], entry["AGENTBOX_ERROR"]), ("failed", "image_in_use"))

    def test_remove_refused_while_a_build_runs(self):
        self.set_recipe()
        self.write("var/lib/agentbox/builds/dms-20261116-100000.json", json.dumps(
            {"image": "dms", "build": "20261116-100000", "status": "running"}))
        said = self.raw_json("recipe-remove", "dms", code=1)
        self.assertEqual(said["error"], "build_running")
        self.assertTrue(os.path.exists(self.tar_path()))

    def test_remove_deletes_recipe_and_versions_but_builds_stay(self):
        self.set_recipe()
        self.set_recipe(data=archive([file("other")]))
        self.write("var/lib/agentbox/images/dms-20261116-100000.qcow2", "disk")
        self.clear_activity()
        result = self.raw_json(f"--actor={ACTOR}", "recipe-remove", "dms")
        self.assertTrue(result["removed"])
        self.assertFalse(os.path.exists(self.path("etc/agentbox/recipes/dms")))
        self.assertIn("its Builds stay", result["message"])
        self.assertTrue(os.path.exists(self.path("var/lib/agentbox/images/dms-20261116-100000.qcow2")))
        [entry] = self.activity("recipe.remove")
        self.assertEqual((entry["AGENTBOX_ACTOR"], entry["AGENTBOX_PARAM_SHA256"]),
                         (ACTOR, result["sha256"]))

    def test_bad_names_and_release_image_names_are_refused(self):
        for name, expected in (("Base", "invalid_argument"), ("base", "invalid_argument"),
                               ("all", "invalid_argument"), ("update", "invalid_argument")):
            with self.subTest(name=name):
                self.assertEqual(self.raw_json("recipe-set", name, code=1)["error"], expected)
        self.image("dms", DERIVED)
        said = self.raw_json("recipe-set", "dms", data=archive(), code=1)
        self.assertEqual(said["error"], "image_exists")
        self.assertFalse(os.path.exists(self.tar_path()))

    def test_missing_or_broken_stored_tar_dies(self):
        self.assertEqual(self.raw_json("recipe-show", "dms", code=1)["error"], "recipe_not_found")
        self.set_recipe()
        self.write("etc/agentbox/recipes/dms/recipe.tar", "broken")
        self.assertEqual(self.raw_json("recipe-show", "dms", code=1)["error"], "invalid_recipe")

    def test_text_file_is_utf8_and_at_most_256_kib(self):
        self.set_recipe(data=archive([file("binary", b"\xff")]))
        self.assertEqual(self.raw_json("recipe-show", "dms", "--file", "binary", code=1)["error"],
                         "invalid_argument")
        huge = b"x" * (recipes.CONTAINERFILE_MAX + 1)
        self.set_recipe(data=archive([file("huge", huge)]))
        self.assertEqual(self.raw_json("recipe-show", "dms", "--file", "huge", code=1)["error"],
                         "invalid_argument")
        self.assertEqual(self.raw_json("recipe-show", "dms", "--file", "missing", code=1)["error"],
                         "invalid_argument")

    def test_recipes_list_name_hash_size_members_versions_vms_and_build(self):
        self.set_recipe()
        vm_records.write(self.root, record("work", 1, image="dms"), record("priv", 2))
        self.write("var/lib/agentbox/images/dms-20261116-100000.qcow2", "")
        os.symlink("dms-20261116-100000.qcow2", self.path("var/lib/agentbox/images/dms.qcow2"))
        item = self.raw_json("recipes")["recipes"][0]
        self.assertEqual((item["name"], item["sha256"], item["size"], item["member_count"],
                          item["versions"], item["vms"], item["current"]),
                         ("dms", hashlib.sha256(archive()).hexdigest(), len(archive()), 1, 0, ["work"],
                          "20261116-100000"))
        self.assertIsNotNone(item["saved"])


class BuildTest(RecipeTest):
    def setUp(self):
        super().setUp()
        self.built()
        self.base = self.newest()["build"]
        self.clear_activity()
        self.forget_calls()

    def test_recipe_build_hands_the_tar_to_podman_and_records_its_hash(self):
        # COPY, ARG and FROM deliberately exercise the context, not a generated Containerfile.
        data = archive([file("Containerfile", b"ARG BASE\nFROM ${BASE}\nCOPY tool /usr/bin/tool\n"),
                        file("tool", "#!/bin/sh\necho from-recipe\n")])
        self.set_recipe(data=data)
        self.built("dms")
        new = self.newest("dms")
        self.assertEqual((new["status"], new["base_build"], new["recipe_sha256"]),
                         ("ok", self.base, hashlib.sha256(data).hexdigest()))
        call = self.podman("build")[-1]
        self.assertEqual(call["argv"][-1], "-")
        self.assertNotIn("-f", call["argv"])
        self.assertIn("--pull=missing", call["argv"])
        self.assertIn("--no-cache", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--build-arg") + 1],
                         f"BASE=localhost/agentbox/base:{self.base}")
        self.assertEqual(call["context"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual([m["name"] for m in call["context"]["members"]], ["Containerfile", "tool"])
        self.assertIn("COPY tool /usr/bin/tool", call["context"]["containerfile"])
        image = self.overview()["dms"]
        self.assertEqual((image["recipe"], image["in_release"], image["containerfile_url"], image["problem"]),
                         (True, False, None, None))
        self.assertIn("Recipe", self.ok("images"))
        shown = self.json("containerfile", "dms")
        self.assertEqual((shown["url"], shown["text"], shown["size"]),
                         (None, "ARG BASE\nFROM ${BASE}\nCOPY tool /usr/bin/tool\n",
                          len("ARG BASE\nFROM ${BASE}\nCOPY tool /usr/bin/tool\n")))
        self.assertIn("current recipe.tar", shown["path"])
        listed = self.raw_json("recipes")["recipes"][0]
        self.assertEqual(listed["current"], new["build"])
        done = next(e for e in self.activity("build.ok") if e.get("AGENTBOX_PARAM_IMAGE") == "dms")
        self.assertEqual(done["AGENTBOX_PARAM_RECIPE_SHA256"], hashlib.sha256(data).hexdigest())
        self.ok("set-image", "priv", "dms")
        self.assertIn(f"dms-{new['build']}.qcow2", self.ok("reset", "priv", "--no-start"))

    def test_changed_safeguard_file_rejects_recipe_and_previous_build_stays(self):
        good = archive()
        self.set_recipe(data=good)
        self.built("dms")
        previous = self.newest("dms")["build"]
        self.clear_activity()
        weak = archive([file("Containerfile", b"FROM ${BASE}\nCOPY npmrc /etc/npmrc\n"),
                        file("npmrc", b"ignore-scripts=false\nmin-release-age=0\n")])
        self.set_recipe(data=weak)
        code, out = self.build("dms")
        self.assertEqual(code, 1, out)
        new = self.newest("dms")
        self.assertEqual((new["status"], new["recipe_sha256"]),
                         ("rejected", hashlib.sha256(weak).hexdigest()))
        self.assertTrue(any("/etc/npmrc differs from the Base image" in p for p in new["problems"]))
        self.assertEqual(self.current("dms"), f"dms-{previous}.qcow2")
        self.assertFalse(os.path.exists(f"{self.path('var/lib/agentbox/images')}/dms-{new['build']}.qcow2"))
        self.assertEqual(self.podman("run")[1:], [])

    def test_updates_select_recipes_and_base_updates_select_every_active_image(self):
        view = {"images": [
            {"name": "base", "update": False, "in_release": True, "recipe": False},
            {"name": "rust", "update": False, "in_release": True, "recipe": False},
            {"name": "dms", "update": True, "in_release": False, "recipe": True},
            {"name": "old", "update": True, "in_release": False, "recipe": False},
        ]}
        self.assertEqual(images.update_images(view), ["dms"])
        view["images"][0]["update"] = True
        self.assertEqual(images.update_images(view), ["base", "rust", "dms"])

    def test_build_all_includes_recipes_after_release_images(self):
        self.image("rust", DERIVED)
        self.set_recipe()
        self.built("all")
        builds = self.podman("build")
        self.assertEqual([call["argv"][-1] for call in builds][-1], "-")
        self.assertEqual(self.podman("build")[-1]["context"]["sha256"], hashlib.sha256(archive()).hexdigest())
        self.assertEqual(self.newest("dms")["status"], "ok")
        self.assertEqual(self.newest("dms")["base_build"], self.newest("base")["build"])

    def test_a_broken_recipe_fails_only_its_own_build_in_build_all(self):
        self.set_recipe()
        self.set_recipe("good")
        # a tar broken on disk after it was saved (a disk error, a hand edit)
        with open(self.tar_path(), "wb") as f:
            f.write(b"not a tar")
        code, out = self.build("all")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.newest("base")["status"], "ok")
        self.assertEqual(self.newest("good")["status"], "ok")
        broken = self.newest("dms")
        self.assertEqual((broken["status"], broken["recipe_sha256"]), ("failed", None))
        self.assertIn("invalid Recipe", broken["error"])
        failed = next(e for e in self.activity("build.failed") if e.get("AGENTBOX_PARAM_IMAGE") == "dms")
        self.assertEqual(failed["AGENTBOX_ERROR"], "invalid_recipe")

    def test_same_name_release_image_wins_and_images_reports_the_problem(self):
        self.set_recipe()
        self.image("dms", DERIVED + "COPY release /usr/bin/release\n", release="release\n")
        self.built("dms")
        call = self.podman("build")[-1]
        self.assertIn("-f", call["argv"])
        self.assertNotIn("context", call)
        image = self.overview()["dms"]
        self.assertEqual((image["recipe"], image["in_release"]), (False, True))
        self.assertIn("Release wins", image["problem"])
        self.assertIn("Problem:", self.ok("images"))
        with mock.patch.object(recipes.images, "release_defined", return_value=["base", "dms"]):
            with self.assertRaises(Fail) as raised:
                recipes.read("dms")
        self.assertEqual(raised.exception.error, "image_exists")
