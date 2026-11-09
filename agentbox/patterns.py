"""Regex entries (ADR 0013): an entry of the Allowlist, the Watchlist, the Legitimate domains
or the Harmless marks whose value is a regular expression instead of a domain.

Matching. re.fullmatch on the lowercased host name without its trailing dot; hosts longer
than a host name or with other characters than printable ASCII never match. A domain entry
keeps covering its subdomains; a pattern matches exactly what it matches.

Rules. Whether a pattern may be an entry is decided by the classifier
(proxy/agentbox-suspicious, pattern_problem), which the proxy loads as well: agentbox
loads the same file (paths.SUSPICIOUS) so that both judge a pattern alike, and a pattern
agentbox accepts is never one the proxy leaves out. At most 200 printable ASCII characters
without spaces; it compiles; no uppercase letters, backreferences or lookarounds; no
quantifier or alternative inside a repeated group; at most two quantifiers in a row that
can take the same characters. The breadth check: it matches none of a fixed sample of
unrelated hosts; the Watchlist does without it (a broad pattern there only raises false
Alerts).

Stored. A record entry has the field regex in place of domain; a Harmless mark is the line
"regex <pattern>". A stored pattern the rules refuse (a hand edit, or a later Release with
stricter rules) counts as nothing: on the Allowlist it allows nothing, as a Harmless mark it
silences nothing, as a Legitimate domain it lifts nothing, and on the Watchlist Open web stays
closed until it is removed. It is listed with the reason (refused) and removed by its pattern;
the other entries keep working. Commands take the pattern with --regex PATTERN where they take a domain,
and remove or change an entry by the same value.
"""
import types

from . import paths
from .errors import die
from .sanitize import clean

_rules = None


def rules():
    """The classifier as a module, for its pattern rules (loaded once, like the proxy loads
    it; no bytecode is written next to it)."""
    global _rules
    if _rules is None:
        path = paths.SUSPICIOUS
        module = types.ModuleType("agentbox_suspicious")
        module.__file__ = path
        try:
            with open(path, encoding="utf-8") as f:
                code = compile(f.read(), path, "exec")
            exec(code, module.__dict__)
        except FileNotFoundError:
            die(f"{path} is missing", 127, error="action_failed")
        except (OSError, SyntaxError, ValueError) as e:
            die(f"{path} cannot be loaded ({type(e).__name__})", error="action_failed")
        _rules = module
    return _rules


def problem(pattern, broad=True):
    """Why pattern cannot be an entry, as (reason, English text, sample host or None), else
    None (the classifier's pattern_problem)."""
    return rules().pattern_problem(pattern, broad)


def checked(pattern, broad=True):
    """The pattern of a new entry, or the command ends: invalid_pattern with the reason
    (too-long, characters, syntax, uppercase, backreference, lookaround, nested-quantifier,
    too-complex), or pattern_too_broad with the host of the sample it matches. broad: with
    the breadth check (every list but the Watchlist)."""
    why = problem(pattern, broad)
    shown = clean(str(pattern), False)[:60]
    if why and why[0] == "too-broad":
        die(f"--regex {shown}: {why[1]}", error="pattern_too_broad", host=why[2])
    if why:
        die(f"--regex {shown}: {why[1]}", error="invalid_pattern", reason=why[0])
    return pattern


def refused(e, broad=True):
    """Why the rules refuse a stored Regex entry (a hand edit, or a later Release with stricter
    rules or a larger breadth sample), else None (the classifier's refused_entry, the same text
    the proxy goes by). Such an entry counts as nothing, but stays listed with this reason and
    can be removed by its pattern; the other entries keep working."""
    return rules().refused_entry(e, broad) if "regex" in e else None


def compiled(pattern):
    return rules().compile_pattern(pattern)


def matches(compiled_pattern, host):
    """Whether a compiled pattern matches host (lowercased, without its trailing dot)."""
    return rules().pattern_matches(compiled_pattern, host)


def key(e):
    """What tells an entry apart within its scope: ("regex", pattern) or ("domain", domain)."""
    return ("regex", e["regex"]) if "regex" in e else ("domain", e["domain"])


def value(e):
    return e["regex"] if "regex" in e else e["domain"]


def shown(e):
    """An entry's value for people: the domain, or "pattern <regex>"."""
    return f"pattern {e['regex']}" if "regex" in e else e["domain"]


def both(e):
    """An entry as the --json output shows it: domain and regex both present, the other null."""
    return {"domain": e.get("domain"), "regex": e.get("regex"), **{k: v for k, v in e.items()
                                                                   if k not in ("domain", "regex")}}
