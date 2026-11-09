// The Host's readings: the current sample from the `host` event (every 5 s) and 30 minutes of
// history, fetched once from GET /host and extended by each sample. Shared by the host bar
// and the System page.
import { computed, ref } from 'vue'
import type { HostHistoryPoint, HostSample, HostSnapshot } from '~/utils/host'
import type { Live } from '~/utils/live'

const snapshot = ref<HostSnapshot | null>(null)
const history = ref<HostHistoryPoint[]>([])
const unavailable = ref(false)
let loading: Promise<void> | null = null
let subscribedTo: Live | null = null

async function load() {
  try {
    const data = await api<HostSnapshot>('/host')
    snapshot.value = data
    history.value = Array.isArray(data.history) ? data.history : []
    unavailable.value = false
  }
  catch (e) {
    // 503 readings_unavailable: this Host has no sampler; other errors: the next load tries again
    unavailable.value = e instanceof ApiError && e.code === 'readings_unavailable'
  }
}

export function useHost() {
  const live = useLive()
  if (subscribedTo !== live) {
    subscribedTo = live
    live.on('host', (sample: HostSample) => {
      history.value = appendSample(history.value, sample, snapshot.value?.history_seconds ?? 1800)
    })
    // after a reconnect the history may have a gap: load it again (when the first load was done)
    live.on('hello', (data: { resumed?: boolean }) => {
      if (data?.resumed === false && snapshot.value) loading = load()
    })
  }
  if (!snapshot.value && !loading) {
    loading = load().finally(() => { loading = null })
  }
  /** the newest sample: the live one, else the fetched one */
  const sample = computed<HostSample | null>(() => {
    const liveSample = live.state.host as HostSample | null
    if (liveSample && Array.isArray(liveSample.readings)) return liveSample
    return snapshot.value
  })
  const readings = computed(() => sortReadings(sample.value?.readings ?? []))
  return {
    sample,
    readings,
    history,
    unavailable,
    windowSeconds: computed(() => snapshot.value?.history_seconds ?? 1800),
    reload: load,
  }
}

/** Tests only. */
export function resetHostForTests() {
  snapshot.value = null
  history.value = []
  unavailable.value = false
  loading = null
  subscribedTo = null
}
