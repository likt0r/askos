// The Host's readings: the resource `host` (GET /host), its current sample and 30 minutes of
// history, which each `host` event (every 5 s) brings up to date.
// Shared by the host bar and the System page.
import { computed } from 'vue'
import type { HostSample } from '~/utils/host'

export function useHost() {
  const host = useResource('host')
  /** the newest sample (the `host` event writes it into the resource) */
  const sample = computed<HostSample | null>(() => host.data.value ?? null)
  const readings = computed(() => sortReadings(sample.value?.readings ?? []))
  return {
    resource: host,
    sample,
    readings,
    history: computed(() => (Array.isArray(host.data.value?.history) ? host.data.value.history : [])),
    /** this Host has no sampler */
    unavailable: computed(() => !!host.data.value?.unavailable),
    windowSeconds: computed(() => host.data.value?.history_seconds ?? 1800),
  }
}
