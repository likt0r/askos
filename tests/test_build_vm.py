"""The build VMs' image (ADR 0025, #144, agentbox/build_vm.py): the signed Fedora Cloud Base
Generic qcow2 of the release fedora-bootc's tag names, found and downloaded by the version
check's fetch as agentbox-feeds, checked by root (gpgv as agentbox-gpgv against the Host's own
Fedora key, the SHA-256 of the signed CHECKSUM, the qcow2 header) and kept under
/var/lib/agentbox/build-vm; renewed only when the release changes.

The harness of test_versions.py (the real CLI under AGENTBOX_ROOT, fake curl playing
fedoraproject.org), plus fake setpriv, gpgv (fake_gpgv.py: a signature is good when it names the
keyring's bytes and the text) and qemu-img (a fake disk is JSON naming its header fields).
Nothing reaches the network, nothing runs as another user. RealGpgvTest runs the Host's own gpg
and gpgv, when there are, on a throwaway key.

Run from the repo root: python3 -m unittest discover -s tests
"""
import base64
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_gpgv import EXPIRED, TWICE, sign  # noqa: E402
from test_registry import fake  # noqa: E402
from test_versions import AGENTBOX, NOTHING_NEW, REGISTRY, REPO, VERSIONS, VersionsTest  # noqa: E402

KEY = {"44": b"\x99\x02\x0dfedora-44-primary", "43": b"\x99\x02\x0dfedora-43-primary"}
DIRECTORY = "https://download.fedoraproject.org/pub/fedora/linux/releases/{}/Cloud/x86_64/images/"
NAME = "Fedora-Cloud-Base-Generic-{}-{}.x86_64.qcow2"
CHECKSUM = "Fedora-Cloud-{}-{}-x86_64-CHECKSUM"
IMAGE_44 = NAME.format("44", "1.7")
IMAGE_43 = NAME.format("43", "1.6")
BUILD_VM = "var/lib/agentbox/build-vm"
RECORD = f"{BUILD_VM}/image.json"
DOWNLOADS = "var/lib/agentbox/available/build-vm"


def crc24(data):
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def armored(key):
    """The key as /etc/pki/rpm-gpg holds it: an ASCII-armored public key block."""
    b64 = base64.b64encode(key).decode()
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    crc = base64.b64encode(crc24(key).to_bytes(3, "big")).decode()
    return f"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n{body}\n={crc}\n-----END PGP PUBLIC KEY BLOCK-----\n"


def entry(release, name, sha, size, subvariant="Cloud_Base", directory=None):
    """An entry of Fedora's releases.json."""
    return {"version": release, "arch": "x86_64", "variant": "Cloud", "subvariant": subvariant,
            "link": (directory or DIRECTORY.format(release)) + name, "sha256": sha, "size": str(size)}


class BuildVmImageTest(VersionsTest):
    def setUp(self):
        super().setUp()
        fake(os.path.join(self.dir, "bin", "setpriv"), "fake_setpriv.py")
        fake(os.path.join(self.dir, "bin", "unshare"), "fake_unshare.py")
        fake(os.path.join(self.dir, "bin", "gpgv"), "fake_gpgv.py")
        for release, key in KEY.items():
            self.write(f"etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-{release}-primary", armored(key))
        self.fedora_dir = os.path.join(self.dir, "fedora")
        os.makedirs(self.fedora_dir)
        self.publish(IMAGE_44)

    def disk(self, **header):
        """A fake qcow2 (fake_qemu_img.py) with these header fields."""
        return json.dumps({"fedora-cloud": True, **header}).encode()

    def publish(self, name, content=None, release="44", signed_by=None, signed_sha=None, extra=(),
                listed_sha=None, key=None):
        """Fedora publishes `name` with its CHECKSUM, signed by the release's key (or `signed_by`)
        and naming the image's SHA-256 (or `signed_sha`); releases.json lists it (with
        `listed_sha`) besides the entries `extra`."""
        content = self.disk() if content is None else content
        sha = hashlib.sha256(content).hexdigest()
        compose = name.rsplit("-", 1)[1].split(".x86_64")[0]
        checksum = CHECKSUM.format(release, compose)
        text = (f"# {name}: {len(content)} bytes\nSHA256 ({name}) = {signed_sha or sha}\n"
                f"# Fedora-Cloud-Base-GCE-{release}-{compose}.x86_64.tar.gz: 5 bytes\n"
                f"SHA256 (Fedora-Cloud-Base-GCE-{release}-{compose}.x86_64.tar.gz) = {'1' * 64}\n")
        for file, data in ((name, content), (checksum, sign(key or KEY[signed_by or release], text).encode())):
            with open(os.path.join(self.fedora_dir, file), "wb") as f:
                f.write(data)
        self.registry({**REGISTRY, "fedora": [*extra, entry(release, name, listed_sha or sha, len(content))],
                       "downloads": {DIRECTORY.format(release) + name: os.path.join(self.fedora_dir, name),
                                     DIRECTORY.format(release) + checksum: os.path.join(self.fedora_dir, checksum)}})
        return content

    def in_use(self, name, content, release):
        """An image a check took before (of an earlier Release of Agent Box, for another release)."""
        self.write(f"{BUILD_VM}/images/{name}", content.decode())
        self.write(RECORD, json.dumps({"name": name, "release": release,
                                       "sha256": hashlib.sha256(content).hexdigest(), "checked": 1}))

    def kept(self):
        return sorted(os.listdir(self.path(f"{BUILD_VM}/images")))

    def known(self):
        """What the fake curl serves now (registry.json)."""
        with open(os.path.join(self.dir, "registry.json")) as f:
            return json.load(f)

    def image_record(self):
        with open(self.path(RECORD)) as f:
            return json.load(f)

    def downloads(self):
        return [u for u in self.curl() if "download.fedoraproject.org" in u]

    def fetch_only(self):
        code, out, err = self.run_agentbox("fetch-versions")
        self.assertEqual(code, 0, err)

    def refused(self, why, starts=False):
        """check-versions refuses the fetched image for `why` (or a reason starting with it);
        the image of release 43 stays in use, with its record, and no partial copy is left."""
        code, out, err = self.run_agentbox("check-versions")
        self.assertEqual(code, 0, err)
        got = self.record()["build_vm_image"]
        if starts:
            self.assertTrue(got["error"].startswith(why), got["error"])
        else:
            self.assertEqual(got["error"], why)
            self.assertIn(f"{IMAGE_44} refused: {why}; {IMAGE_43} stays in use", out)
        self.assertEqual(got["image"], None)
        self.assertEqual(self.kept(), [IMAGE_43])
        self.assertEqual(self.image_record()["name"], IMAGE_43)
        self.assertIn("build_vm_image", err)            # not everything determined
        return out


class TakeTest(BuildVmImageTest):
    def test_a_good_image_is_checked_and_kept(self):
        content = self.disk()
        out, _ = self.check()
        self.assertEqual(self.kept(), [IMAGE_44])
        with open(self.path(f"{BUILD_VM}/images/{IMAGE_44}"), "rb") as f:
            self.assertEqual(f.read(), content)
        self.assertEqual(os.stat(self.path(f"{BUILD_VM}/images/{IMAGE_44}")).st_mode & 0o777, 0o644)
        # QEMU, as qemu, must reach the files of a build VM below it: search, not listing
        self.assertEqual(os.stat(self.path(BUILD_VM)).st_mode & 0o777, 0o711)
        rec = self.image_record()
        self.assertEqual((rec["name"], rec["release"], rec["sha256"]),
                         (IMAGE_44, "44", hashlib.sha256(content).hexdigest()))
        got = self.record()["build_vm_image"]
        self.assertEqual((got["image"], got["state"], got["error"]), (IMAGE_44, "checked", None))
        self.assertIn(f"Build VM image: checked and took {IMAGE_44}", out)
        # the fetch downloaded the image and its CHECKSUM from the release's directory, HTTPS only
        self.assertEqual(self.downloads(), [DIRECTORY.format("44") + IMAGE_44,
                                            DIRECTORY.format("44") + CHECKSUM.format("44", "1.7")])
        for call in self.calls("curl"):
            if "-o" in call["argv"]:
                self.assertIn("--max-filesize", call["argv"])
                self.assertIn("-m", call["argv"])
        # the header was read with the format named, under limits of memory and CPU time
        info = [c for c in self.calls("qemu-img") if c["argv"][0] == "info"]
        self.assertEqual([c["argv"][:4] for c in info], [["info", "-f", "qcow2", "--output=json"]])
        self.assertEqual(info[0]["limits"], {"as": 1 << 30, "cpu": 30})
        # nothing of it left in the download or as a partial copy
        self.assertFalse([n for n in self.kept() if n.endswith(".part")])

    def test_a_version_2_qcow2_is_kept(self):
        """compat 0.10 has no corrupt flag at all; only a corrupt flag that is set refuses."""
        self.publish(IMAGE_44, content=self.disk(compat="0.10"))
        self.check()
        self.assertEqual(self.kept(), [IMAGE_44])
        self.assertEqual(self.record()["build_vm_image"]["error"], None)

    def test_gpgv_runs_as_a_user_of_its_own_with_the_hosts_key_only(self):
        """agentbox-gpgv, which nothing else runs as: not agentbox-feeds, whose fetch downloaded
        what gpgv checks and whose processes could trace a gpgv of the same uid."""
        self.check()
        setpriv = self.calls("setpriv")
        self.assertEqual(len(setpriv), 1)
        options = setpriv[0]["argv"][:setpriv[0]["argv"].index("--")]
        self.assertEqual(options, ["--reuid=agentbox-gpgv", "--regid=agentbox-gpgv", "--clear-groups",
                                   "--no-new-privs"])
        gpgv = self.calls("gpgv")
        self.assertEqual([c["user"] for c in gpgv], ["agentbox-gpgv"])
        # in a network namespace of its own, entered as root before setpriv drops: no network
        [unshare] = self.calls("unshare")
        self.assertEqual(unshare["argv"][:4], ["--net", "--", "setpriv", "--reuid=agentbox-gpgv"])
        self.assertEqual(gpgv[0]["unshared"], "net")
        # and bounded: memory, processes, file size, CPU time
        self.assertEqual(gpgv[0]["limits"], {"as": 512 << 20, "nproc": 4, "fsize": 1 << 20, "cpu": 30})
        # the keyring is the Host's key of release 44, dearmored, and nothing else
        argv = gpgv[0]["argv"]
        self.assertEqual(argv[argv.index("--output") + 1], "-")
        self.assertFalse(any(a.startswith(self.path("var/lib/agentbox/available")) for a in argv))

    def test_the_same_release_is_neither_downloaded_nor_checked_again(self):
        self.check()
        before = os.stat(self.path(f"{BUILD_VM}/images/{IMAGE_44}"))
        self.forget_calls()
        out, _ = self.check()
        self.assertEqual(self.downloads(), [])
        self.assertIn("https://fedoraproject.org/releases.json", self.curl())
        self.assertEqual((self.calls("gpgv"), [c for c in self.calls("qemu-img") if c["argv"][0] == "info"]), ([], []))
        self.assertEqual(os.stat(self.path(f"{BUILD_VM}/images/{IMAGE_44}")).st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.record()["build_vm_image"]["state"], "in use")
        self.assertEqual(self.fetched()["build_vm_image"], {"release": "44", "image": IMAGE_44, "error": None})
        # once it is in use, the fetch keeps no copy of it
        self.assertEqual(os.listdir(self.path(DOWNLOADS)), [])

    def test_a_new_release_replaces_the_image(self):
        self.in_use(IMAGE_43, self.disk(release=43), "43")
        out, _ = self.check()
        self.assertEqual(self.kept(), [IMAGE_44])
        self.assertEqual(self.image_record()["release"], "44")
        self.assertIn(f"Removed the replaced build VM image {IMAGE_43}", out)

    def test_the_replaced_image_stays_while_a_build_runs(self):
        self.in_use(IMAGE_43, self.disk(release=43), "43")
        lock = self.run_lock()
        out, _ = self.check()
        self.assertEqual(self.kept(), [IMAGE_43, IMAGE_44])
        self.assertEqual(self.image_record()["name"], IMAGE_44)
        self.assertIn("A Build is running: the replaced build VM image stays until the next check", out)
        lock.close()
        self.check()
        self.assertEqual(self.kept(), [IMAGE_44])

    def test_a_failure_of_the_hosts_own_keeps_the_other_answers(self):
        """After the checks passed, the Host fails to keep the image (here its record cannot be
        written: a directory in its place): only the image's answer fails, and versions.json is
        written with everything else."""
        os.makedirs(self.path(RECORD))
        self.fetch_only()
        code, out, err = self.run_agentbox("check-versions")
        self.assertEqual(code, 0, err)
        rec = self.record()
        self.assertTrue(rec["build_vm_image"]["error"].startswith("cannot keep the image: "),
                        rec["build_vm_image"]["error"])
        self.assertEqual(rec["npm"]["claude-code"]["version"], "2.1.7")
        self.assertIn("build_vm_image", err)

    def test_a_copy_the_host_cannot_write_says_so(self):
        """A write error of the copy (here EFBIG: a file size limit below the image's size; on a
        Host, a full disk) is the Host's, not the fetch's file's."""
        self.publish(IMAGE_44, content=self.disk() + b" " * (256 << 10))
        self.fetch_only()
        self.forget_calls()

        def small_files():
            resource.setrlimit(resource.RLIMIT_FSIZE, (128 << 10, 128 << 10))
        r = subprocess.run([AGENTBOX, "check-versions"], env=self.env(), capture_output=True, text=True,
                           timeout=60, preexec_fn=small_files)
        self.assertEqual(r.returncode, 0, r.stderr)
        why = self.record()["build_vm_image"]["error"]
        self.assertEqual(why, f"cannot copy the image to {self.path(BUILD_VM + '/images')}: File too large")
        self.assertFalse(os.path.exists(self.path(RECORD)))
        self.assertEqual(self.kept(), [])

    def test_a_record_without_its_image_is_dropped_and_the_image_fetched_again(self):
        self.check()
        self.check()                # the image in use: the fetch deletes its download
        os.remove(self.path(f"{BUILD_VM}/images/{IMAGE_44}"))
        self.forget_calls()
        self.check()
        self.assertFalse(self.exists(RECORD))
        self.assertEqual(self.record()["build_vm_image"]["error"],
                         "the fetch's CHECKSUM is missing or no regular file of at most 64 KiB")
        self.check()
        self.assertEqual(self.downloads()[:1], [DIRECTORY.format("44") + IMAGE_44])
        self.assertEqual(self.kept(), [IMAGE_44])
        self.assertEqual(self.image_record()["name"], IMAGE_44)

    def test_nothing_boots_it_yet(self):
        self.check()
        self.assertEqual((self.calls("virsh"), self.calls("virt-install")), ([], []))
        self.assertEqual([c for c in self.podman() if c["argv"][0] in ("build", "run")], [])


class RefuseTest(BuildVmImageTest):
    """A refused image changes nothing: the image in use stays, and so does its record."""

    def setUp(self):
        super().setUp()
        self.in_use(IMAGE_43, self.disk(release=43), "43")

    BAD = "the CHECKSUM's signature is not good (gpgv exit 1, BADSIG: gpgv: BAD signature from \"Fedora\")"

    def test_a_signature_by_another_releases_key_is_refused(self):
        self.publish(IMAGE_44, signed_by="43")
        self.fetch_only()
        self.refused(self.BAD)
        # the image was not even copied: the signature comes first
        self.assertFalse([c for c in self.calls("qemu-img") if c["argv"][0] == "info"])

    def test_a_changed_checksum_is_refused(self):
        self.fetch_only()
        path = self.path(f"{DOWNLOADS}/{CHECKSUM.format('44', '1.7')}")
        with open(path) as f:
            doc = f.read()
        with open(path, "w") as f:
            f.write(doc.replace(f"SHA256 ({IMAGE_44}) = ", f"SHA256 ({IMAGE_44}) = 0"))
        self.refused(self.BAD)

    def test_a_checksum_of_another_release_is_refused(self):
        """The CHECKSUM's name comes from the image's, of the release's pattern; one signed for
        release 43 under that name is checked with release 44's key."""
        content = self.disk()
        self.publish(IMAGE_44, content=content)
        self.fetch_only()
        text = f"SHA256 ({IMAGE_44}) = {hashlib.sha256(content).hexdigest()}\n"
        with open(self.path(f"{DOWNLOADS}/{CHECKSUM.format('44', '1.7')}"), "w") as f:
            f.write(sign(KEY["43"], text))
        self.refused(self.BAD)

    def test_a_signature_by_an_expired_or_revoked_key_is_refused(self):
        """gpgv exits 0 with VALIDSIG for these: its other status lines refuse them."""
        for marker, status in EXPIRED.items():
            with self.subTest(status=status):
                key = KEY["44"] + marker
                self.write("etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary", armored(key))
                self.publish(IMAGE_44, key=key)
                self.fetch_only()
                self.refused(f"the CHECKSUM's signature is not good (gpgv exit 0, {status}: "
                             f"gpgv: Good signature from \"Fedora\")")

    def test_more_than_one_signature_is_refused(self):
        key = KEY["44"] + TWICE
        self.write("etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary", armored(key))
        self.publish(IMAGE_44, key=key)
        self.fetch_only()
        self.refused("the CHECKSUM's signature is not good (gpgv exit 0: gpgv: Good signature from \"Fedora\")")

    def test_a_key_with_a_broken_checksum_line_is_no_key(self):
        key = self.path("etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary")
        good = armored(KEY["44"])
        crc = good.splitlines()[-2]
        for line in ("=!!!!", "=AAAA", "=A"):
            with self.subTest(line=line):
                self.write("etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary", good.replace(crc, line))
                self.fetch_only()
                self.refused(f"the Host has no key of Fedora 44 ({key}, package fedora-gpg-keys)")
                self.assertEqual(self.calls("gpgv"), [])

    def test_an_unreadable_record_removes_no_image(self):
        self.write(RECORD, "{broken")
        known = self.known()
        self.registry({**known, "fedora": {"status": 500}})
        self.fetch_only()
        code, out, err = self.run_agentbox("check-versions")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kept(), [IMAGE_43])
        self.assertIn("cannot be read: no build VM image is removed", out)

    def test_without_the_hosts_key_nothing_is_taken(self):
        os.remove(self.path("etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary"))
        key = self.path("etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary")
        self.fetch_only()
        self.refused(f"the Host has no key of Fedora 44 ({key}, package fedora-gpg-keys)")
        self.assertEqual(self.calls("gpgv"), [])

    def test_an_image_that_is_not_the_signed_one_is_refused(self):
        self.publish(IMAGE_44, signed_sha="2" * 64)
        self.fetch_only()
        self.refused("the image does not match the signed CHECKSUM")

    def test_a_checksum_without_the_image_is_refused(self):
        content = self.disk()
        self.publish(IMAGE_44, content=content)
        self.fetch_only()
        path = self.path(f"{DOWNLOADS}/{CHECKSUM.format('44', '1.7')}")
        text = f"SHA256 (Fedora-Cloud-Base-Generic-44-1.8.x86_64.qcow2) = {hashlib.sha256(content).hexdigest()}\n"
        with open(path, "w") as f:
            f.write(sign(KEY["44"], text))
        self.refused(f"the signed CHECKSUM names no single SHA-256 of {IMAGE_44}")

    def test_a_qcow2_that_names_another_file_is_refused(self):
        for header, why in (({"backing-filename": "/etc/shadow"}, "the qcow2 names a backing file"),
                            ({"data-file": "/dev/sda"}, "the qcow2 names an external data file"),
                            ({"encrypted": True}, "the qcow2 is encrypted"),
                            ({"corrupt": True}, "the qcow2 is marked corrupt"),
                            ({"virtual-size": 200 << 30}, "the qcow2's virtual size is not between 1 byte and 20 GiB"),
                            ({"format": "raw"}, "not a qcow2 by qemu-img info: qemu-img: Could not open")):
            with self.subTest(header=header):
                self.publish(IMAGE_44, content=self.disk(**header))
                self.fetch_only()
                self.refused(why, starts="Could not open" in why)


class UntrustedFetchTest(BuildVmImageTest):
    """Root reads what agentbox-feeds left as untrusted input."""

    def setUp(self):
        super().setUp()
        self.in_use(IMAGE_43, self.disk(release=43), "43")

    def fetch_file(self, build_vm_image):
        code, out, err = self.run_agentbox("fetch-versions")
        self.assertEqual(code, 0, err)
        found = self.fetched()
        found["build_vm_image"] = build_vm_image
        self.write("var/lib/agentbox/available/available.json", json.dumps(found))

    def test_a_fetch_that_names_another_release_is_refused_before_anything_runs(self):
        self.write(f"{DOWNLOADS}/{IMAGE_43.replace('1.6', '1.7')}", self.disk().decode())
        for image in (IMAGE_43.replace("1.6", "1.7"), "../../../etc/shadow", f"{IMAGE_44}/x", 44):
            with self.subTest(image=image):
                self.forget_calls()
                self.fetch_file({"release": "43", "image": image, "error": None})
                code, out, err = self.run_agentbox("check-versions")
                self.assertEqual(code, 0, err)
                self.assertEqual(self.record()["build_vm_image"]["error"], "unknown error")
                self.assertEqual((self.calls("gpgv"), self.kept()), ([], [IMAGE_43]))

    def test_an_error_text_of_the_fetch_must_be_one_it_reports(self):
        self.fetch_file({"release": "44", "image": None, "error": "\x1b[31mcall me"})
        self.ok("check-versions")
        self.assertEqual(self.record()["build_vm_image"]["error"], "unknown error")

    def test_a_link_instead_of_the_image_is_not_followed(self):
        self.fetch_file({"release": "44", "image": IMAGE_44, "error": None})
        path = self.path(f"{DOWNLOADS}/{IMAGE_44}")
        target = self.path(f"{BUILD_VM}/images/{IMAGE_43}")
        os.remove(path)
        os.symlink(target, path)
        self.refused(f"the fetch's image is missing or no regular file of at most {2 << 30} bytes")

    def test_a_link_instead_of_the_download_directory_is_not_followed(self):
        self.fetch_file({"release": "44", "image": IMAGE_44, "error": None})
        moved = os.path.join(self.dir, "elsewhere")
        os.rename(self.path(DOWNLOADS), moved)
        os.symlink(moved, self.path(DOWNLOADS))
        self.refused("the fetch's download is missing")

    def test_a_file_without_the_entry_was_not_fetched_yet(self):
        found = {k: v for k, v in self.fetched_or_run().items() if k != "build_vm_image"}
        self.write("var/lib/agentbox/available/available.json", json.dumps(found))
        self.ok("check-versions")
        got = self.record()["build_vm_image"]
        self.assertEqual(got["error"], "not fetched yet; the next fetch asks fedoraproject.org")
        self.assertEqual(self.kept(), [IMAGE_43])

    def fetched_or_run(self):
        self.ok("fetch-versions")
        return self.fetched()


class FetchTest(BuildVmImageTest):
    """The fetch as agentbox-feeds: what it takes from releases.json and downloads."""

    def test_it_takes_only_the_generic_qcow2_of_the_release_from_its_directory(self):
        content = self.disk()
        sha = hashlib.sha256(content).hexdigest()
        decoys = [entry("44", "Fedora-Cloud-Base-UEFI-UKI-44-1.7.x86_64.qcow2", sha, 5, "Cloud_Base_UKI"),
                  entry("43", IMAGE_43, sha, 5),
                  entry("44", IMAGE_44, sha, 5, directory="https://download.fedoraproject.org/pub/alt/"),
                  entry("44", IMAGE_44, sha, 5, directory=DIRECTORY.format("44").replace("https", "http")),
                  entry("44", "Fedora-Cloud-Base-AmazonEC2-44-1.7.x86_64.raw.xz", sha, 5)]
        self.publish(IMAGE_44, content=content, extra=decoys)
        self.ok("fetch-versions")
        self.assertEqual(self.fetched()["build_vm_image"], {"release": "44", "image": IMAGE_44, "error": None})
        self.assertEqual(sorted(os.listdir(self.path(DOWNLOADS))), sorted([IMAGE_44, CHECKSUM.format("44", "1.7")]))
        # only the decoys: no image of the release
        self.registry({**REGISTRY, "fedora": decoys})
        self.ok("fetch-versions")
        self.assertEqual(self.fetched()["build_vm_image"]["error"],
                         "no Cloud Base Generic qcow2 of the release in releases.json")

    def test_a_download_that_is_not_the_listed_one_is_dropped(self):
        self.publish(IMAGE_44, listed_sha="3" * 64)
        self.ok("fetch-versions")
        self.assertEqual(self.fetched()["build_vm_image"]["error"], "the download does not match releases.json")
        self.assertEqual(os.listdir(self.path(DOWNLOADS)), [])

    def test_a_download_larger_than_the_limit_is_dropped(self):
        for flood in (False, True):
            with self.subTest(flood=flood):
                content = self.publish(IMAGE_44)
                if flood:
                    known = self.known()
                    known["downloads"][DIRECTORY.format("44") + IMAGE_44] = {"flood": True}
                    self.registry(known)
                r = self.fetch_with(AGENTBOX_BUILD_VM_IMAGE_BYTES=str(len(content) - 1))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.fetched()["build_vm_image"]["error"], "download of the image failed")
                self.assertEqual(os.listdir(self.path(DOWNLOADS)), [])

    def test_fedoraproject_org_not_answering_leaves_the_image_in_use(self):
        self.check()
        for answer, why in (({"status": 500}, "fedoraproject.org answered 500"), (None, "fedoraproject.org answered 404")):
            with self.subTest(why=why):
                known = self.known()
                known.pop("fedora")
                if answer:
                    known["fedora"] = answer
                self.registry(known)
                code, out, err = self.run_agentbox("fetch-versions")
                self.assertEqual(code, 0, err)
                self.ok("check-versions")
                got = self.record()["build_vm_image"]
                self.assertEqual((got["image"], got["error"]), (IMAGE_44, why))
                self.assertEqual(self.kept(), [IMAGE_44])

    def test_a_failed_image_step_keeps_the_other_answers(self):
        """The image's error is its own: a download directory it cannot make (here a file in its
        place) fails only the image."""
        self.write(DOWNLOADS, "not a directory")
        r = self.fetch_with()
        self.assertEqual(r.returncode, 0, r.stderr)
        found = self.fetched()
        self.assertEqual(found["build_vm_image"], {"release": "44", "image": IMAGE_44,
                                                   "error": "download of the image failed"})
        self.assertEqual(found["npm"]["claude-code"], {"version": "2.1.7", "error": None})
        self.assertEqual(found["orca"]["version"], "1.4.251")

    def test_a_fetch_killed_during_the_image_download_keeps_the_other_answers(self):
        """As systemd ends the unit at its time limit: what was fetched before the image stands,
        and the image is what did not finish."""
        known = self.known()
        known["downloads"][DIRECTORY.format("44") + IMAGE_44] = {"kill_parent": True}
        self.registry(known)
        r = self.fetch_with()
        self.assertEqual(r.returncode, -9, r.stderr)
        found = self.fetched()
        self.assertEqual(found["build_vm_image"], {"release": "44", "image": None,
                                                   "error": "the image's download did not finish"})
        self.assertEqual(found["npm"]["claude-code"], {"version": "2.1.7", "error": None})
        self.assertIsNotNone(found["digests"]["fedora-bootc"]["digest"])
        self.ok("check-versions")
        rec = self.record()
        self.assertEqual(rec["build_vm_image"]["error"], "the image's download did not finish")
        self.assertEqual(rec["npm"]["claude-code"]["version"], "2.1.7")

    def fetch_with(self, **extra):
        return subprocess.run([AGENTBOX, "fetch-versions"], env=self.env(**extra), capture_output=True, text=True,
                              timeout=60)


class NoUpdateTest(BuildVmImageTest):
    def test_a_new_image_starts_no_build(self):
        """Like a new digest (#144, 2026-11-23): with auto_update on and nothing else new, the
        check takes the image and starts nothing."""
        self.ok("regenerate")
        self.built()
        with open(os.path.join(self.dir, "registry.json")) as f:
            known = json.load(f)
        self.registry({**known, **NOTHING_NEW})
        self.forget_calls()
        self.ok("fetch-versions")
        out = self.ok("--actor=timer", "check-versions")
        self.assertIn(f"Build VM image: checked and took {IMAGE_44}", out)
        self.assertIn("Automatic update: no Image needs a new Build", out)
        self.assertEqual([c for c in self.systemctl() if c[:2] == ["start", "--no-block"]], [])


class FetchTimeLimitTest(unittest.TestCase):
    """The fetch may now download the image: its unit's limit leaves curl's download time, and
    Update now waits for the unit longer than that limit, so the root part never reads the
    fetch's files while it still writes them."""

    def test_update_now_waits_for_the_fetch_longer_than_its_unit_may_run(self):
        from unittest import mock
        sys.path.insert(0, REPO)
        from agentbox import build_vm, generate, images, versions
        unit = generate.VERSIONS_FETCH_SERVICE.format(program="/usr/bin/agentbox", days=2, orca="", playwright="",
                                                      timeout=versions.FETCH_SECONDS)
        self.assertIn(f"TimeoutStartSec={versions.FETCH_SECONDS}s\n", unit)
        self.assertGreaterEqual(versions.FETCH_SECONDS, build_vm.DOWNLOAD_SECONDS + 10 * 60)
        with mock.patch.object(images.programs, "systemctl", return_value=mock.Mock(returncode=0)) as systemctl, \
                mock.patch.object(images.versions, "check") as check:
            images._check_versions(lambda line: None)
        self.assertEqual(systemctl.call_args.args, ("start", versions.FETCH_UNIT))
        self.assertGreater(systemctl.call_args.kwargs["timeout"], versions.FETCH_SECONDS)
        check.assert_called_once()


class VersionsRecordTest(BuildVmImageTest):
    def test_check_versions_json_names_the_image(self):
        self.ok("fetch-versions")
        code, out, err = self.run_agentbox("check-versions", "--json")
        self.assertEqual(code, 0, err)
        got = json.loads(out)["build_vm_image"]
        self.assertEqual({k: got[k] for k in ("release", "image", "offered", "state", "error")},
                         {"release": "44", "image": IMAGE_44, "offered": IMAGE_44, "state": "checked", "error": None})
        with open(self.path(VERSIONS)) as f:
            self.assertEqual(json.load(f)["build_vm_image"], got)


@unittest.skipUnless(shutil.which("gpg") and shutil.which("gpgv"), "needs gpg and gpgv")
class RealGpgvTest(unittest.TestCase):
    """The Host's own gpg and gpgv on a throwaway key: build_vm's reading of an armored key as gpg
    exports it, and of what gpgv answers (its status lines), which the fake only imitates.
    gpgv runs through fake_setpriv.py, as the test's own user."""

    def setUp(self):
        sys.path.insert(0, REPO)
        from agentbox import build_vm
        self.build_vm = build_vm
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.bin = os.path.join(self.dir, "bin")
        fake(os.path.join(self.bin, "setpriv"), "fake_setpriv.py")
        fake(os.path.join(self.bin, "unshare"), "fake_unshare.py")
        self.keys = {name: self.new_key(name) for name in ("fedora", "other")}

    def gpg(self, home, *args, data=None):
        r = subprocess.run(["gpg", "--homedir", home, "--batch", "--quiet", "--pinentry-mode", "loopback",
                            "--passphrase", "", *args], input=data, capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def new_key(self, name):
        home = os.path.join(self.dir, name)
        os.mkdir(home, 0o700)
        self.addCleanup(subprocess.run, ["gpgconf", "--homedir", home, "--kill", "all"], capture_output=True)
        self.gpg(home, "--quick-gen-key", f"{name} <{name}@example.org>", "ed25519", "sign", "never")
        return home

    def clearsign(self, name, text):
        return self.gpg(self.keys[name], "--clearsign", data=text.encode())

    def signed(self, checksum, armored_key):
        """build_vm.signed_text of `checksum` with `armored_key` as the Host's key."""
        from unittest import mock
        path = os.path.join(self.dir, "RPM-GPG-KEY-fedora-44-primary")
        with open(path, "wb") as f:
            f.write(armored_key)
        env = {"PATH": self.bin + ":" + os.environ["PATH"], "FAKE_DIR": self.dir}
        with mock.patch.object(self.build_vm, "key_file", return_value=path), mock.patch.dict(os.environ, env):
            return self.build_vm.signed_text(checksum)

    def test_an_armored_key_reads_as_gpg_exports_it(self):
        binary = self.gpg(self.keys["fedora"], "--export")
        armored = self.gpg(self.keys["fedora"], "--armor", "--export")
        self.assertEqual(self.build_vm.dearmor(armored), binary)
        self.assertEqual(self.build_vm.dearmor(armored.replace(b"\n", b"\r\n")), binary)
        with_headers = armored.replace(b"-----\n\n", b"-----\nVersion: GnuPG v2\nComment: Fedora 44\n\n", 1)
        self.assertNotEqual(with_headers, armored)
        self.assertEqual(self.build_vm.dearmor(with_headers), binary)

    def test_gpgv_answers_are_read_as_build_vm_expects(self):
        key = self.gpg(self.keys["fedora"], "--armor", "--export")
        text = f"# {IMAGE_44}: 5 bytes\nSHA256 ({IMAGE_44}) = {'a' * 64}\n"
        good = self.clearsign("fedora", text)
        self.assertEqual(self.signed(good, key), (text, None))
        # a changed text
        signed, why = self.signed(good.replace(b"= aaaa", b"= baaa"), key)
        self.assertEqual(signed, None)
        self.assertIn("gpgv exit 1, BADSIG", why)
        # another key's signature: the Host's key is the only one
        signed, why = self.signed(self.clearsign("other", text), key)
        self.assertEqual(signed, None)
        self.assertIn("ERRSIG", why)
        # two clearsigned blocks, both good: gpgv fails them, and so does build_vm
        two = good + self.clearsign("fedora", f"SHA256 ({IMAGE_44}) = {'b' * 64}\n")
        signed, why = self.signed(two, key)
        self.assertEqual(signed, None)
        self.assertIn("ERROR", why)
        # what goes before the block is not signed and not given back
        self.assertEqual(self.signed(b"SHA256 (" + IMAGE_44.encode() + b") = " + b"c" * 64 + b"\n" + good, key),
                         (text, None))


if __name__ == "__main__":
    unittest.main()
