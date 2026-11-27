// The Host's readings: the current sample from the `host` event (every 5 s) and 30 minutes of
// history, the resource `host` (GET /host), to which the resources module appends each sample.
// Shared by the host bar and the System page.
import { computed } from 'vue'
import type { HostSample } from '~/utils/host'

export function useHost() {
  const live = useLive()
  const host = useResource('host')
  /** the newest sample: the live one, else the one read */
  const sample = computed<HostSample | null>(() => {
    const liveSample = live.state.host as HostSample | null
    if (liveSample && Array.isArray(liveSample.readings)) return liveSample
    return host.data.value ?? null
  })
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
