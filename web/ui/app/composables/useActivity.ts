// The Activity timeline of one page: the resource `activity` for the filter, kept current by
// `activity` events (the resources module writes each into the timelines whose filter it passes).
// A filter change reads again (search after a short pause) and keeps the list before on screen
// meanwhile; "more" reads a longer list.
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import type { ActivityFilter } from '~/utils/activity'

export function useActivity(options: { searchDelayMs?: number } = {}) {
  const filter = reactive<ActivityFilter>({ kinds: [], vm: '', search: '' })
  // the filter read: the search only after a short pause
  const search = ref('')
  const lines = ref(ACTIVITY_PAGE)
  const timeline = useResource('activity', {
    params: () => ({ kinds: [...filter.kinds], vm: filter.vm, search: search.value, lines: lines.value }),
    keepPrevious: true,
  })
  let timer: ReturnType<typeof setTimeout> | null = null

  /** Older entries: the same query with a longer list. */
  function showMore() {
    lines.value = Math.min(ACTIVITY_MAX_LINES, lines.value * 2)
  }

  // another kind or VM starts at the first length, in the same step (no read at the old one)
  watch(() => [filter.kinds.join(','), filter.vm], () => {
    lines.value = ACTIVITY_PAGE
  }, { flush: 'sync' })
  watch(() => filter.search, () => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      lines.value = ACTIVITY_PAGE
      search.value = filter.search
    }, options.searchDelayMs ?? 300)
  })

  onScopeDispose(() => {
    if (timer) clearTimeout(timer)
  })

  const data = timeline.data
  return {
    filter,
    timeline,
    entries: computed(() => (Array.isArray(data.value?.entries) ? data.value.entries : [])),
    truncated: computed(() => !!data.value?.truncated),
    retentionDays: computed(() => (typeof data.value?.retention_days === 'number' ? data.value.retention_days : null)),
    /** a read for this filter is on the way (the list shown may be that of the filter before) */
    loading: computed(() => timeline.loading.value || timeline.previous.value),
    showMore,
    lines,
  }
}
