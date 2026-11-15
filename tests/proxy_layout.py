"""The proxy programs laid out as installed, for tests that copy and change one of them:
<prefix>/libexec/ for the programs, <prefix>/lib/agentbox/agentbox the package they import
(the proxy and the classifier find it there, list_records)."""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def libexec(prefix):
    """<prefix>/libexec, created, with the checkout's package where the programs look for it."""
    os.makedirs(os.path.join(prefix, "lib", "agentbox"))
    os.symlink(os.path.join(REPO, "agentbox"), os.path.join(prefix, "lib", "agentbox", "agentbox"))
    os.makedirs(os.path.join(prefix, "libexec"))
    return os.path.join(prefix, "libexec")
