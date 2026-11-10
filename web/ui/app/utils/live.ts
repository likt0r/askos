// The live connection to GET /api/v1/events (Server-Sent Events, docs/control-room-api.md
// "Live events") and the state it carries. One instance per page (useLive()).
//
// EventSource retries on its own only after a dropped connection. After an HTTP error
// (401 when the session ended, 503 too_many_streams, a backend that is restarting) it
// gives up (readyState CLOSED). Then this asks GET /session: signed out goes to the
// sign-in page, otherwise it opens a new EventSource after a growing delay. A stream that
// stops delivering events (the backend sends `host` every 5 s; its `: ping` comments are
// invisible to EventSource) is treated as dead after `deadAfterMs`.

import { reactive } from 'vue'

export type Connection = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'signed-out'

export interface Vm {
  name: string
  state: string
  libvirt_state?: string | null
  network?: number
  orca_port?: number
  address?: string
  memory_mib?: number
  data_disk_gib?: number
  github_owners?: string[]
  /** the VM variables of the record (names and values, not secret); the VM has them from its next start */
  env?: Record<string, string>
  image?: string
  build_pin?: string | null
  image_build?: string | null
  next_build?: string | null
  system_disk?: { since: string | null, age_days: number | null, file: string | null, image: string | null, build: string | null } | null
  reset_changes_build?: boolean
  reset_due?: boolean
  agents?: Record<string, number> | null
  /** what answers while libvirt reports the VM as running; null otherwise or not asked yet */
  responding?: { guest_agent: boolean | null, orca: boolean, since: string | null } | null
  /** wakes that gave up since the VM's last start */
  failed_wakes?: { count: number, last: string | null, address: string | null } | null
  open_web_until?: string | null
  devices?: { address: string, connections: number }[] | null
  [key: string]: unknown
}

export interface VmProblem { vm: string, problem: string }
export interface VmList { vms: Vm[], problems: VmProblem[] }
export interface WatchState { running: boolean, since: string | null, restarts: number, last_exit: number | null }
export interface VersionState { version: string, ui: string, installed: string | null }

export interface LiveState {
  connection: Connection
  /** false until the first `state` event or fetch: the VM list is not known yet */
  loaded: boolean
  vms: Vm[]
  problems: VmProblem[]
  watch: WatchState | null
  version: VersionState | null
  host: Record<string, unknown> | null
  hostReboot: Record<string, unknown> | null
  services: Record<string, unknown> | null
  /** the stream id from `hello`; changes when the backend restarts */
  stream: string | null
  /** when the next attempt to connect is due (ms since epoch), while reconnecting */
  retryAt: number | null
  /** failed attempts in a row */
  attempts: number
}

/** Events that carry no state of their own: views subscribe with `on()`. */
export const STREAM_EVENTS = ['activity', 'alert', 'github-token', 'build', 'build-log', 'notification'] as const
export type Listener = (data: any) => void

interface EventSourceLike {
  readonly readyState: number
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
}

export interface LiveOptions {
  url?: string
  /** true while the session is valid; throws when the backend cannot be asked */
  checkSession: () => Promise<boolean>
  /** GET /vms, used after `agentbox watch` runs again */
  fetchVms: () => Promise<VmList>
  /** the session ended */
  onSignedOut?: () => void
  createEventSource?: (url: string) => EventSourceLike
  /** delays before the 1st, 2nd, ... new connection after the stream gave up */
  backoffMs?: number[]
  deadAfterMs?: number
  now?: () => number
}

const CLOSED = 2

export function sortVms(vms: Vm[]): Vm[] {
  return [...vms].sort((a, b) => (a.network ?? Infinity) - (b.network ?? Infinity) || a.name.localeCompare(b.name))
}

export function createLive(options: LiveOptions) {
  const url = options.url ?? '/api/v1/events'
  const backoff = options.backoffMs ?? [1000, 3000, 5000, 10000, 20000, 30000]
  const deadAfter = options.deadAfterMs ?? 30000
  const now = options.now ?? (() => Date.now())
  const create = options.createEventSource ?? ((u: string) => new EventSource(u))

  const state = reactive<LiveState>({
    connection: 'idle', loaded: false, vms: [], problems: [], watch: null, version: null,
    host: null, hostReboot: null, services: null, stream: null, retryAt: null, attempts: 0,
  })

  let source: EventSourceLike | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let watchdog: ReturnType<typeof setInterval> | null = null
  let lastEventAt = 0
  let periodic = false      // seen a `host` event: the stream has a pulse to watch
  let generation = 0        // events of a replaced EventSource are ignored
  const listeners = new Map<string, Set<Listener>>()

  function setVms(list: Partial<VmList>) {
    state.vms = sortVms(Array.isArray(list.vms) ? list.vms : [])
    state.problems = Array.isArray(list.problems) ? list.problems : []
    state.loaded = true
  }

  function replaceVm(vm: Vm) {
    if (!vm || typeof vm.name !== 'string') return
    const i = state.vms.findIndex(v => v.name === vm.name)
    if (i >= 0) state.vms.splice(i, 1, vm)
    else state.vms = sortVms([...state.vms, vm])
  }

  async function refetchVms() {
    try {
      setVms(await options.fetchVms())
    }
    catch {
      // the next `state`/`vm` event or reconnect brings the list
    }
  }

  const handlers: Record<string, (data: any) => void> = {
    hello(data) {
      state.connection = 'live'
      state.attempts = 0
      state.retryAt = null
      state.stream = typeof data?.stream === 'string' ? data.stream : null
    },
    state(data) {
      setVms(data ?? {})
    },
    vm(data) {
      replaceVm(data)
    },
    watch(data) {
      const wasStopped = state.watch?.running === false
      state.watch = data
      // VM state may have changed unseen while the root helper was not running
      if (wasStopped && data?.running === true) void refetchVms()
    },
    version(data) {
      state.version = data
    },
    host(data) {
      periodic = true
      state.host = data
    },
    'host-reboot'(data) {
      state.hostReboot = data
    },
    services(data) {
      state.services = data
    },
  }

  /** Subscribe to an event by name (state events too, after the state was updated);
   * returns the function that unsubscribes. Whole objects, never patches: a repeat is harmless. */
  function on(name: string, listener: Listener): () => void {
    const set = listeners.get(name) ?? new Set<Listener>()
    set.add(listener)
    listeners.set(name, set)
    return () => { set.delete(listener) }
  }

  function dispatch(name: string, data: unknown) {
    for (const listener of [...(listeners.get(name) ?? [])]) {
      try {
        listener(data)
      }
      catch {
        // one subscriber's bug must not stop the others or the stream
      }
    }
  }

  function listen(es: EventSourceLike, gen: number) {
    const names = new Set<string>([...Object.keys(handlers), ...STREAM_EVENTS])
    for (const name of names) {
      const handle = handlers[name]
      es.addEventListener(name, (event: MessageEvent) => {
        if (gen !== generation) return
        lastEventAt = now()
        let data: unknown
        try {
          data = JSON.parse(event.data)
        }
        catch {
          return
        }
        handle?.(data)
        dispatch(name, data)
      })
    }
    es.addEventListener('error', () => {
      if (gen !== generation) return
      if (es.readyState === CLOSED) void gaveUp()
      else if (state.connection === 'live' || state.connection === 'connecting') state.connection = 'reconnecting'
    })
  }

  function open() {
    clearRetry()
    generation += 1
    lastEventAt = now()
    periodic = false
    state.connection = state.attempts === 0 && state.connection !== 'reconnecting' ? 'connecting' : 'reconnecting'
    const es = create(url)
    source = es
    listen(es, generation)
  }

  function closeSource() {
    generation += 1
    source?.close()
    source = null
  }

  function clearRetry() {
    if (retryTimer) clearTimeout(retryTimer)
    retryTimer = null
    state.retryAt = null
  }

  /** The EventSource stopped for good (HTTP error) or the stream went silent. */
  async function gaveUp() {
    closeSource()
    state.connection = 'reconnecting'
    const gen = generation
    let signedIn = true
    try {
      signedIn = await options.checkSession()
    }
    catch {
      // backend unreachable: keep trying
    }
    if (gen !== generation || state.connection !== 'reconnecting') return   // stopped or reconnected meanwhile
    if (!signedIn) {
      stop('signed-out')
      options.onSignedOut?.()
      return
    }
    const delay = backoff[Math.min(state.attempts, backoff.length - 1)] ?? 30000
    state.attempts += 1
    state.retryAt = now() + delay
    retryTimer = setTimeout(open, delay)
  }

  function checkPulse() {
    if (source && periodic && state.connection === 'live' && now() - lastEventAt > deadAfter) void gaveUp()
  }

  function start() {
    if (source || retryTimer) return
    state.attempts = 0
    open()
    if (!watchdog) watchdog = setInterval(checkPulse, Math.min(5000, deadAfter / 2))
  }

  /** Try again at once (a "retry now" button, the browser coming back online). */
  function reconnectNow() {
    if (state.connection !== 'reconnecting') return
    closeSource()
    open()
  }

  function stop(connection: Connection = 'idle') {
    closeSource()
    clearRetry()
    if (watchdog) clearInterval(watchdog)
    watchdog = null
    state.connection = connection
  }

  /** Forget everything (sign-out). */
  function reset() {
    stop()
    Object.assign(state, {
      loaded: false, vms: [], problems: [], watch: null, version: null, host: null,
      hostReboot: null, services: null, stream: null, attempts: 0,
    })
  }

  return { state, start, stop, reset, reconnectNow, refetchVms, on }
}

export type Live = ReturnType<typeof createLive>
