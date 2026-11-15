"""The classifier (agentbox-suspicious, paths.SUSPICIOUS) as agentbox asks it: the only module
that starts it or reads its answers. It owns the arguments, the environment
(paths.classifier_env: the lists of this Host or of the test root), the time limit
(paths.CLASSIFIER_SECONDS), the exit codes and the shape of each answer, checked here once;
an answer of the wrong shape ends the command with INVALID. Each question runs the program,
except is_address, which loads it as a module (scripts.py). Tests fake it at one seam: the
program at <root>/usr/libexec/agentbox-suspicious."""
import json
import os
import subprocess
import sys

from . import paths, programs, scripts
from .errors import die
from .sanitize import clean

INVALID = "agentbox-suspicious: invalid answer"
SELF_TEST_SECONDS = 300


def _ask(valid, *args):
    """The JSON answer to args, when valid(answer) says it has its shape."""
    r = programs.run([paths.SUSPICIOUS, *args], paths.CLASSIFIER_SECONDS, what="agentbox-suspicious",
                     env=paths.classifier_env())
    try:
        answer = json.loads(r.stdout)
    except ValueError:
        answer = None
    if not valid(answer):
        die(INVALID, error="action_failed")
    return answer


def _strings(value):
    return isinstance(value, list) and all(isinstance(s, str) for s in value)


def _signal(s):
    """A Signal of the classifier's verdict: its name, whether it is strong or suppressed, the
    reason's English text, code and parameters (text)."""
    return (isinstance(s, dict) and isinstance(s.get("signal"), str) and isinstance(s.get("reason"), str)
            and isinstance(s.get("code"), str) and isinstance(s.get("params"), dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in s["params"].items())
            and isinstance(s.get("strong"), bool) and isinstance(s.get("suppressed"), bool))


def report(domain):
    """The verdict on a domain (--json): {"signals": [Signal...], "feeds": {name: state}, ...}."""
    return _ask(lambda a: isinstance(a, dict) and isinstance(a.get("signals"), list)
                and isinstance(a.get("feeds"), dict) and all(_signal(s) for s in a["signals"]), "--json", domain)


def signals(hosts):
    """The Signals of hosts (--signals) as ({host: {"signals": [...], "strong": bool}}, why the
    Watchlist cannot be read or None)."""
    a = _ask(lambda a: isinstance(a, dict) and isinstance(a.get("hosts"), dict)
             and all(isinstance(a["hosts"].get(h), dict) and isinstance(a["hosts"][h].get("signals"), list)
                     and isinstance(a["hosts"][h].get("strong"), bool) for h in hosts)
             and isinstance(a.get("watchlist_unreadable"), (str, type(None))), "--signals", *hosts)
    return a["hosts"], a["watchlist_unreadable"]


def lifts(domain):
    """What making the domain legitimate would lift (--lifts): {"signals": [strong Signals],
    "feed_entries": [threat-intelligence entries], "feed_present": bool}."""
    return _ask(lambda a: isinstance(a, dict) and _strings(a.get("signals")) and _strings(a.get("feed_entries"))
                and isinstance(a.get("feed_present"), bool), "--lifts", domain)


_constants = None


def constants():
    """The categories ({category: strong}) and built-in Legitimate domains (--constants, asked
    once): {"categories": {...}, "genuine": [...]}."""
    global _constants
    if _constants is None:
        _constants = _ask(lambda a: isinstance(a, dict) and isinstance(a.get("categories"), dict)
                          and all(isinstance(k, str) and isinstance(v, bool) for k, v in a["categories"].items())
                          and _strings(a.get("genuine")), "--constants")
    return _constants


def feeds():
    """The state of the Feeds (--watchlist): {name: {"present": bool, "entries", "updated",
    "error"}}."""
    return _ask(lambda a: isinstance(a, dict) and isinstance(a.get("feeds"), dict), "--watchlist")["feeds"]


def classify(domain):
    """The Signals of a destination as (text, suspicious, why the hand-maintained Watchlist
    cannot be read or None). The classifier prints the Signals and exits non-zero on strong
    suspicion, 3 with the reason on stderr while the Watchlist cannot be read; without a
    classifier, or when it does not answer in time, every domain counts as suspicious."""
    try:
        r = subprocess.run([paths.SUSPICIOUS, domain], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=paths.classifier_env(), timeout=paths.CLASSIFIER_SECONDS)
    except OSError:
        return "", True, None
    except subprocess.TimeoutExpired:
        return "agentbox-suspicious does not answer", True, None
    unreadable = clean(r.stderr.decode("utf-8", "replace").strip(), False)[-300:] if r.returncode == 3 else None
    return r.stdout.decode("utf-8", "replace").rstrip("\n"), r.returncode != 0, unreadable


def self_test(conf):
    """The detection self-test (--self-test) with the lists in the folder conf: None when it
    passed, else why it failed (its FAILED lines, else its stderr or exit code)."""
    try:
        r = subprocess.run([sys.executable, "-I", "-B", paths.SUSPICIOUS, "--self-test"], capture_output=True,
                           text=True, env=dict(os.environ, AGENTBOX_CONF_DIR=conf), timeout=SELF_TEST_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        die(f"{paths.SUSPICIOUS}: {e}", error="action_failed")
    if r.returncode == 0:
        return None
    said = "; ".join(line.split(None, 1)[-1] for line in r.stdout.splitlines() if line.startswith("FAILED"))
    return said or r.stderr.strip()[-300:] or f"exit {r.returncode}"


_module = None


def is_address(host):
    """Whether a resolver reads host as an IP address, as the classifier does (raw-ip); the
    classifier is loaded as a module once, without bytecode next to it."""
    global _module
    if _module is None:
        _module = scripts.load(paths.SUSPICIOUS, "agentbox_suspicious")
    return _module.is_address(host)
