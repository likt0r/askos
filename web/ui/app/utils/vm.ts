import type { PausedByAlert, Vm } from './live'

/** Agent counts in a fixed order (working, waiting, done, then the rest). */
export function agentCounts(vm: Vm): { key: string, n: number }[] | null {
  if (vm.agents == null) return null
  const order = ['working', 'waiting', 'done']
  return Object.entries(vm.agents)
    .filter(([, n]) => typeof n === 'number' && n > 0)
    .sort(([a], [b]) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b))
    .map(([key, n]) => ({ key, n }))
}

/** Addresses connected to the Orca port (SSH sessions apart: sshSessionCount). */
export function deviceCount(vm: Vm): number | null {
  if (vm.devices == null) return null
  return vm.devices.filter(d => d.kind !== 'ssh').length
}

/** Addresses with an open SSH session. */
export function sshSessionCount(vm: Vm): number | null {
  if (vm.devices == null) return null
  return vm.devices.filter(d => d.kind === 'ssh').length
}

/** The VM's Container image cleanup; records without the fields mean on and 7 days. */
export function containerImageCleanup(vm: Pick<Vm, 'container_image_cleanup' | 'container_image_cleanup_days'>) {
  return {
    enabled: vm.container_image_cleanup ?? true,
    days: vm.container_image_cleanup_days ?? 7,
  }
}

/** The Alert that paused the VM (#11), while it is paused by it; null otherwise. */
export function pausedByAlert(vm: Vm): PausedByAlert | null {
  const a = vm.paused_by_alert
  if (vm.state !== 'paused' || !a || typeof a.domain !== 'string') return null
  return { time: a.time ?? null, domain: a.domain, signals: Array.isArray(a.signals) ? a.signals : [] }
}

export interface NotResponding {
  /** what does not answer: the system did not boot (not even the guest agent), Orca alone, or not known */
  what: 'system' | 'orca' | 'unknown'
  /** since when Orca does not answer (ISO time), or null */
  since: string | null
  /** connections to the Orca port that gave up since the VM's start, or null when none */
  wakes: { count: number, last: string | null } | null
}

/** What the VM page says about a VM that does not respond (#54); null in any other state. */
export function notResponding(vm: Vm): NotResponding | null {
  if (vm.state !== 'not_responding') return null
  const r = vm.responding
  const what = r?.guest_agent === false ? 'system' : r?.guest_agent === true ? 'orca' : 'unknown'
  const w = vm.failed_wakes
  return {
    what,
    since: r?.since ?? null,
    wakes: w && typeof w.count === 'number' && w.count > 0 ? { count: w.count, last: w.last ?? null } : null,
  }
}

/** A device paired with a VM's Orca (GET /vms/<vm>/pairings). */
export interface Device {
  id: string
  name: string
  scope: 'mobile' | 'runtime' | string
  paired_at: string | null
  last_seen_at: string | null
  observer: boolean
  removable: boolean
}

/** GET /vms/<vm>/pairings. */
export interface Pairings { vm: string, devices: Device[], observer_known: boolean, single_removal: boolean }
