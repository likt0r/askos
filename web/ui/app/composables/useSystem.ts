// The root helper `agentbox watch`, the Host-reboot notice and the Host's services: the resource
// `system`, which the `watch`, `host-reboot` and `services` events keep current (GET /system is its
// fallback).
import { computed } from 'vue'

export function useSystem() {
  const system = useResource('system', { extra: true })
  const data = system.data
  return {
    watch: computed(() => data.value?.watch ?? null),
    hostReboot: computed(() => data.value?.host_reboot ?? null),
    services: computed(() => data.value?.services ?? null),
    warnings: computed(() => data.value?.warnings ?? null),
    /** an update of every Image runs (the `services` event says it); undefined before one */
    updating: computed(() => data.value?.updating),
  }
}
