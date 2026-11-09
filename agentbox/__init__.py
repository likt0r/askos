"""agentbox: operate the Agent Box VMs on the Host.

Standard library only (ADR 0006). The CLI runs as root except for `logs` and
`pair`, which only need libvirt access.
"""
