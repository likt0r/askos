import type { Vm } from './live'

/** Agent counts in a fixed order (working, waiting, done, then the rest). */
export function agentCounts(vm: Vm): { key: string, n: number }[] | null {
  if (vm.agents == null) return null
  const order = ['working', 'waiting', 'done']
  return Object.entries(vm.agents)
    .filter(([, n]) => typeof n === 'number' && n > 0)
    .sort(([a], [b]) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b))
    .map(([key, n]) => ({ key, n }))
}

export function deviceCount(vm: Vm): number | null {
  if (vm.devices == null) return null
  return vm.devices.length
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
