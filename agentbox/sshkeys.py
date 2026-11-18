"""SSH keys and the host key of a VM's SSH access (ADR 0020).

A VM's record holds whether it has SSH access (`"ssh_access"`, off when missing) and its SSH
keys (`"ssh_keys"`: key, name, fingerprint, added, added_by), registry.py. Every key is checked
here on the Host before it is stored, so that OpenSSH in the VM takes it: one of TYPES (RSA of
MIN_RSA_BITS to MAX_RSA_BITS, its numbers in their shortest form; an ECDSA point on its curve),
its base64 decodes and names the same type, no options before it, at most MAX_KEYS per VM
that fit into the fw_cfg entry together (MAX_ENTRY) and no duplicate by its SHA256
fingerprint, which is computed here as OpenSSH computes it (no new dependency, ADR 0006). The
checks the VM makes again (fwcfg-ssh) are plausibility checks only (ADR 0019).

The host key is an Ed25519 key the Host makes once per VM (generate.py makes its secret with
openssl; host_key_file() writes it in OpenSSH's own format, which sshd reads). It reaches the
VM with the keys in one fw_cfg entry (fw_cfg()): `none` without SSH access, else a line
`host_key <base64 of the key file>` and one line `key <type> <base64>` per key.

Only the standard library, so the Control Room may load it with registry.py.
"""
import base64
import binascii
import hashlib
import os
import re
import struct
import unicodedata

from .errors import Fail, die
from .sanitize import clean

TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
         "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-rsa")
MIN_RSA_BITS = 3072
MAX_RSA_BITS = 16384                 # OpenSSH's own limit
MAX_KEYS = 16
# bytes of the fw_cfg entry; fwcfg-ssh in the VM refuses a longer one (its MAX_ENTRY, the same
# number). Of it, HOST_KEY_LINE is kept for the host key's line (under 700 bytes: an Ed25519
# key named after a VM of at most 16 characters), the rest is for the lines of the keys.
MAX_ENTRY = 64 * 1024
HOST_KEY_LINE = 1024
MAX_KEY_BYTES = MAX_ENTRY - HOST_KEY_LINE
MAX_NAME = 100
MAX_INPUT = 16384                    # bytes of one key line; an RSA key of 16384 bits needs ~2.8 KiB
# what a key type looks like (ssh-dss, a certificate, ...): a word of this form that is not the
# first of the line means options before the key
TYPE_LIKE = re.compile(r"(ssh|ecdsa|sk)-[A-Za-z0-9@._-]+")
# the length of an ECDSA public point (uncompressed: 0x04, then both coordinates) per curve
EC_POINT = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
# the curves themselves, y^2 = x^3 - 3x + b modulo p (FIPS 186-4, D.1.2): (p, b, bits of the
# order of their group)
CURVES = {
    "nistp256": (2**256 - 2**224 + 2**192 + 2**96 - 1,
                 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b, 256),
    "nistp384": (2**384 - 2**128 - 2**96 + 2**32 - 1,
                 0xb3312fa7e23ee7e4988e056be3f82d19181d9c6efe8141120314088f5013875ac656398d8a2ed19d2a85c8edd3ec2aef,
                 384),
    "nistp521": (2**521 - 1,
                 0x51953eb9618e1c9a1f929a21a0b68540eea2da725b99b315f3b8b489918ef109e156193951ec7e937b1652c0bd3bb1bf073573df883d2c34f1ef451fd46b503f00,
                 521),
}
FIELDS = ("key", "name", "fingerprint", "added", "added_by")
HOST_KEY_TYPE = "ssh-ed25519"
HOST_KEY_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
HOST_KEY_END = "-----END OPENSSH PRIVATE KEY-----"
OPENSSH_MAGIC = b"openssh-key-v1\0"


def _string(blob, pos):
    """The SSH wire string at pos: (bytes, next position); None past the end."""
    if pos + 4 > len(blob):
        return None
    n = struct.unpack(">I", blob[pos:pos + 4])[0]
    if pos + 4 + n > len(blob):
        return None
    return blob[pos + 4:pos + 4 + n], pos + 4 + n


def _strings(blob):
    """Every SSH wire string of a key blob; None when it does not divide into them."""
    out, pos = [], 0
    while pos < len(blob):
        got = _string(blob, pos)
        if got is None:
            return None
        out.append(got[0])
        pos = got[1]
    return out


def _ssh(data):
    return struct.pack(">I", len(data)) + data


def fingerprint(blob):
    """OpenSSH's SHA256 fingerprint of a key blob: SHA256:<base64 without padding>."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _invalid(why):
    die(f"not a valid SSH public key: {why}", error="ssh_key_invalid")


def _mpint(data):
    """The positive number of an SSH mpint in its shortest form (RFC 4251: a leading zero byte
    only before a byte with its high bit set), else None: one number has one encoding, so one
    key has one fingerprint."""
    if not data or data[0] & 0x80 or data[0] == 0 and (len(data) == 1 or not data[1] & 0x80):
        return None
    return int.from_bytes(data, "big")


def _on_curve(curve, point):
    """Whether the uncompressed point is one OpenSSH takes as a public key of the curve
    (sshkey_ec_validate_public): on the curve, both coordinates below p and longer than half
    the bits of the group's order. The curves have cofactor 1, so every point on one is in
    its group."""
    p, b, bits = CURVES[curve]
    size = (len(point) - 1) // 2
    x, y = int.from_bytes(point[1:1 + size], "big"), int.from_bytes(point[1 + size:], "big")
    return (x < p and y < p and (y * y - x * x * x + 3 * x - b) % p == 0
            and x.bit_length() > bits // 2 and y.bit_length() > bits // 2)


def _blob_problem(kind, blob):
    """Why the blob is not a public key of this type, or None; ("weak", bits) for an RSA key
    that is too short."""
    parts = _strings(blob)
    if not parts:
        return "its base64 is not a key"
    if parts[0] != kind.encode():
        return f"its base64 holds a key of another type ({clean(parts[0].decode('ascii', 'replace'), False)[:40]})"
    if kind == "ssh-rsa":
        e, n = (_mpint(parts[1]), _mpint(parts[2])) if len(parts) == 3 else (None, None)
        if not e or not n or e.bit_length() > MAX_RSA_BITS:
            return "its base64 is not an RSA key with its numbers in their shortest form"
        if n.bit_length() > MAX_RSA_BITS:
            return f"RSA keys have at most {MAX_RSA_BITS} bits, this one has {n.bit_length()}"
        return ("weak", n.bit_length()) if n.bit_length() < MIN_RSA_BITS else None
    if kind in ("ssh-ed25519", "sk-ssh-ed25519@openssh.com"):
        wanted = 2 if kind == "ssh-ed25519" else 3
        return None if len(parts) == wanted and len(parts[1]) == 32 else "its base64 is not an Ed25519 key"
    curve = kind.split("@")[0].rsplit("-", 1)[1]           # ecdsa-sha2-<curve>, sk-ecdsa-sha2-<curve>@...
    wanted = 3 if kind.startswith("ecdsa") else 4
    if (len(parts) != wanted or parts[1] != curve.encode() or len(parts[2]) != EC_POINT[curve]
            or parts[2][:1] != b"\x04"):
        return "its base64 is not an ECDSA key of its curve"
    if not _on_curve(curve, parts[2]):
        return "its ECDSA point is not on its curve"
    return None


def parse(text, name=None):
    """The one SSH public key in text (a line of a .pub file or authorized_keys, blank lines
    and # comments around it allowed) as {"type", "key": "<type> <base64>", "name",
    "fingerprint"}. name: the key's name, else its comment. Dies with ssh_key_invalid,
    ssh_key_type_refused, ssh_key_too_weak or ssh_key_options_refused. Its messages never
    repeat the text."""
    if not isinstance(text, str):
        _invalid("no text")
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        _invalid("no key given")
    if len(lines) > 1:
        _invalid("more than one line; give one key at a time")
    line = lines[0].strip()
    if len(line.encode("utf-8", "replace")) > MAX_INPUT:
        _invalid(f"longer than {MAX_INPUT} bytes")
    if any(c != "\t" and unicodedata.category(c) == "Cc" for c in line):
        _invalid("it has control characters")
    words = line.split()
    kind = words[0]
    if not TYPE_LIKE.fullmatch(kind):
        if any(TYPE_LIKE.fullmatch(w) for w in words[1:]):
            die("SSH keys with options (command=, from=, restrict, ...) are refused: the VM alone would "
                "enforce them, so they restrict nothing", error="ssh_key_options_refused")
        _invalid("it does not start with a key type")
    if kind not in TYPES:
        die(f"SSH keys of type {clean(kind, False)[:60]} are refused (allowed: Ed25519, ECDSA, their "
            f"security-key forms, RSA of at least {MIN_RSA_BITS} bits)", error="ssh_key_type_refused",
            type=clean(kind, False)[:60])
    if len(words) < 2:
        _invalid("the base64 after the type is missing")
    try:
        blob = base64.b64decode(words[1], validate=True)
    except (binascii.Error, ValueError):
        _invalid("the part after the type is not base64")
    why = _blob_problem(kind, blob)
    if isinstance(why, tuple):
        die(f"RSA keys need at least {MIN_RSA_BITS} bits, this one has {why[1]}", error="ssh_key_too_weak",
            bits=why[1], min_bits=MIN_RSA_BITS)
    if why:
        _invalid(why)
    # the comment as the name: only its printable characters, so it passes check_name()
    comment = "".join(c for c in " ".join(words[2:]) if c.isprintable())[:MAX_NAME].strip()
    return {"type": kind, "key": f"{kind} {words[1]}", "fingerprint": fingerprint(blob),
            "name": check_name(name) if name is not None else comment}


def check_name(name):
    """A key's name as given (--name): printable text of at most MAX_NAME characters."""
    if not isinstance(name, str) or len(name) > MAX_NAME or not name.isprintable() or name != name.strip():
        die(f"a key's name is up to {MAX_NAME} printable characters, without spaces at either end",
            error="invalid_argument", option="--name")
    return name


def key_type(key):
    """The type of a stored key ("<type> <base64>")."""
    return key.split(" ", 1)[0]


def _key_bytes(keys):
    """What the keys' lines take in the fw_cfg entry (fw_cfg())."""
    return sum(len(f"key {k['key']}\n".encode()) for k in keys)


def add(keys, new):
    """keys with new appended; dies when there are MAX_KEYS already (ssh_keys_too_many), the
    lines of all of them would no longer fit into the fw_cfg entry (ssh_keys_too_large) or one
    of them has its fingerprint (ssh_key_duplicate)."""
    if any(k["fingerprint"] == new["fingerprint"] for k in keys):
        die(f"this SSH key ({new['fingerprint']}) is entered for this VM already", error="ssh_key_duplicate",
            fingerprint=new["fingerprint"])
    if len(keys) >= MAX_KEYS:
        die(f"at most {MAX_KEYS} SSH keys per VM", error="ssh_keys_too_many", max_keys=MAX_KEYS)
    size = _key_bytes([*keys, new])
    if size > MAX_KEY_BYTES:
        die(f"the SSH keys of a VM take at most {MAX_KEY_BYTES} bytes together in the file the VM reads; with "
            f"this one they would take {size}", error="ssh_keys_too_large", max_bytes=MAX_KEY_BYTES)
    return [*keys, new]


def problem(keys):
    """What is wrong with the SSH keys of a record, or None (registry.problem)."""
    if not isinstance(keys, list):
        return "not a list"
    if len(keys) > MAX_KEYS:
        return f"more than {MAX_KEYS} keys"
    seen = set()
    for i, k in enumerate(keys):
        if not isinstance(k, dict) or set(k) != set(FIELDS) or not all(isinstance(k[f], str) for f in FIELDS):
            return f"key {i + 1} must have exactly the text fields {', '.join(FIELDS)}"
        try:
            parsed = parse(k["key"], k["name"])
        except Fail as f:
            return f"key {i + 1}: {f.message}"
        if parsed["key"] != k["key"]:
            return f"key {i + 1} must be just its type and base64"
        if parsed["fingerprint"] != k["fingerprint"]:
            return f"key {i + 1} has the wrong fingerprint"
        if k["fingerprint"] in seen:
            return f"key {i + 1} is there twice"
        seen.add(k["fingerprint"])
    if _key_bytes(keys) > MAX_KEY_BYTES:
        return f"the keys take more than {MAX_KEY_BYTES} bytes together"
    return None


# --- the host key ---------------------------------------------------------------------------

def host_key_file(seed, public, comment):
    """An Ed25519 private key (seed and public key, 32 bytes each) in OpenSSH's own format,
    unencrypted, as ssh-keygen writes it."""
    check = os.urandom(4)
    private = (check + check + _ssh(HOST_KEY_TYPE.encode()) + _ssh(public) + _ssh(seed + public)
               + _ssh(comment.encode()))
    pad = 1
    while len(private) % 8:
        private += bytes([pad])
        pad += 1
    blob = _ssh(HOST_KEY_TYPE.encode()) + _ssh(public)
    data = (OPENSSH_MAGIC + _ssh(b"none") + _ssh(b"none") + _ssh(b"") + struct.pack(">I", 1)
            + _ssh(blob) + _ssh(private))
    text = base64.b64encode(data).decode()
    return (HOST_KEY_BEGIN + "\n" + "".join(text[i:i + 70] + "\n" for i in range(0, len(text), 70))
            + HOST_KEY_END + "\n").encode()


def host_public(data):
    """The public key blob of a host key file (host_key_file()); None when it is not one."""
    try:
        text = data.decode("ascii").strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    if not (text.startswith(HOST_KEY_BEGIN) and text.endswith(HOST_KEY_END)):
        return None
    try:
        raw = base64.b64decode("".join(text[len(HOST_KEY_BEGIN):-len(HOST_KEY_END)].split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw.startswith(OPENSSH_MAGIC):
        return None
    pos = len(OPENSSH_MAGIC)
    for _ in range(3):                                  # cipher, kdf, kdf options
        got = _string(raw, pos)
        if got is None:
            return None
        pos = got[1]
    if raw[pos:pos + 4] != struct.pack(">I", 1):
        return None
    got = _string(raw, pos + 4)
    if got is None or _blob_problem(HOST_KEY_TYPE, got[0]):
        return None
    return got[0]


def public_line(blob):
    """A key blob as "<type> <base64>"."""
    return f"{_strings(blob)[0].decode()} {base64.b64encode(blob).decode()}"


def fw_cfg(access, keys, host_key):
    """The content of the fw_cfg entry opt/agentbox/ssh: "none" without SSH access (QEMU refuses
    empty fw_cfg files), else the host key and every key."""
    if not access:
        return b"none\n"
    return ("host_key " + base64.b64encode(host_key).decode() + "\n"
            + "".join(f"key {k['key']}\n" for k in keys)).encode()
