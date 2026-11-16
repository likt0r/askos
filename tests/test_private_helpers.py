"""#114: no module of agentbox/ imports or calls an underscore helper of another module.

A helper with a leading underscore belongs to its module: whoever owns it may change it
without looking elsewhere. A helper another module needs gets a public name in the module
that owns it. The check reads the source with ast, so it catches `from .x import _y` as
well as `x._y` after `from . import x`.

Run from the repo root: python3 -m unittest discover -s tests
"""
import ast
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(REPO, "agentbox")


def private(name):
    return name.startswith("_") and not name.startswith("__")


def uses(source, own, modules):
    """(line, text) of every private name of another module of `modules` in `source`,
    the code of module `own`."""
    tree = ast.parse(source)
    alias = {}           # local name -> module of the package
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "agentbox" and len(parts) == 2 and parts[1] in modules:
                    alias[a.asname or a.name] = parts[1]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 1 or module.split(".")[0] == "agentbox":
                source_module = module.split(".")[-1] if module and module != "agentbox" else None
                for a in node.names:
                    if source_module is None and a.name in modules:
                        alias[a.asname or a.name] = a.name
                    elif private(a.name) and source_module != own:
                        found.append((node.lineno, f"from {'.' * node.level}{module} import {a.name}"))
    for node in ast.walk(tree):
        # x._y, or agentbox.x._y after `import agentbox.x`
        if isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Name, ast.Attribute)) and private(node.attr):
            base = ast.unparse(node.value)
            if alias.get(base, own) != own:
                found.append((node.lineno, f"{base}.{node.attr}"))
    return sorted(found)


class PrivateHelperTest(unittest.TestCase):
    def modules(self):
        return {n[:-3] for n in os.listdir(PACKAGE) if n.endswith(".py")}

    def test_no_module_uses_a_private_helper_of_another(self):
        modules = self.modules()
        found = []
        for name in sorted(modules):
            with open(os.path.join(PACKAGE, name + ".py")) as f:
                found += [f"agentbox/{name}.py:{line}: {text}" for line, text in uses(f.read(), name, modules)]
        self.assertEqual(found, [])

    def test_the_check_sees_every_form(self):
        modules = {"images", "generate", "own"}
        source = ("from . import images\nfrom .generate import _mark, public\nimport agentbox.images as im\n"
                  "from agentbox import generate as g\nfrom .own import _fine\n"
                  "images._records()\nim._backing(1)\ng._pending('x')\nimages.public()\nos._exit(0)\n"
                  "images.__name__\nimport agentbox.generate\nagentbox.generate._virsh()\n")
        self.assertEqual(uses(source, "own", modules),
                         [(2, "from .generate import _mark"), (6, "images._records"), (7, "im._backing"),
                          (8, "g._pending"), (13, "agentbox.generate._virsh")])

    def test_a_module_may_use_its_own_private_helpers(self):
        self.assertEqual(uses("from . import own\nown._helper()\n", "own", {"own"}), [])


if __name__ == "__main__":
    unittest.main()
