# The VM is the unit of separation; there are no shared "contexts"

Originally a "context" (work, private) was a trust zone with exactly one VM. Once any number of VMs became possible, we chose to make each VM self-contained (own Claude token, GitHub owners, allowlist entries, network) instead of grouping VMs into contexts that inherit settings. Settings can be copied from another VM at creation time, but are independent afterwards, so a change never silently affects several VMs at once.
