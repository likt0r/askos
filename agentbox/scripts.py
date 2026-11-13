"""Programs of the Release that agentbox loads as modules, to apply the same rules as they do:
the classifier (agentbox-suspicious, patterns.py) and the choice of Orca's release
(vm/build/orca-release, versions.py). Loading runs only their definitions (each guards its
main), and writes no bytecode next to them."""
import types

from .errors import die


def load(path, name):
    """The program at path as a module named name; the command ends when it is missing or
    cannot be loaded."""
    module = types.ModuleType(name)
    module.__file__ = path
    try:
        with open(path, encoding="utf-8") as f:
            code = compile(f.read(), path, "exec")
        exec(code, module.__dict__)
    except FileNotFoundError:
        die(f"{path} is missing", 127, error="action_failed")
    except (OSError, SyntaxError, ValueError) as e:
        die(f"{path} cannot be loaded ({type(e).__name__})", error="action_failed")
    return module
