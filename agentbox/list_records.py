"""The list records (GLOSSARY: List record): the Allowlist, the Watchlist, the Legitimate
domains, the Harmless marks and Open web as files on the Host, and the rules for their
entries, in one place for agentbox, the proxy (agentbox-proxy) and the classifier
(agentbox-suspicious), which import this module (ADR 0013).

A record is the file {"entries": [...]}; an entry names a domain or, as a Regex entry, a
pattern ("regex") in place of the domain, with fields of its own besides. A broken or
duplicate entry makes the whole record broken, with the same text for every reader. The
text lists from before the records hold a domain per line and a comment after #.

Leaf module: it imports only the standard library, so the proxy programs, which run as their
own users, load nothing else of agentbox with it (tests/test_list_records.py)."""
import json
import re
import time
import warnings

# the files, in the configuration directory (/etc/agentbox, paths.CONF)
ALLOWLIST = "allowlist.json"
LEGACY_ALLOWLIST = ("allowlist", "allowlist-{}")     # before allowlist.json: every VM, one VM
WATCHLIST = "watchlist.json"
LEGITIMATE = "legitimate.json"
LEGACY_WATCHLIST = "suspicious-domains"
LEGACY_LEGITIMATE = "legitimate-domains"
HARMLESS = "harmless"
OPEN_WEB = "open-{}"

# a VM's name, and the words that stand, or may come to stand, where one may stand (the scope
# "all" of Allowlist entries, `agentbox focus off`, the Control Room's areas and paths, its API
# versions v1, v2, ...), so no VM can have them (registry.py)
VM_NAME = re.compile(r"[a-z][a-z0-9-]{1,15}")
RESERVED_NAMES = frozenset({"off", "all", "new", "status", "login", "logout", "activity", "allowlist", "watchlist",
                            "images", "settings", "system", "host", "events", "version", "vms", "github-app"})
API_VERSION = re.compile(r"v[0-9]+")
ALL = "all"             # the scope of an Allowlist entry for every VM
# lowercase labels of letters, digits and inner hyphens, at least two of them
DOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
# the Watchlist's categories, each also the Signal of a hit; payload is a weak one
CATEGORIES = ("exfil", "c2", "tunnel", "paste", "phishing", "blockchain", "payload")
# the fields of an entry besides domain or regex; an entry that differs breaks its record
WATCH_FIELDS = {"category": str, "source": str, "note": str, "added": int, "added_by": str}
LEGITIMATE_FIELDS = {"note": str, "added": int, "added_by": str}
ALLOWLIST_FIELDS = {"scope": str, "expires": (int, type(None)), "note": str, "added": int, "added_by": str}
MAX_TEXT = 200          # note and source


# --- Regex entries ---------------------------------------------------------------

# the parser of the re module, for the rules below (re._parser since Python 3.11)
try:
    from re import _constants as _sre, _parser as _sre_parse
except ImportError:                                     # pragma: no cover
    import sre_constants as _sre
    import sre_parse as _sre_parse

MAX_PATTERN = 200
# what a pattern may consist of: printable ASCII without spaces (host names have neither more
# nor spaces, and the proxy log names a pattern as one word)
PATTERN_TEXT = re.compile(r"[!-~]{1,%d}" % MAX_PATTERN)
# the hosts a pattern is tried on: the same characters, at most as long as a host name can be;
# this bounds what any pattern can cost per connection
PATTERN_HOST = re.compile(r"[!-~]{1,253}")
MAX_HOST = 253
# at most this many ways for a pattern to match one host (_ways): two quantifiers in a row
# that can take the same characters, such as .*foo.*, and no more
MAX_WAYS = (MAX_HOST + 1) ** 2
# The breadth check: a Regex entry (except on the Watchlist, where a broad pattern only raises
# false Alerts) must match none of these unrelated hosts, common public domains and names
# made up at random. A fixed list, so that a pattern is judged the same on every Host.
BREADTH_SAMPLE = (
    "google.com", "www.google.com", "mail.google.com", "youtube.com", "facebook.com", "instagram.com",
    "wikipedia.org", "en.wikipedia.org", "amazon.com", "amazon.de", "apple.com", "microsoft.com",
    "bing.com", "yahoo.com", "x.com", "twitter.com", "linkedin.com", "reddit.com", "netflix.com",
    "cloudflare.com", "akamai.net", "fastly.net", "bbc.co.uk", "www.bbc.co.uk", "gov.uk", "nasa.gov",
    "mit.edu", "archive.org", "mozilla.org", "debian.org", "ubuntu.com", "wordpress.com",
    "medium.com", "stackoverflow.com", "gitlab.com", "bitbucket.org", "dropbox.com", "zoom.us",
    "paypal.com", "ebay.com", "booking.com", "nytimes.com", "spiegel.de", "heise.de", "lemonde.fr",
    "baidu.com", "qq.com", "yandex.ru", "vk.com", "naver.com", "rakuten.co.jp", "example.com",
    "example.net", "example.org", "k3j9x2.net", "zq-ab.io", "a.co", "b.ai", "x1.y2.z3.de",
    "7h4t.dev", "qwpzlm.app", "mxv-9kq.xyz", "lmn0p.top", "123.net", "a-b-c.info", "r4nd0m.site",
    "pqz.online", "vxw.sh", "tt.me", "foo-bar-baz.cloud", "z.tech", "h7.cn", "n0de.ru", "o9.fr",
    "qqq.jp", "aaa.biz", "bbb.us", "ccc.ca", "ddd.eu", "eee.nl", "fff.it", "ggg.es", "hhh.pl",
    "iii.ch", "jjj.se", "kkk.no", "nnn.at", "ppp.br", "rrr.in", "sss.com.au", "ttt.tv", "uuu.cc",
    "vvv.io", "www.qx7-vendor.com", "ab.cd.ef.gh.ij.example.org", "xn--80ak6aa92e.com",
    "s0me-very-long-label-made-up-for-the-breadth-check-of-patterns.net", "192.0.2.1", "1.1.1.1",
    # long and many-label names, as data sent out in DNS-shaped hosts looks: a pattern that
    # only asks for length or for many labels (^[a-z0-9.-]{80,}$) is no narrow one
    "q" * 63 + ".net", ("7f3a9c0e1b5d4f2a8c6e0b9d7f1a3c5e" * 2)[:63] + ".io",
    ".".join(["a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b"] * 3 + ["x9-y8-z7"]) + ".com",
    ".".join(["b" * 63] * 3 + ["c" * 57]) + ".net", ".".join(["mx"] * 83) + ".com",
    "a.b.c.d.e.f.g.h.i.j.k.l.example.net", "1.2.3.4.5.6.7.8.org",
    "data-0.data-1.data-2.data-3.data-4.data-5.data-6.cdn.example.com",
)
# characters a host can have when a pattern is tried on it (PATTERN_HOST, lowercased)
_HOST_CHARS = frozenset(chr(c) for c in range(0x21, 0x7f)) - frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DIGITS = frozenset("0123456789")
_WORD = _DIGITS | frozenset("abcdefghijklmnopqrstuvwxyz_")
_CATEGORIES = {_sre.CATEGORY_DIGIT: _DIGITS, _sre.CATEGORY_NOT_DIGIT: _HOST_CHARS - _DIGITS,
               _sre.CATEGORY_SPACE: frozenset(), _sre.CATEGORY_NOT_SPACE: _HOST_CHARS,
               _sre.CATEGORY_WORD: _WORD, _sre.CATEGORY_NOT_WORD: _HOST_CHARS - _WORD,
               _sre.CATEGORY_LINEBREAK: frozenset(), _sre.CATEGORY_NOT_LINEBREAK: _HOST_CHARS}
_REPEATS = {_sre.MAX_REPEAT, _sre.MIN_REPEAT, getattr(_sre, "POSSESSIVE_REPEAT", _sre.MAX_REPEAT)}
_END = "<end>"          # in a FIRST set: the end of the host may come next
# why a pattern is refused: the stable reason (agentbox's invalid_pattern names it) and English
PATTERN_REASONS = {
    "too-long": f"longer than {MAX_PATTERN} characters",
    "characters": "only printable ASCII characters without spaces are allowed",
    "syntax": "not a regular expression",
    "uppercase": "an uppercase letter, but host names are compared in lowercase",
    "backreference": r"a backreference (\1, (?P=name), (?(1)...)), which may backtrack for very long",
    "lookaround": "a lookahead or lookbehind ((?=...), (?!...), (?<=...), (?<!...))",
    # refused also where a delimiter would keep it fast (^(?:[a-z]+\.){1,5}example\.org$): the
    # rule stays simple; a domain entry or a pattern without the repeated group does it instead
    "nested-quantifier": "a quantifier or an alternative inside a repeated group, such as (a+)+ or (a|ab)+, "
                         "which may backtrack for very long (a domain entry covers subdomains, or write it "
                         "without the repeated group, such as ^[a-z0-9.-]+\\.example\\.org$)",
    "too-complex": "more than two quantifiers or alternatives in a row that can take the same characters, "
                   "such as .*a.*b.*c: matching would take too long",
}


def pattern_problem(pattern, broad=True):
    """Why pattern cannot be a Regex entry, as (reason, English text, the sample host it
    matches or None), else None. broad: also the breadth check (BREADTH_SAMPLE), which the
    Watchlist does without. The rules: at most MAX_PATTERN printable ASCII characters
    without spaces; a regular expression of the re module without uppercase letters (it is
    matched against lowercased host names), backreferences or lookarounds; no quantifier or
    alternative inside a repeated group; at most MAX_WAYS ways to match a host, so that no
    host makes it backtrack for long."""
    if not isinstance(pattern, str) or not pattern:
        return "characters", PATTERN_REASONS["characters"], None
    if len(pattern) > MAX_PATTERN:
        return "too-long", PATTERN_REASONS["too-long"], None
    if not PATTERN_TEXT.fullmatch(pattern):
        return "characters", PATTERN_REASONS["characters"], None
    try:
        # a warning (such as a possible nested set, [[) refuses it too, and never reaches a log
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            tree = _sre_parse.parse(pattern)
            compiled = re.compile(pattern)
    except (re.error, OverflowError, RecursionError, ValueError, Warning) as e:
        why = getattr(e, "msg", None) or str(e) or type(e).__name__
        return "syntax", f"{PATTERN_REASONS['syntax']} ({why[:100]})", None
    reason = _rule(list(tree), False, tree.state.flags)
    if reason is None and _ways(list(tree), {_END}) > MAX_WAYS:
        reason = "too-complex"
    if reason:
        return reason, PATTERN_REASONS[reason], None
    if broad:
        host = next((h for h in BREADTH_SAMPLE if compiled.fullmatch(h)), None)
        if host:
            return "too-broad", f"too broad: it matches {host}, one of the unrelated hosts it must not match", host
    return None


def compile_pattern(pattern, broad=True):
    """The compiled pattern of a Regex entry, or None when it breaks a rule (pattern_problem)."""
    if pattern_problem(pattern, broad):
        return None
    return re.compile(pattern)


def pattern_matches(compiled, host):
    """Whether a Regex entry's compiled pattern matches host (already lowercased, without its
    trailing dot); hosts longer than a host name or with other characters never match."""
    return bool(PATTERN_HOST.fullmatch(host) and compiled.fullmatch(host))


def _rule(items, repeated, flags):
    """The first rule the parsed items break, as a reason of PATTERN_REASONS, else None.
    repeated: inside a group that repeats (a quantifier with a maximum above 1)."""
    for op, av in items:
        if op in (_sre.GROUPREF, _sre.GROUPREF_EXISTS):
            return "backreference"
        if op in (_sre.ASSERT, _sre.ASSERT_NOT):
            return "lookaround"
        if not flags & _sre.SRE_FLAG_IGNORECASE and _uppercase(op, av):
            return "uppercase"
        if op is _sre.SUBPATTERN:
            _, add, remove, body = av
            reason = _rule(list(body), repeated, (flags | add) & ~remove)
        elif op is _sre.ATOMIC_GROUP:
            reason = _rule(list(av), repeated, flags)
        elif op is _sre.BRANCH:
            reason = "nested-quantifier" if repeated else next(
                (r for r in (_rule(list(alt), repeated, flags) for alt in av[1]) if r), None)
        elif op in _REPEATS:
            low, high, body = av
            reason = "nested-quantifier" if repeated and low != high else _rule(list(body), repeated or high > 1, flags)
        else:
            reason = None
        if reason:
            return reason
    return None


def _uppercase(op, av):
    if op is _sre.LITERAL:
        return 0x41 <= av <= 0x5a
    if op is _sre.IN:
        return any(o is _sre.LITERAL and 0x41 <= a <= 0x5a or o is _sre.RANGE and 0x41 <= a[0] <= a[1] <= 0x5a
                   for o, a in av)
    return False


def _chars(op, av):
    """The host characters a one-character item can match (rather more than fewer), or None
    when it is no such item."""
    if op is _sre.LITERAL:
        return frozenset({chr(av).lower()}) & _HOST_CHARS
    if op is _sre.NOT_LITERAL:
        return _HOST_CHARS - {chr(av)}
    if op is _sre.ANY:
        return _HOST_CHARS
    if op is not _sre.IN:
        return None
    out, negate = set(), False
    for o, a in av:
        if o is _sre.NEGATE:
            negate = True
        elif o is _sre.LITERAL:
            out.add(chr(a))
        elif o is _sre.RANGE:
            out |= {chr(c) for c in range(a[0], min(a[1], 0x7e) + 1)}
        elif o is _sre.CATEGORY:
            out |= _CATEGORIES.get(a, _HOST_CHARS)
        else:
            out |= _HOST_CHARS
    # negated: exactly what is left out; else with the lowercase letters, as ignorecase may match them
    return _HOST_CHARS - out if negate else frozenset({c.lower() for c in out}) & _HOST_CHARS


def _taken(items):
    """Every host character the items can consume."""
    out = set()
    for op, av in items:
        chars = _chars(op, av)
        if chars is not None:
            out |= chars
        elif op is _sre.SUBPATTERN:
            out |= _taken(av[3])
        elif op is _sre.ATOMIC_GROUP:
            out |= _taken(av)
        elif op is _sre.BRANCH:
            for alt in av[1]:
                out |= _taken(alt)
        elif op in _REPEATS:
            out |= _taken(av[2])
        elif op is not _sre.AT:
            out |= _HOST_CHARS
    return out


def _first(items, follow):
    """What the first character of the items, followed by follow, can be (with _END: the end
    of the host): their FIRST set."""
    for op, av in reversed(list(items)):
        follow = _first_item(op, av, follow)
    return follow


def _first_item(op, av, follow):
    chars = _chars(op, av)
    if chars is not None:
        return set(chars)
    if op is _sre.AT:
        return {_END} if av in (_sre.AT_END, _sre.AT_END_STRING) else follow
    if op is _sre.SUBPATTERN:
        return _first(av[3], follow)
    if op is _sre.ATOMIC_GROUP:
        return _first(av, follow)
    if op is _sre.BRANCH:
        return set().union(*(_first(alt, follow) for alt in av[1]))
    if op in _REPEATS:
        low, high, body = av
        if high == 0:
            return follow
        first = _first(body, follow)
        return first | follow if low == 0 else first
    return set(_HOST_CHARS) | {_END}


def _ways(items, follow):
    """At most how many ways the items, followed by follow (a FIRST set), can match one
    stretch of a host: what a backtracking matcher may have to try. A quantifier counts
    with the number of lengths it can take, unless what follows it cannot start with a
    character it takes (then only one length can go on); an alternative counts with the
    sum of its branches unless they start differently. Capped above MAX_WAYS."""
    total = 1
    for op, av in reversed(list(items)):
        total = min(total * _ways_item(op, av, follow), MAX_WAYS + 1)
        follow = _first_item(op, av, follow)
    return total


def _ways_item(op, av, follow):
    if op is _sre.SUBPATTERN:
        return _ways(av[3], follow)
    if op is _sre.ATOMIC_GROUP:
        return _ways(av, follow)
    if op is _sre.BRANCH:
        alts = av[1]
        firsts = [_first(alt, follow) for alt in alts]
        ways = [_ways(alt, follow) for alt in alts]
        apart = all(a.isdisjoint(b) for i, a in enumerate(firsts) for b in firsts[i + 1:])
        return max(ways) if apart else sum(ways)
    if op in _REPEATS:
        low, high, body = av
        inner = _ways(body, follow)
        if high <= 1:
            return inner + 1 if low == 0 else inner
        lengths = min(high, MAX_HOST) - min(low, MAX_HOST) + 1
        if lengths <= 1 or _taken(body).isdisjoint(follow):
            return inner
        return lengths * inner
    return 1


def refused_entry(e, broad=True):
    """Why the rules refuse a stored Regex entry's pattern (a hand edit, or a later Release
    with stricter rules or a larger BREADTH_SAMPLE), as English text, else None. Such an
    entry counts as nothing, in the proxy as in agentbox, which lists it with this text and
    removes it by its pattern: on the Allowlist it allows nothing, as a Harmless mark it
    silences nothing, as a Legitimate domain it lifts nothing, and on the Watchlist, where it
    would raise a Signal, Open web stays closed until it is gone."""
    if not isinstance(e, dict) or not isinstance(e.get("regex"), str):
        return None
    why = pattern_problem(e["regex"], broad)
    return f"regex entry {printable(e['regex'], 60)} refused: {why[1]}" if why else None


# --- records -----------------------------------------------------------------------

def printable(text, limit):
    """text for a message: unprintable characters as ?, at most limit characters."""
    return "".join(c if c.isprintable() else "?" for c in text)[:limit]


def key(e):
    """What tells an entry apart within its list: ("regex", pattern) or ("domain", domain); a
    domain and a Regex entry of the same text are two entries."""
    return ("regex", e["regex"]) if "regex" in e else ("domain", e["domain"])


def entries(text):
    """The list entries of a record's text; without it the record is broken (ValueError)."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("no list entries")
    return data["entries"]


def problem(e, fields, shown=printable, extra=None):
    """What is wrong with a stored entry, else None: no JSON object, a field missing or of the
    wrong type (domain or regex, then fields: {name: type}), both domain and regex, an
    invalid domain (shown(text, limit) shows it), then extra(e), the list's own checks. A
    pattern the rules refuse is no broken entry: it counts as nothing (refused_entry)."""
    if not isinstance(e, dict):
        return "not a JSON object"
    name = "regex" if "regex" in e else "domain"
    for k, kind in ((name, str), *fields.items()):
        if k not in e or isinstance(e[k], bool) or not isinstance(e[k], kind):
            return f"field {k} missing or of the wrong type"
    if name == "regex":
        if "domain" in e:
            return "both domain and regex"
    elif not DOMAIN.fullmatch(e["domain"]):
        return f"invalid domain {shown(e['domain'], 60)}"
    return extra(e) if extra else None


def _watch_or_legitimate(e):
    if "category" in e and e["category"] not in CATEGORIES:
        return f"unknown category {printable(e['category'], 20)}"
    if len(e["note"]) > MAX_TEXT or len(e.get("source", "")) > MAX_TEXT:
        return "text too long"
    return None


def watch_problem(e):
    """What is wrong with an entry of the Watchlist, else None."""
    return problem(e, WATCH_FIELDS, extra=_watch_or_legitimate)


def legitimate_problem(e):
    """What is wrong with an entry of the Legitimate domains, else None."""
    return problem(e, LEGITIMATE_FIELDS, extra=_watch_or_legitimate)


def reserved_name(name):
    """Whether name is a word no VM can have (RESERVED_NAMES, an API version)."""
    return name in RESERVED_NAMES or bool(API_VERSION.fullmatch(name))


def allowlist_problem(e, shown=printable):
    """What is wrong with an entry of the Allowlist, else None: the common checks, a scope that
    is neither all nor a VM's name, a note too long."""
    def scope_or_note(e):
        if e["scope"] != ALL and (not VM_NAME.fullmatch(e["scope"]) or reserved_name(e["scope"])):
            return f"invalid scope {shown(e['scope'], 20)}"
        if len(e["note"]) > MAX_TEXT:
            return "note too long"
        return None
    return problem(e, ALLOWLIST_FIELDS, shown, scope_or_note)


def allowlist_key(e):
    """What tells an Allowlist entry apart: its domain or pattern, and its scope."""
    return key(e), e["scope"]


def allowlist_twice(e, shown=printable):
    return f"{shown(key(e)[1], MAX_HOST)} ({e['scope']}) twice"


def allowlist_entries(text):
    """The entries of allowlist.json's text; a broken or second entry makes the whole record
    broken (ValueError), for the proxy as for agentbox."""
    return checked(entries(text), allowlist_problem, allowlist_key, allowlist_twice)


def checked(items, check, key=key, twice=lambda e: f"{printable(key(e)[1], MAX_HOST)} listed twice"):
    """The entries; at the first broken one (check(e) says why) or one whose key(e) came before
    (twice(e) says so) the record is broken: ValueError "broken entry (...)" or the text of
    twice, by default the value shown with printable, at most as long as a host name (a hand
    edit may hold control characters)."""
    seen = set()
    for e in items:
        why = check(e)
        why = f"broken entry ({why})" if why else twice(e) if key(e) in seen else None
        if why:
            raise ValueError(why)
        seen.add(key(e))
    return items


# --- the text lists from before the records --------------------------------------------

def legacy_lines(text):
    """The lines of a text list as (what comes before #, the comment)."""
    return [(body, comment) for body, _, comment in (line.partition("#") for line in text.splitlines())]


def legacy_domain(word):
    """A domain as a text list has it: lowercased, without its trailing dot."""
    return word.lower().rstrip(".")


def legacy_domains(text):
    """The domains of a text list: the first word of each line, comments after #."""
    return [legacy_domain(body.split()[0]) for body, _ in legacy_lines(text) if body.split()]


def legacy_allowlist(text):
    """The domains of an Allowlist text list: each line before #, stripped, as a domain; empty
    lines left out (a line that is no valid domain is the caller's to skip, DOMAIN)."""
    return [d for d in (legacy_domain(body.strip()) for body, _ in legacy_lines(text)) if d]


def param(ref):
    """An entry asked for, ("domain", d) or ("regex", p), as {"domain": d} or {"regex": p}."""
    return {ref[0]: ref[1]}


def harmless_marks(text):
    """The Harmless marks as {"domain": d} or {"regex": p}: a domain per line (the first word,
    comments after #), or "regex <pattern>" for a Regex entry."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "regex":
            out.append({"regex": parts[1]})
            continue
        parts = line.split("#", 1)[0].split()
        if parts:
            out.append({"domain": legacy_domain(parts[0])})
    return out


# --- Open web --------------------------------------------------------------------

def open_until(path):
    """End of a VM's Open web (epoch seconds) from its file at path, or 0 when the web is not
    open: whole seconds, a fraction cut off, anything else (inf, 1e20, nan) counts as closed."""
    try:
        with open(path) as f:
            u = f.read().strip().split(".")[0]
    except OSError:
        return 0
    return int(u) if re.fullmatch(r"\d+", u) and int(u) > time.time() else 0
