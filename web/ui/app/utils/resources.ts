// The resources module (ADR 0026): every resource of the Control Room API the UI reads, declared
// once, in one cache (TanStack Query), and the table of what a live event does to it. A view asks
// for a resource by name (`useResource('images')`) and gets its data, whether it is loading and
// its failure; it knows neither the path nor when the data is read again.
//
// Data is read again only for a reason the stream or the owner gives: a row of the table below, a
// change made in this browser (`change`), a `hello` with `resumed: false` that is not the first,
// or "Retry". Never by the clock, never on window focus, never as an automatic retry; a read that
// failed because the Control Room could not be reached is read again with the next `hello`.
import { notifyManager, QueryClient, useQuery } from '@tanstack/vue-query'
import { computed, type ComputedRef, type MaybeRefOrGetter, type Ref, toValue } from 'vue'
import { api, ApiError } from './api'
import { applyBuild, type BuildEvent, type ImagesOverview } from './images'
import type { Live } from './live'

// query-core hands results to the views through setTimeout(0) by default; a microtask gives them
// in the same turn, which the tests wait for with flushPromises. Production takes the same path.
notifyManager.setScheduler(queueMicrotask)

// ---- the resources ---------------------------------------------------------------------------

interface Declaration<T> {
  path: string
  /** only carries the type of the answer */
  answer?: T
}

function declare<T>(path: string): Declaration<T> {
  return { path }
}

const RESOURCES = {
  images: declare<ImagesOverview>('/images'),
}

export type ResourceName = keyof typeof RESOURCES
export type ResourceData<N extends ResourceName> = NonNullable<(typeof RESOURCES)[N]['answer']>

// ---- what an event does ----------------------------------------------------------------------

/** One row: a live event either carries the whole object and is written into a resource
 * (`write` returns the new data, or null when it cannot place the event: the resource is stale
 * then), or only says that something changed (`stale` names the resources to read again). */
interface EventRow {
  write?: { [N in ResourceName]?: (data: ResourceData<N>, event: any) => ResourceData<N> | null }
  stale?: (event: any, before: any) => ResourceName[]
}

export const ON_EVENT: Record<string, EventRow> = {
  // one Build; once it ended, what it changed (the current Build, who uses what, older Builds
  // removed) is read again
  build: {
    write: { images: (data, event: BuildEvent) => (event?.image && event.build ? applyBuild(data, event) : data) },
    stale: (event: BuildEvent) => (event?.status !== 'running' ? ['images'] : []),
  },
  // the update of every Image ended: it may have built nothing, so no `build` event says so
  services: {
    stale: (event, before) => (before?.updating === true && event?.updating === false ? ['images'] : []),
  },
}

/** Activity entries (`code ?? action`) after which resources are read again. */
export const ON_ACTIVITY: Record<string, ResourceName[]> = {
  'maintenance.versions': ['images'],
  'build.ok': ['images'],
  'update-now': ['images'],
  // a Recipe defines an Image: it comes, changes or goes without a Build
  'recipe.set': ['images'],
  'recipe.restore': ['images'],
  'recipe.remove': ['images'],
  // which VMs use and pin each Image and Build (`vms`, `used_by`, `pinned_by`)
  'create': ['images'],
  'delete': ['images'],
  'set-image': ['images'],
  'reset': ['images'],
  'reset.automatic': ['images'],
  'reset.update': ['images'],
}

// ---- the cache -------------------------------------------------------------------------------

/** The cache and its wiring to one live connection. */
export function createResources(live: Pick<Live, 'on' | 'state'>) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: Infinity,
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        refetchInterval: false,
        networkMode: 'always',
      },
    },
  })

  // replays: events that arrived while a read of their resource was on the way, applied to its
  // answer, so that the older answer does not undo them
  const replays = new Map<ResourceName, Set<((data: any) => any)[]>>()
  // resources to read once more when the read on the way ends: it may have started before what
  // made them stale
  const again = new Set<ResourceName>()

  async function read<N extends ResourceName>(name: N): Promise<ResourceData<N>> {
    const pending: ((data: any) => any)[] = []
    const set = replays.get(name) ?? new Set()
    replays.set(name, set)
    set.add(pending)
    try {
      let data = await api<ResourceData<N>>(RESOURCES[name].path)
      for (const apply of pending) {
        const next = apply(data)
        if (next === null) again.add(name)
        else data = next
      }
      return data
    }
    finally {
      set.delete(pending)
    }
  }

  function fetching(name: ResourceName) {
    return client.getQueryState([name])?.fetchStatus === 'fetching'
  }

  /** Mark a resource stale: a view that shows it reads it again, at once or, when a read is on
   * the way, right after it; one that is not shown reads it when it is next shown. */
  function markStale(name: ResourceName): Promise<void> {
    if (fetching(name)) {
      again.add(name)
      return Promise.resolve()
    }
    return client.invalidateQueries({ queryKey: [name] }, { cancelRefetch: false })
  }

  function write<N extends ResourceName>(name: N, apply: (data: ResourceData<N>) => ResourceData<N> | null) {
    const pendings = replays.get(name)
    if (pendings?.size) for (const p of pendings) p.push(apply)
    const query = client.getQueryCache().find<ResourceData<N>>({ queryKey: [name], exact: true })
    const data = query?.state.data
    if (!query || data === undefined) return
    const next = apply(data)
    if (next === null) void markStale(name)
    // only the data: a stale mark and a failed read's error stay (setQueryData would clear both)
    else query.setState({ data: next, dataUpdatedAt: Date.now() })
  }

  client.getQueryCache().subscribe((event) => {
    if (event.type !== 'updated' || (event.action.type !== 'success' && event.action.type !== 'error')) return
    const name = event.query.queryKey[0] as ResourceName
    if (again.delete(name)) void markStale(name)
  })

  // the last of each event, for a row that compares (`services`: did the update end?)
  const before: Record<string, unknown> = { services: live.state.services }
  const offs = Object.entries(ON_EVENT).map(([eventName, row]) => live.on(eventName, (data: unknown) => {
    for (const [name, apply] of Object.entries(row.write ?? {}) as [ResourceName, (d: any, e: any) => any][]) {
      write(name, d => apply(d, data))
    }
    for (const name of row.stale?.(data, before[eventName]) ?? []) void markStale(name)
    before[eventName] = data
  }))

  offs.push(live.on('activity', (entry: { code?: string, action?: string } | null) => {
    const code = entry?.code ?? entry?.action
    for (const name of (code && ON_ACTIVITY[code]) || []) void markStale(name)
  }))

  const missedListeners = new Set<() => void>()
  // a stream id: a `hello` came before the cache was made
  let helloSeen = live.state.stream !== null
  offs.push(live.on('hello', (data: { resumed?: boolean } | null) => {
    // a reconnect that could not resume missed events: everything is stale (the first `hello`
    // of the page is no reconnect)
    const missed = helloSeen && data?.resumed === false
    helloSeen = true
    for (const query of client.getQueryCache().getAll()) {
      // the connection is there: a read that failed because the Control Room could not be
      // reached is read again
      if (missed || (query.state.status === 'error' && unreachable(query.state.error))) {
        void markStale(query.queryKey[0] as ResourceName)
      }
    }
    if (missed) for (const fn of [...missedListeners]) fn()
  }))

  return {
    client,
    read,
    markStale,
    /** Called after a `hello` with `resumed: false` that is not the first: events were missed.
     * For what lives outside the cache (a Build's log). */
    onMissedEvents(fn: () => void) {
      missedListeners.add(fn)
      return () => { missedListeners.delete(fn) }
    },
    stop() {
      for (const off of offs) off()
      client.clear()
    },
  }
}

export type Resources = ReturnType<typeof createResources>
let resources: Resources | null = null

function useResources(): Resources {
  resources ??= createResources(useLive())
  return resources
}

/** Forget every resource (signing out). */
export function clearResources() {
  resources?.stop()
  resources = null
}

// ---- what a view uses ------------------------------------------------------------------------

export interface ResourceOptions {
  /** a read whose failure the view does not show (ReadFailure shows nothing) */
  extra?: boolean
  /** read only while this is true (a dialog: while it is open) */
  enabled?: MaybeRefOrGetter<boolean>
}

export interface Resource<T> {
  name: ResourceName
  /** the last answer; undefined until one came */
  data: Readonly<Ref<T | undefined>>
  /** nothing came yet and a read is on the way */
  loading: ComputedRef<boolean>
  /** why the last read failed; null after one that succeeded */
  error: Readonly<Ref<unknown>>
  extra: boolean
  /** read it again now ("Retry") */
  retry: () => Promise<void>
}

/** A resource by name, for a view: one shared copy per resource, kept current by the table above. */
export function useResource<N extends ResourceName>(name: N, options: ResourceOptions = {}): Resource<ResourceData<N>> {
  const r = useResources()
  const query = useQuery({
    queryKey: [name],
    queryFn: () => r.read(name),
    enabled: () => toValue(options.enabled ?? true),
  }, r.client)
  return {
    name,
    data: query.data as Readonly<Ref<ResourceData<N> | undefined>>,
    loading: computed(() => query.isLoading.value),
    error: query.error,
    extra: !!options.extra,
    retry: async () => { await query.refetch() },
  }
}

/** A change made in this browser: runs the request and marks what it changes stale, whether it
 * succeeded or was refused (a refusal may say that something else runs already). */
export async function change<T = unknown>(path: string, options: Parameters<typeof api>[1], stale: ResourceName[]): Promise<T> {
  const r = useResources()
  try {
    return await api<T>(path, options)
  }
  finally {
    await Promise.all(stale.map(name => r.markStale(name)))
  }
}

/** See `onMissedEvents` of the resources. */
export function onMissedEvents(fn: () => void) {
  return useResources().onMissedEvents(fn)
}

/** Whether a read failed only because the Control Room could not be reached. */
export function unreachable(error: unknown) {
  return error instanceof ApiError && error.code === 'network'
}
