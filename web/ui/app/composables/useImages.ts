// Images and Builds of the System page: the resource `images` (utils/resources.ts), rebuilds,
// Update now (#114), and the log of a Build (live from `build-log` events; the log endpoint fills
// the gaps: after a reconnect that could not resume, or when lines were skipped). Whether an
// update of every Image runs comes from the `services` event (`updating`), before that from the
// overview.
import { computed, onScopeDispose, reactive, ref } from 'vue'
import type { BuildEvent, BuildLog, BuildLogEvent, LogLine } from '~/utils/images'

export function useImages() {
  const live = useLive()
  const resource = useResource('images')
  const overview = resource.data
  const startError = ref<unknown>(null)
  const starting = ref<string | null>(null)
  const logs = reactive<Record<string, BuildLog>>({})
  const logErrors = reactive<Record<string, unknown>>({})
  const logLoading = reactive<Record<string, boolean>>({})
  // lines of `build-log` events that arrived while the log was being fetched
  const buffered = new Map<string, { lines: LogLine[], truncated: boolean }>()

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

  // whether the update runs by the last `services` event (the backend decides, also while its
  // unit still stops); null before one
  const updateUnit = computed<boolean | null>(() => {
    const updating = (live.state.services as { updating?: unknown } | null)?.updating
    return typeof updating === 'boolean' ? updating : null
  })
  const updating = computed(() => updateUnit.value ?? overview.value?.updating ?? false)

  /** Update now: the version check, a Build of every Image with an update, then a Reset of
   * these VMs onto the newest Build of their Image. Whether it started. */
  async function startUpdate(reset: string[] = []) {
    starting.value = 'update'
    startError.value = null
    try {
      await change('/updates', { method: 'POST', body: reset.length ? { reset } : {} }, ['images'])
      return true
    }
    catch (e) {
      startError.value = e
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
      // refused because another Build runs, the overview read again shows it, and the buttons say so
      await change('/builds', { method: 'POST', body: reset.length ? { image: target, reset } : { image: target } }, ['images'])
      return true
    }
    catch (e) {
      startError.value = e
      return false
    }
    finally {
      starting.value = null
    }
  }

  // the overview follows `build` events in the resources module; here the start error and the log
  const offBuild = live.on('build', (event: BuildEvent) => {
    if (!event?.image || !event.build) return
    startError.value = null     // what it said (a Build running already) is out of date
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

  // a connection that could not resume missed lines of the open logs
  const offMissed = onMissedEvents(() => {
    for (const key of Object.keys(logs)) {
      const [image, build] = key.split('/')
      if (image && build) void fetchLog(image, build)
    }
  })

  onScopeDispose(() => {
    offBuild()
    offLog()
    offMissed()
  })

  return { resource, startError, starting, updating, logs, logErrors, logLoading, start, startUpdate, fetchLog }
}
