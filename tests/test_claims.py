"""The claims table (docs/claims.md, #10): every promise it quotes still stands, word for word,
under its heading, and every test it names as defending it still exists.

check() reads such a table. ClaimsTableTest runs it on docs/claims.md; FixtureTest on small
tables and documents in a temporary directory, so that a removed sentence, a renamed heading
and a missing test each fail with the row they belong to.

Run from the repo root: python3 -m unittest discover -s tests
"""
import os
import re
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
TABLE = os.path.join(REPO, "docs", "claims.md")
sys.path.insert(0, TESTS)         # the named tests load as unittest discover -s tests loads them

HEADER = ["Claim", "Section", "Defended by"]
SEPARATOR = re.compile(r"\|?(\s*:?-{3,}:?\s*\|){2}\s*:?-{3,}:?\s*\|?")
# [heading](file#anchor), the file relative to the table
LINK = re.compile(r"\[([^\]]+)\]\(([^)#\s]+)#([^)\s]+)\)")
TEST_NAME = re.compile(r"(test\w*)\.([A-Za-z_]\w*)\.(test\w*)")
HEADING = re.compile(r"(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
FENCE = re.compile(r"\s*(```|~~~)")


def words(text):
    """The text with line wraps and runs of spaces as single spaces."""
    return " ".join(text.split())


def anchor(heading):
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces as hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def sections(path):
    """Each heading of a Markdown file as (text, anchor, the words up to the next heading of
    its level or above), headings in code blocks left out."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    headings, fence = [], None
    for i, line in enumerate(lines):
        if m := FENCE.match(line):
            fence = None if fence == m.group(1) else fence or m.group(1)
        elif fence is None and (h := HEADING.fullmatch(line)):
            headings.append((i, len(h.group(1)), h.group(2)))
    seen, out = {}, []
    for n, (i, level, text) in enumerate(headings):
        end = next((j for j, other, _ in headings[n + 1:] if other <= level), len(lines))
        slug = anchor(text)
        out.append((text, slug if slug not in seen else f"{slug}-{seen[slug]}", words("\n".join(lines[i + 1:end]))))
        seen[slug] = seen.get(slug, 0) + 1
    return out


def cells(line):
    """The cells of a table row; a pipe inside a cell is written \\|."""
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", inner)]


def rows(lines):
    """(row number, line number, cells) of each row of the table with the columns HEADER;
    None without such a table."""
    for start, line in enumerate(lines):
        if line.lstrip().startswith("|") and cells(line) == HEADER:
            break
    else:
        return None
    if start + 1 >= len(lines) or not SEPARATOR.fullmatch(lines[start + 1].strip()):
        return None
    found = []
    for number, line in enumerate(lines[start + 2:], start + 3):
        if not line.lstrip().startswith("|"):
            break
        found.append((len(found) + 1, number, cells(line)))
    return found


def check_claim(claim, section, base, docs):
    """What is wrong with a claim and the section it names (docs: the files read so far)."""
    if len(claim) < 3 or not (claim.startswith('"') and claim.endswith('"')):
        return ["the claim is not in double quotes"]
    link = LINK.fullmatch(section)
    if not link:
        return [f"the section {section!r} is not a link [heading](file#anchor)"]
    heading, target, slug = link.groups()
    path = os.path.normpath(os.path.join(base, target))
    if not os.path.isfile(path):
        return [f"{target} does not exist"]
    if path not in docs:
        docs[path] = sections(path)
    named = [s for s in docs[path] if s[0] == heading]
    if not named:
        return [f'no heading "{heading}" in {target}']
    under = [text for _, s, text in named if s == slug]
    if not under:
        return [f'the link to "{heading}" ends in #{slug}, its anchor is #{named[0][1]}']
    if words(claim[1:-1]) not in under[0]:
        return [f'the claim is not under "{heading}" in {target}']
    return []


def check_tests(defended, loader):
    """What is wrong with the tests a row names: each in backticks, separated by <br> or commas."""
    names = re.findall(r"`([^`]*)`", defended)
    rest = re.sub(r"`[^`]*`|<br\s*/?>|,", " ", defended).strip()
    problems = [f"unknown token {rest!r} in Defended by"] if rest else []
    if not names and not rest:
        problems.append("no test named")
    for name in names:
        parts = TEST_NAME.fullmatch(name)
        if not parts:
            problems.append(f"unknown token `{name}`: not test_module.TestClass.test_name")
            continue
        if not os.path.isfile(os.path.join(TESTS, parts.group(1) + ".py")):
            problems.append(f"no test {name}: tests/{parts.group(1)}.py does not exist")
            continue
        try:
            found = loader.loadTestsFromName(name)
        except Exception as e:          # an attribute test_... that is no method
            found, why = [], e
        else:
            found = list(found) if isinstance(found, unittest.TestSuite) else [found]
            # a name that does not load comes back as a _FailedTest holding the loader's error
            why = getattr(found[0], "_exception", None) if found else None
        if not (len(found) == 1 and isinstance(found[0], unittest.TestCase) and found[0].id() == name):
            why = str(why).strip().splitlines()[-1] if why and str(why).strip() else ""
            problems.append(f"no test {name}" + (f" ({why})" if why else ""))
    return problems


def check(table):
    """What is wrong with the claims table in this file, one message per problem."""
    with open(table, encoding="utf-8") as f:
        found = rows(f.read().splitlines())
    if found is None:
        return [f"no table with the columns {' | '.join(HEADER)}"]
    if not found:
        return ["the table has no rows"]
    base, docs, loader, problems = os.path.dirname(table), {}, unittest.TestLoader(), []
    for row, line, row_cells in found:
        where = f"row {row} (line {line})"
        if len(row_cells) != len(HEADER):
            problems.append(f"{where}: {len(row_cells)} cells instead of {len(HEADER)}")
            continue
        claim, section, defended = row_cells
        problems += [f"{where}: {p}" for p in check_claim(claim, section, base, docs)]
        problems += [f"{where}: {p}" for p in check_tests(defended, loader)]
    return problems


class ClaimsTableTest(unittest.TestCase):
    def test_every_claim_stands_under_its_heading_and_every_test_exists(self):
        problems = check(TABLE)
        if problems:
            self.fail("docs/claims.md: a PR that changes a promise or a test that defends it updates "
                      "the table in the same PR\n" + "\n".join(problems))


# --- the check on fixtures -----------------------------------------------------------

DOC = """# Architecture

## Layers

### VMs
- Each VM is   one KVM VM
  with its own kernel, so a kernel exploit does not reach the Host.

```
### Not a heading
```

### Network
- The proxy accepts only `CONNECT` to port 443 and
  refuses addresses that are not public (a | b).

## Setting a Host up
Setup runs a self-test first.
"""
ADR = """# The VM is the unit of separation

## Decision
One VM per unit of separation.
"""
KERNEL = '"Each VM is one KVM VM with its own kernel, so a kernel exploit does not reach the Host."'
PROXY = '"The proxy accepts only `CONNECT` to port 443 and refuses addresses that are not public (a \\| b)."'
VMS = "[VMs](../ARCHITECTURE.md#vms)"
NETWORK = "[Network](../ARCHITECTURE.md#network)"
OK = "`test_claims.FixtureTest.test_a_table_that_holds_passes`"


class FixtureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        os.makedirs(os.path.join(self.dir, "docs", "adr"))
        self.write("ARCHITECTURE.md", DOC)
        self.write("docs/adr/0001-vm.md", ADR)

    def write(self, rel, text):
        with open(os.path.join(self.dir, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def check(self, *table_rows, head="# Claims\n\n"):
        """check() on a table of these rows (claim, section, defended by); the first row is on line 5."""
        self.write("docs/claims.md", head + "| Claim | Section | Defended by |\n|---|---|---|\n"
                   + "".join(f"| {c} | {s} | {d} |\n" for c, s, d in table_rows) + "\nText after the table.\n")
        return check(os.path.join(self.dir, "docs", "claims.md"))

    def test_a_table_that_holds_passes(self):
        # line wraps, runs of spaces and list markers do not count; a pipe in a claim is \|;
        # several tests in a row; an ADR; a part of a sentence, under a heading above its own
        other = "`test_claims.FixtureTest.test_a_changed_word_fails`"
        self.assertEqual(self.check((KERNEL, VMS, f"{OK}<br>{other}"), (PROXY, NETWORK, f"{OK}, {other}"),
                                    ('"One VM per unit of separation."', "[Decision](adr/0001-vm.md#decision)", OK),
                                    ('"kernel exploit"', "[Layers](../ARCHITECTURE.md#layers)", OK)), [])

    def test_a_removed_sentence_fails_with_its_row(self):
        self.write("ARCHITECTURE.md", DOC.replace("so a kernel exploit does not reach the Host.", "so it is safe."))
        self.assertEqual(self.check((PROXY, NETWORK, OK), (KERNEL, VMS, OK)),
                         ['row 2 (line 6): the claim is not under "VMs" in ../ARCHITECTURE.md'])

    def test_a_changed_word_fails(self):
        self.assertEqual(self.check((KERNEL.replace("does not", "cannot"), VMS, OK)),
                         ['row 1 (line 5): the claim is not under "VMs" in ../ARCHITECTURE.md'])

    def test_a_sentence_under_another_heading_fails(self):
        self.assertEqual(self.check((PROXY, VMS, OK), (KERNEL, "[Setting a Host up](../ARCHITECTURE.md#setting-a-host-up)",
                                                       OK)),
                         ['row 1 (line 5): the claim is not under "VMs" in ../ARCHITECTURE.md',
                          'row 2 (line 6): the claim is not under "Setting a Host up" in ../ARCHITECTURE.md'])

    def test_a_renamed_heading_fails(self):
        self.write("ARCHITECTURE.md", DOC.replace("### VMs", "### Virtual machines"))
        self.assertEqual(self.check((PROXY, NETWORK, OK), (KERNEL, VMS, OK)),
                         ['row 2 (line 6): no heading "VMs" in ../ARCHITECTURE.md'])

    def test_a_link_to_another_anchor_or_file_fails(self):
        self.assertEqual(self.check((KERNEL, "[VMs](../ARCHITECTURE.md#vm)", OK),
                                    (KERNEL, "[VMs](../ARCHITEKTUR.md#vms)", OK),
                                    (KERNEL, "VMs", OK),
                                    ('"Not a heading"', "[Not a heading](../ARCHITECTURE.md#not-a-heading)", OK)),
                         ['row 1 (line 5): the link to "VMs" ends in #vm, its anchor is #vms',
                          "row 2 (line 6): ../ARCHITEKTUR.md does not exist",
                          "row 3 (line 7): the section 'VMs' is not a link [heading](file#anchor)",
                          'row 4 (line 8): no heading "Not a heading" in ../ARCHITECTURE.md'])

    def test_a_missing_test_fails(self):
        problems = self.check((KERNEL, VMS, OK + "<br>`test_claims.FixtureTest.test_that_was_deleted`"),
                              (PROXY, NETWORK, "`test_claims.NoSuchTest.test_a_table_that_holds_passes`"),
                              (PROXY, NETWORK, "`test_nothing_here.FixtureTest.test_x`"))
        # after each name the loader's reason, in the words of the Python that runs the tests
        self.assertEqual([p.split(" (", 2)[:2] for p in problems],
                         [["row 1", "line 5): no test test_claims.FixtureTest.test_that_was_deleted"],
                          ["row 2", "line 6): no test test_claims.NoSuchTest.test_a_table_that_holds_passes"],
                          ["row 3", "line 7): no test test_nothing_here.FixtureTest.test_x: "
                                    "tests/test_nothing_here.py does not exist"]])
        self.assertIn("test_that_was_deleted", problems[0].split(" (", 2)[2])
        self.assertIn("NoSuchTest", problems[1].split(" (", 2)[2])

    def test_unknown_tokens_fail(self):
        self.assertEqual(self.check((KERNEL, VMS, "`test_claims.FixtureTest`"),
                                    (KERNEL, VMS, "`test_claims.FixtureTest.check`"),
                                    (KERNEL, VMS, OK + " and the VM tests"),
                                    (KERNEL, VMS, ""),
                                    ("Each VM is one KVM VM", VMS, OK),
                                    (KERNEL, VMS + " | " + OK, OK)),
                         ["row 1 (line 5): unknown token `test_claims.FixtureTest`: not test_module.TestClass.test_name",
                          "row 2 (line 6): unknown token `test_claims.FixtureTest.check`: "
                          "not test_module.TestClass.test_name",
                          "row 3 (line 7): unknown token 'and the VM tests' in Defended by",
                          "row 4 (line 8): no test named",
                          "row 5 (line 9): the claim is not in double quotes",
                          "row 6 (line 10): 4 cells instead of 3"])

    def test_without_the_table_or_its_rows(self):
        self.write("docs/claims.md", "# Claims\n\n| Claim | Section |\n|---|---|\n| a | b |\n")
        self.assertEqual(check(os.path.join(self.dir, "docs", "claims.md")),
                         ["no table with the columns Claim | Section | Defended by"])
        self.assertEqual(self.check(), ["the table has no rows"])


if __name__ == "__main__":
    unittest.main()
