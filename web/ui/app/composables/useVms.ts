// The VMs and the problems of their records: the resource `vms`, which the `state` and `vm` events
// keep current (GET /vms is its fallback). Not known until the first `state` event or read.
import { computed } from 'vue'
import type { Vm, VmProblem } from '~/utils/live'

export function useVms() {
  const list = useResource('vms', { extra: true })
  return {
    vms: computed<Vm[]>(() => list.data.value?.vms ?? []),
    problems: computed<VmProblem[]>(() => list.data.value?.problems ?? []),
    /** the VM list is known */
    loaded: computed(() => list.data.value !== undefined),
  }
}
