#!/usr/bin/env python3
"""Stand-in for `gpgv` checking a clearsigned file in tests.

A fake signature is good when it names the SHA-256 of the keyring's bytes and the signed text
(sign() makes one): so a file signed with another key, or whose text was changed, is BAD. Like
the real gpgv it writes the signed text to --output whatever the signature, takes no armored
keyring ("invalid packet", exit 2) and says VALIDSIG on --status-fd only for a good one.
A key whose bytes end in one of EXPIRED (":expired-key", ":revoked-key", ":expired-sig") makes
a good signature come with EXPKEYSIG, REVKEYSIG or EXPSIG, and still VALIDSIG and exit 0, as
gpgv 2.4.9 answers for a signature by an expired or revoked key or one that expired.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "gpgv", "argv": [...], "user": the
             user fake_setpriv.py ran it as, or null}

  gpgv --homedir <dir> --status-fd 2 --keyring <file> --output - <file>
"""
import hashlib
import json
import os
import sys

DIR = os.environ.get("FAKE_DIR", "")       # tests import sign() from here
HEAD = "-----BEGIN PGP SIGNED MESSAGE-----\n"
SIGNATURE = "-----BEGIN PGP SIGNATURE-----\n"
END = "-----END PGP SIGNATURE-----\n"
EXPIRED = {b":expired-key": "EXPKEYSIG", b":revoked-key": "REVKEYSIG", b":expired-sig": "EXPSIG"}


def signature(key, text):
    return "fake:" + hashlib.sha256(key + text.encode()).hexdigest()


def sign(key, text):
    """A clearsigned file of `text` (ending in a newline) by the binary key `key`."""
    return f"{HEAD}Hash: SHA256\n\n{text}{SIGNATURE}\n{signature(key, text)}\n{END}"


def split(doc):
    """(signed text, signature) of a clearsigned file, or None."""
    if not doc.startswith(HEAD) or SIGNATURE not in doc:
        return None
    head, _, rest = doc[len(HEAD):].partition("\n\n")
    text, _, sig = rest.partition(SIGNATURE)
    sig = sig.split(END)[0].strip()
    return text, sig


def main(argv):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "gpgv", "argv": argv, "user": os.environ.get("FAKE_SETPRIV_USER")}) + "\n")
    opts, files, i = {}, [], 0
    while i < len(argv):
        if argv[i] in ("--homedir", "--status-fd", "--keyring", "--output"):
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            files.append(argv[i])
            i += 1
    if len(files) != 1 or opts.get("--output") != "-" or opts.get("--status-fd") != "2" or "--keyring" not in opts:
        print("gpgv (fake): unexpected arguments", file=sys.stderr)
        return 2
    with open(opts["--keyring"], "rb") as f:
        key = f.read()
    with open(files[0], encoding="utf-8", errors="replace") as f:
        parts = split(f.read())
    if parts is None:
        print("gpgv: no signature found", file=sys.stderr)
        return 2
    text, sig = parts
    sys.stdout.write(text)
    sys.stdout.flush()
    if key.startswith(b"-----BEGIN"):
        print("gpgv: [don't know]: invalid packet (ctb=2d)\ngpgv: Can't check signature: No public key",
              file=sys.stderr)
        return 2
    if sig != signature(key, text):
        print("[GNUPG:] BADSIG 0123456789ABCDEF Fedora\ngpgv: BAD signature from \"Fedora\"", file=sys.stderr)
        return 1
    status = next((s for marker, s in EXPIRED.items() if key.endswith(marker)), "GOODSIG")
    print(f"[GNUPG:] {status} 0123456789ABCDEF Fedora\ngpgv: Good signature from \"Fedora\"\n"
          "[GNUPG:] VALIDSIG 36F612DC0123456789ABCDEF0123456789ABCDEF 2026-04-24", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
