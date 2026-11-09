// Images and Builds of the System page: the overview from GET /images kept current by `build`
// events, rebuilds, Update now (#114), and the log of a Build (live from `build-log` events; the
// log endpoint fills the gaps: after a reconnect that could not resume, or when lines were
// skipped). Whether an update of every Image runs comes from the `services` event (its unit),
// before that from the overview.
import { computed, onScopeDispose, reactive, ref, watch } from 'vue'
import type { BuildEvent, BuildLog, BuildLogEvent, ImagesOverview, LogLine } from '~/utils/images'

export function useImages() {
  const live = useLive()
  const overview = ref<ImagesOverview | null>(null)
  const loading = ref(true)
  const error = ref<unknown>(null)
  const startError = ref<unknown>(null)
  const starting = ref<string | null>(null)
  const logs = reactive<Record<string, BuildLog>>({})
  const logErrors = reactive<Record<string, unknown>>({})
  const logLoading = reactive<Record<string, boolean>>({})
  let token = 0
  // lines of `build-log` events that arrived while the log was being fetched
  const buffered = new Map<string, { lines: LogLine[], truncated: boolean }>()

  async function load() {
    const mine = ++token
    try {
      const data = await api<ImagesOverview>('/images')
      if (mine !== token) return
      overview.value = data
      error.value = null
    }
    catch (e) {
      if (mine === token) error.value = e
    }
    finally {
      if (mine === token) loading.value = false
    }
  }

  /** The log of a Build: all of it the first time, only what is newer after that. Lines that
   * `build-log` events delivered while the request was out come after the fetched ones. */
  async function fetchLog(image: string, build: string) {
    const key = logKey(image, build)
    if (logLoading[key]) {
      // a request is out already: ask again once it is back, for what it may have missed
      buffered.set(key, { lines: buffered.get(key)?.lines ?? [], truncated: true })
      return
    }
    const held = logs[key]
    const params = new URLSearchParams({ build, lines: '500' })
    if (held?.cursor) params.set('after_cursor', held.cursor)
    logLoading[key] = true
    buffered.delete(key)
    let again = false
    try {
      const data = await api<{ entries: LogLine[], cursor: string | null, status: string, step: string | null, error: string | null, problems: string[] }>(
        `/images/${encodeURIComponent(image)}/log?${params}`)
      const extra = buffered.get(key)
      // lines after a gap (`truncated`) are not kept: the request after this one fetches all
      const lines = appendLines(appendLines(held?.lines ?? [], data.entries ?? []), extra && !extra.truncated ? extra.lines : [])
      logs[key] = {
        lines,
        cursor: lines.at(-1)?.cursor ?? data.cursor ?? held?.cursor ?? null,
        status: data.status, step: data.step, error: data.error, problems: data.problems,
      }
      again = !!extra?.truncated
      delete logErrors[key]
    }
    catch (e) {
      logErrors[key] = e
    }
    finally {
      logLoading[key] = false
      buffered.delete(key)
    }
    if (again) await fetchLog(image, build)
  }

  // the update's unit by the last `services` event; null before one
  const ACTIVE = ['active', 'activating', 'deactivating', 'reloading']
  const updateUnit = computed<boolean | null>(() => {
    const units = (live.state.services as { units?: { unit: string, active: string }[] } | null)?.units
    return Array.isArray(units) ? units.some(u => u.unit === UPDATE_UNIT && ACTIVE.includes(u.active)) : null
  })
  const updating = computed(() => updateUnit.value ?? overview.value?.updating ?? false)
  // an update that ended may have built nothing, so no `build` event tells
  watch(updateUnit, (now, before) => {
    if (before && !now) void load()
  })

  /** Update now: the version check, a Build of every Image with an update, then a Reset of
   * these VMs onto the newest Build of their Image. Whether it started. */
  async function startUpdate(reset: string[] = []) {
    starting.value = 'update'
    startError.value = null
    try {
      await api('/updates', { method: 'POST', body: reset.length ? { reset } : {} })
      await load()
      return true
    }
    catch (e) {
      startError.value = e
      if (e instanceof ApiError && (e.code === 'update_running' || e.code === 'build_running')) await load()
      return false
    }
    finally {
      starting.value = null
    }
  }

  /** Start a Build of an Image (or "all"); with `reset` an Update: when the Build succeeds,
   * its unit resets these VMs onto it. Whether it started. */
  async function start(target: string, reset: string[] = []) {
    starting.value = target
    startError.value = null
    try {
      await api('/builds', { method: 'POST', body: reset.length ? { image: target, reset } : { image: target } })
      await load()
      return true
    }
    catch (e) {
      startError.value = e
      // another Build is running: show it, so that the buttons say so
      if (e instanceof ApiError && e.code === 'build_running') await load()
      return false
    }
    finally {
      starting.value = null
    }
  }

  const offBuild = live.on('build', (event: BuildEvent) => {
    if (!overview.value || !event?.image || !event.build) return
    startError.value = null     // what it said (a Build running already) is out of date
    const next = applyBuild(overview.value, event)
    if (next) overview.value = next
    // what a finished Build changes (the current Build, who uses what, older Builds removed)
    if (!next || event.status !== 'running') void load()
    if (event.status !== 'running') {
      const key = logKey(event.image, event.build)
      if (logs[key]) void fetchLog(event.image, event.build)
    }
  })

  const offLog = live.on('build-log', (event: BuildLogEvent) => {
    if (!event?.image || !event.build || !Array.isArray(event.entries)) return
    const key = logKey(event.image, event.build)
    const held = logs[key]
    if (logLoading[key]) {
      // the log is being fetched: keep these lines for when it is back
      const extra = buffered.get(key) ?? { lines: [], truncated: false }
      buffered.set(key, { lines: [...extra.lines, ...event.entries], truncated: extra.truncated || !!event.truncated })
      return
    }
    if (!held) return          // nobody looks at this log: it is fetched when opened
    if (event.truncated) {
      void fetchLog(event.image, event.build)
      return
    }
    logs[key] = { ...held, lines: appendLines(held.lines, event.entries), cursor: event.cursor ?? held.cursor }
  })

  // a connection that could not resume missed events
  let helloSeen = live.state.connection === 'live'
  const offHello = live.on('hello', (data: { resumed?: boolean }) => {
    if (helloSeen && data?.resumed === false) {
      void load()
      for (const key of Object.keys(logs)) {
        const [image, build] = key.split('/')
        if (image && build) void fetchLog(image, build)
      }
    }
    helloSeen = true
  })

  onScopeDispose(() => {
    offBuild()
    offLog()
    offHello()
    token++
  })

  void load()
  return { overview, loading, error, startError, starting, updating, logs, logErrors, logLoading, load, start, startUpdate, fetchLog }
}
