# Agent Box

Coding agents in isolated VMs with allowlist egress and short-lived credentials.
Start with [README.md](README.md) and [ARCHITECTURE.md](ARCHITECTURE.md), and use the
vocabulary in [GLOSSARY.md](GLOSSARY.md). Decisions with lasting consequences are in
[docs/adr/](docs/adr/).

Tests run without root, libvirt or a VM: `python3 tests/run_parallel.py` from the repo
root runs them in parallel worker processes, by test class (module or class names to run
only those, `--help` for the rest); `python3 -m unittest discover -s tests` is the plain
fallback that runs the same tests one after another. They drive the real CLI
(`bin/agentbox`, package `agentbox/`) with fake external programs on `PATH`
(`tests/fake_virsh.py` and others) and `AGENTBOX_ROOT` pointing at a temporary directory
instead of `/etc/agentbox` and `/var/lib/agentbox`. Test classes run at the same time, so
each test keeps its files and sockets in its own temporary directory, and whatever listens
on TCP binds port 0 itself and reports the port (`tests/served_port.py`).

## Pull requests

Every PR an agent opens starts as a draft and leaves draft (`gh pr ready`) only after the
review loop in [docs/agents/review-loop.md](docs/agents/review-loop.md). Agents never merge.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues of `likt0r/askos`, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
