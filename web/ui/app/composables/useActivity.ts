// The Activity timeline of one page: fetched with the filter, kept current by `activity`
// events. A filter change fetches again (search after a short pause).
import { onScopeDispose, reactive, ref, watch } from 'vue'
import type { ActivityEntry, ActivityFilter, ActivityResult } from '~/utils/activity'

export function useActivity(options: { searchDelayMs?: number } = {}) {
  const live = useLive()
  const filter = reactive<ActivityFilter>({ kinds: [], vm: '', search: '' })
  const entries = ref<ActivityEntry[]>([])
  const truncated = ref(false)
  const retentionDays = ref<number | null>(null)
  const loading = ref(true)
  const error = ref<unknown>(null)
  const lines = ref(ACTIVITY_PAGE)
  let token = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  async function load() {
    const mine = ++token
    loading.value = true
    try {
      const result = await api<ActivityResult>(`/activity?${activityQuery(filter, lines.value)}`)
      if (mine !== token) return
      entries.value = Array.isArray(result.entries) ? result.entries : []
      truncated.value = !!result.truncated
      retentionDays.value = typeof result.retention_days === 'number' ? result.retention_days : null
      error.value = null
    }
    catch (e) {
      if (mine !== token) return
      error.value = e
    }
    finally {
      if (mine === token) loading.value = false
    }
  }

  function reload() {
    lines.value = ACTIVITY_PAGE
    return load()
  }

  /** Older entries: the same query with a longer list. */
  function showMore() {
    lines.value = Math.min(ACTIVITY_MAX_LINES, lines.value * 2)
    return load()
  }

  // a live entry passes the search by agentbox's text or by the words the UI shows for it
  const words = useActivityText()
  const offEntry = live.on('activity', (data: unknown) => {
    if (error.value || !isEntry(data) || !matchesFilter(data, filter, words(data))) return
    entries.value = addEntry(entries.value, data, lines.value)
  })
  // a connection that could not resume missed entries
  let helloSeen = false
  const offHello = live.on('hello', (data: { resumed?: boolean }) => {
    if (helloSeen && data?.resumed === false) void load()
    helloSeen = true
  })
  if (live.state.connection === 'live') helloSeen = true

  watch(() => [filter.kinds.join(','), filter.vm], () => void reload())
  watch(() => filter.search, () => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => void reload(), options.searchDelayMs ?? 300)
  })

  onScopeDispose(() => {
    offEntry()
    offHello()
    if (timer) clearTimeout(timer)
    token++
  })

  void load()
  return { filter, entries, truncated, retentionDays, loading, error, reload, showMore, lines }
}
