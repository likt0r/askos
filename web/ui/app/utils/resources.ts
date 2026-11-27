// The resources module (ADR 0026): every resource of the Control Room API the UI reads, declared
// once, in one cache (TanStack Query), and the table of what a live event does to it. A view asks
// for a resource by name (`useResource('images')`) and gets its data, whether it is loading and
// its failure; it knows neither the path nor when the data is read again.
//
// Changes are declared here too (CHANGES) and run through it (`useChange('rebuild')`): it runs the
// request, marks what the change makes stale and presents a failure where utils/failures.ts says.
//
// Data is read again only for a reason the stream or the owner gives: a row of the tables below
// (an event, an Activity entry, a change made in this browser), a `hello` with `resumed: false`
// that is not the first, or "Retry"; and a resource the Host sends no event about (`fresh`) when a
// view of it opens. Never by the module's clock (a view may choose its own: `every`), never on
// window focus, never as an automatic retry; a read that failed because the Control Room could
// not be reached is read again with the next `hello`.
import { hashKey, keepPreviousData, notifyManager, QueryClient, useMutation, useQuery } from '@tanstack/vue-query'
import { computed, type ComputedRef, type MaybeRefOrGetter, type Ref, ref, toValue } from 'vue'
import { api, ApiError } from './api'
import { allowlistEntryPath, type AllowlistEntry, entryPath, forbidPath, type ForbidEntry, type GithubAppOverview, type GithubTokens, type SshConfig, type Target } from './access'
import { useChangeToast } from '../composables/useChangeToast'
import { failurePlace, type FailurePlace, unreachable } from './failures'
import { applyBuild, type BuildEvent, type BuildLog, type BuildLogEvent, buildLogPath, type ContainerfileText, type ImagesOverview, joinLog, withLogEvent } from './images'
import { type HostOverview, type HostSample, noReadings, withSample } from './host'
import type { SettingInfo, SettingValue } from './settings'
import type { Pairings } from './vm'
import { type ActivityEntry, type ActivityParams, activityQuery, type ActivityResult, activityText, isEntry, matchesFilter, type Translator, withEntry } from './activity'
import { joinVmLog, type LogFilters, type VmLog, vmLogPath } from './vmRules'
import type { Live, VersionInfo, Vm } from './live'

// query-core hands results to the views through setTimeout(0) by default; a microtask gives them
// in the same turn, which the tests wait for with flushPromises. Production takes the same path.
notifyManager.setScheduler(queueMicrotask)

// ---- the resources ---------------------------------------------------------------------------

interface Declaration<T, P> {
  /** `held`: the copy in the cache, for a resource read on from where it is (a Build's log) */
  path: (params: P, held: T | undefined) => string
  /** an answer that continues the held copy, joined to it */
  merge?: (held: T | undefined, answer: any) => T
  /** what a copy becomes when events were missed (a reconnect that could not resume), before it
   * is read again: one read on from where it is must not take later events past the gap */
  missed?: (held: T) => T
  /** the copy read still has a gap (its answer was cut short, or events were skipped meanwhile):
   * read on right after it */
  readOn?: (data: T) => boolean
  /** the Host sends no event about its changes: read again whenever a view of it opens (the owner
   * on #216) */
  fresh: boolean
  /** not kept once no view shows it (a copy each view opening reads anew anyway) */
  forget: boolean
  /** codes the Host answers for a state, not a failure: read as this data */
  states: Record<string, () => T>
  /** only carry the types of the answer and of the parameters */
  answer?: T
  params?: P
}

/** A resource: its path, or for one with parameters (a VM's), its path from them. */
function declare<T, P = undefined>(path: string | ((params: P, held: T | undefined) => string),
  options: { fresh?: boolean, forget?: boolean, states?: Record<string, () => T>, merge?: (held: T | undefined, answer: any) => T,
    missed?: (held: T) => T, readOn?: (data: T) => boolean } = {}): Declaration<T, P> {
  return { path: typeof path === 'string' ? () => path : path, fresh: !!options.fresh, forget: !!options.forget,
    states: options.states ?? {}, merge: options.merge, missed: options.missed, readOn: options.readOn }
}

const RESOURCES = {
  images: declare<ImagesOverview>('/images'),
  // a device pairs inside Orca with the code: nothing on the Host records it
  pairings: declare<Pairings, { vm: string }>(p => `/vms/${p.vm}/pairings`, { fresh: true }),
  // the tokens a VM got and the requests refused, for a period
  githubTokens: declare<GithubTokens, { vm: string, since: string }>(p => `/vms/${p.vm}/github-tokens?since=${encodeURIComponent(p.since)}`),
  // a VM's SSH connection help (its `~/.ssh/config` block and host key)
  sshConfig: declare<SshConfig, { vm: string }>(p => `/vms/${p.vm}/ssh-config`),
  // an App installed or uninstalled on GitHub: nothing on the Host records it; no App set up is a
  // state, not a failure
  githubApp: declare<GithubAppOverview>('/github-app', {
    fresh: true,
    states: { github_app_missing: () => ({ apps: [], missing: [], none: true }) },
  }),
  // the Host's readings with 30 minutes of history; a Host without a sampler is a state
  host: declare<HostOverview>('/host', { states: { readings_unavailable: noReadings } }),
  // the UI build installed and the Release of Agent Box the Host runs
  version: declare<VersionInfo>('/version'),
  // a Build's log: read on after the held cursor, so only what is new comes
  buildLog: declare<BuildLog, { image: string, build: string }>((p, held) => buildLogPath(p.image, p.build, held),
    { merge: joinLog, missed: log => ({ ...log, gap: true }), readOn: log => !!log.gap }),
  // a VM's journal for the filters the owner applied (`shown`: each "Show", and each opening of
  // the tab, is a new copy, the newest entries read anew): read on after the held cursor while
  // following, by the view's clock (what one answer could not hold comes with the next), and
  // when marked stale (the journal's cursor does not depend on the stream); nothing on the Host
  // says when it grows
  vmLog: declare<VmLog, { vm: string, filters: LogFilters, shown: number }>((p, held) => vmLogPath(p.vm, p.filters, held),
    { fresh: true, forget: true, merge: joinVmLog }),
  // the Settings (the form holds a draft of them; a VM's defaults read them too)
  settings: declare<{ settings: Record<string, SettingInfo> }>('/settings'),
  // the Activity timeline for a filter, the newest `lines` entries (`since`: only that recent)
  activity: declare<ActivityResult, ActivityParams>(p => `/activity?${activityQuery(p, p.lines, p.since)}`),
  // an Image's Containerfile: the installed Release's, or its Recipe's
  containerfile: declare<ContainerfileText, { image: string }>(p => `/images/${encodeURIComponent(p.image)}/containerfile`),
}

export type ResourceName = keyof typeof RESOURCES
export type ResourceParams<N extends ResourceName> = (typeof RESOURCES)[N] extends Declaration<any, infer P> ? P : never
export type ResourceData<N extends ResourceName> = NonNullable<(typeof RESOURCES)[N]['answer']>

/** The key of a copy in the cache: `[name]`, or `[name, params]` for one with parameters. */
const keyOf = (name: ResourceName, params?: unknown) => (params === undefined ? [name] : [name, params])

// ---- what an event does ----------------------------------------------------------------------

/** What an event or an Activity entry tells about whom it concerns: its VM, and an entry's
 * parameters (a Recipe's `name`). */
interface Concerns { vm?: string | null, params?: Record<string, unknown> | null }

/** A resource an event or an Activity entry makes stale: by its name, or for one with
 * parameters, only those of the event (its VM's, its Image's). */
type Stale = ResourceName | { name: ResourceName, params: (entry: Concerns) => unknown }

/** The resources a VM has one of (their parameters are `{ vm }`). */
type VmResourceName = { [N in ResourceName]: ResourceParams<N> extends { vm: string } ? N : never }[ResourceName]

/** A VM's resource, of the entry's VM (every VM's when it names none). */
const ofEntryVm = (name: VmResourceName): Stale => ({ name, params: entry => (entry.vm ? { vm: entry.vm } : undefined) })

/** An Image's resource, of the Image the entry's `name` names (every Image's when it names none). */
const ofEntryImage = (name: 'containerfile'): Stale => ({
  name,
  params: entry => (typeof entry.params?.name === 'string' && entry.params.name ? { image: entry.params.name } : undefined),
})

/** One row: a live event either carries the whole object and is written into a resource
 * (`write` returns the new data, or null when it cannot place the event: the resource is stale
 * then), or only says that something changed (`stale` names the resources to read again). */
interface EventRow {
  write?: { [N in ResourceName]?: (data: ResourceData<N>, event: any) => ResourceData<N> | null }
  /** the parameters of the copy `write` goes into (a Build's log: the event's Image and Build);
   * none: the resource without parameters */
  writeParams?: (event: any) => unknown
  /** written into every copy of the resource, with its parameters (each Activity filter) */
  writeEach?: { [N in ResourceName]?: (data: ResourceData<N>, event: any, params: ResourceParams<N>, context: Context) => ResourceData<N> | null }
  /** `before`: the last event of this row, or with `key` the last of the same object */
  stale?: (event: any, before: any) => Stale[]
  /** compare per object (a VM by its name) */
  key?: (event: any) => string | undefined
  /** the objects known before their first event: from the live state, and from each `state`
   * event, which sends them all */
  known?: (state: any) => any[]
}

/** A Build's log, of the event's Image and Build. */
const ofBuild = (event: { image?: string | null, build?: string | null }): Stale =>
  ({ name: 'buildLog', params: () => ({ image: event.image, build: event.build }) })

/** What a row may need of the page: the words an Activity entry shows in the UI's language. */
interface Context {
  words: (entry: ActivityEntry) => string
}

const ON_EVENT: Record<string, EventRow> = {
  // one whole Activity entry: into each timeline whose filter it passes (by agentbox's text or
  // the words the UI shows for it), newest first, no repeats (`addEntry`)
  activity: {
    writeEach: {
      activity: (data, entry, params, context) => (isEntry(entry) && matchesFilter(entry, params, context.words(entry))
        ? withEntry(data, entry, params.lines)
        : data),
    },
  },
  // one Build; once it ended, what it changed (the current Build, who uses what, older Builds
  // removed) is read again, and the end of its log
  build: {
    write: { images: (data, event: BuildEvent) => (event?.image && event.build ? applyBuild(data, event) : data) },
    stale: (event: BuildEvent) => (event?.status !== 'running'
      ? ['images', ...(event?.image && event.build ? [ofBuild(event)] : [])]
      : []),
  },
  // lines of a running Build's log: appended to it; lines skipped (`truncated`): the gap is read
  'build-log': {
    writeParams: (event: BuildLogEvent) => (event?.image && event.build ? { image: event.image, build: event.build } : undefined),
    write: { buildLog: (data, event: BuildLogEvent) => withLogEvent(data, event) },
    stale: (event: BuildLogEvent) => (event?.truncated && event.image && event.build ? [ofBuild(event)] : []),
  },
  // a VM's SSH access turned on or off, or its host key made: its connection help is read again
  // (whatever did it: the UI, the CLI, regenerate)
  vm: {
    key: (vm: Vm) => vm?.name,
    known: (state: { vms?: Vm[] }) => (Array.isArray(state?.vms) ? state.vms : []),
    stale: (vm: Vm, before: Vm | undefined) => (before && vm?.name
      && (before.ssh?.enabled !== vm.ssh?.enabled || before.ssh?.host_key_fingerprint !== vm.ssh?.host_key_fingerprint)
      ? [{ name: 'sshConfig', params: () => ({ vm: vm.name }) }]
      : []),
  },
  // a sample of the Host's readings (every 5 s): appended to the history
  host: {
    write: { host: (data, sample: HostSample) => withSample(data, sample) },
  },
  // a VM got a GitHub token or was refused one: its list of tokens and requests is read again
  'github-token': {
    stale: () => [ofEntryVm('githubTokens')],
  },
  // the update of every Image ended: it may have built nothing, so no `build` event says so; the
  // version check ended (`available`, what a Build would update): started by hand, it records no
  // Activity entry
  services: {
    stale: (event, before) => ((before?.updating === true && event?.updating === false)
      || (versionCheckRuns(before) && !versionCheckRuns(event)) ? ['images'] : []),
  },
}

const VERSION_CHECK = 'agentbox-versions.service'

function versionCheckRuns(services: { units?: { unit?: string, state?: string }[] } | null | undefined) {
  const unit = services?.units?.find(u => u?.unit === VERSION_CHECK)
  return unit?.state === 'running' || unit?.state === 'stopping'
}

/** Activity entries (`code ?? action`) after which resources are read again. The end of a Build
 * needs no row: its `build` event marks `images` stale. */
const ON_ACTIVITY: Record<string, Stale[]> = {
  'maintenance.versions': ['images'],
  'update-now': ['images'],
  // a Recipe defines an Image: it comes, changes or goes without a Build, and so does the text of
  // its Containerfile
  'recipe.set': ['images', ofEntryImage('containerfile')],
  'recipe.restore': ['images', ofEntryImage('containerfile')],
  'recipe.remove': ['images', ofEntryImage('containerfile')],
  // which VMs use and pin each Image and Build (`vms`, `used_by`, `pinned_by`)
  'create': ['images', 'githubApp'],
  'delete': ['images', 'githubApp'],
  'set-image': ['images'],
  'reset': ['images'],
  'reset.automatic': ['images'],
  'reset.update': ['images'],
  // the GitHub Apps, their Installations and which VMs' owners miss one
  'set-github-app': ['githubApp'],
  'remove-github-app': ['githubApp'],
  'set-owners': ['githubApp'],
  // the Settings
  'set-settings': ['settings'],
  // a VM's Paired devices
  'observe': [ofEntryVm('pairings')],
  'unpair': [ofEntryVm('pairings')],
  'revoke-pairings': [ofEntryVm('pairings')],
}

// ---- the changes -----------------------------------------------------------------------------

type Method = 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface ChangeRequest {
  method: Method
  path: string
  body?: unknown
}

interface ChangeDeclaration<A> {
  request: (args: A) => ChangeRequest
  /** the resources it makes stale without an Activity entry saying so */
  stale: ResourceName[]
}

function declareChange<A = void>(request: (args: A) => ChangeRequest, stale: ResourceName[] = []): ChangeDeclaration<A> {
  return { request, stale }
}

/** Every change the views make, declared once: its request, and what it makes stale. A change
 * that records an Activity entry is read again through that entry's row in ON_ACTIVITY, once, so
 * it names nothing here (the owner on #215). */
const CHANGES = {
  // the Images: a Build's `build` events and the Activity entries say what changed
  rebuild: declareChange((image: string) => ({ method: 'POST', path: '/builds', body: { image } })),
  updateImage: declareChange((a: { image: string, reset: string[] }) =>
    ({ method: 'POST', path: '/builds', body: a.reset.length ? { image: a.image, reset: a.reset } : { image: a.image } })),
  updateNow: declareChange((reset: string[]) => ({ method: 'POST', path: '/updates', body: reset.length ? { reset } : {} })),
  // the Settings (Activity kind `settings`)
  setSettings: declareChange((body: Record<string, SettingValue>) => ({ method: 'PATCH', path: '/settings', body })),
  // the GitHub Apps
  setGithubApp: declareChange((body: { app_id: number, private_key: string }) => ({ method: 'PUT', path: '/github-app', body })),
  removeGithubApp: declareChange((id: number) => ({ method: 'DELETE', path: `/github-app/${id}` })),
  // a VM's lifecycle: its new state comes as a `vm` event
  startVm: declareChange((vm: string) => ({ method: 'POST', path: `/vms/${vm}/start` })),
  pauseVm: declareChange((vm: string) => ({ method: 'POST', path: `/vms/${vm}/pause` })),
  restartVm: declareChange((vm: string) => ({ method: 'POST', path: `/vms/${vm}/restart` })),
  resetVm: declareChange((a: { vm: string, start: boolean }) => ({ method: 'POST', path: `/vms/${a.vm}/reset`, body: { start: a.start } })),
  focus: declareChange((vm: string) => ({ method: 'POST', path: `/vms/${vm}/focus` })),
  focusOff: declareChange(() => ({ method: 'DELETE', path: '/focus' })),
  // a VM's record
  createVm: declareChange((body: {
    name: string, image: string | null, memory_mib: number | null, data_disk_gib: number | null, github_owners: string[],
    copy_allowlist_from: string | null, copy_ssh_keys_from: string | null,
  }) => ({ method: 'POST', path: '/vms', body })),
  deleteVm: declareChange((a: { vm: string, deleteDataDisk: boolean }) =>
    ({ method: 'DELETE', path: `/vms/${a.vm}`, body: { confirm: a.vm, delete_data_disk: a.deleteDataDisk } })),
  setImage: declareChange((a: { vm: string, image: string, build: string | null }) =>
    ({ method: 'PUT', path: `/vms/${a.vm}/image`, body: { image: a.image, build: a.build } })),
  setMemory: declareChange((a: { vm: string, mib: number }) => ({ method: 'PUT', path: `/vms/${a.vm}/memory`, body: { memory_mib: a.mib } })),
  setEnv: declareChange((a: { vm: string, env: Record<string, string> }) => ({ method: 'PUT', path: `/vms/${a.vm}/env`, body: { env: a.env } })),
  setSecret: declareChange((a: { vm: string, name: string, value: string }) =>
    ({ method: 'PUT', path: `/vms/${a.vm}/secrets/${a.name}`, body: { value: a.value } })),
  removeSecret: declareChange((a: { vm: string, name: string }) => ({ method: 'DELETE', path: `/vms/${a.vm}/secrets/${a.name}` })),
  setContainerImageCleanup: declareChange((a: { vm: string, enabled?: boolean, days?: number }) =>
    ({ method: 'PUT', path: `/vms/${a.vm}/container-image-cleanup`, body: { enabled: a.enabled, days: a.days } })),
  // a VM's access: GitHub owners, its Claude token (it stays on the Host), SSH access and keys
  setOwners: declareChange((a: { vm: string, owners: string[] }) => ({ method: 'PUT', path: `/vms/${a.vm}/github-owners`, body: { owners: a.owners } })),
  setClaudeToken: declareChange((a: { vm: string, token: string }) => ({ method: 'PUT', path: `/vms/${a.vm}/claude-token`, body: { token: a.token } })),
  removeClaudeToken: declareChange((vm: string) => ({ method: 'DELETE', path: `/vms/${vm}/claude-token` })),
  setSsh: declareChange((a: { vm: string, enabled: boolean }) => ({ method: 'PUT', path: `/vms/${a.vm}/ssh`, body: { enabled: a.enabled } })),
  addSshKey: declareChange((a: { vm: string, key: string, name: string }) =>
    ({ method: 'POST', path: `/vms/${a.vm}/ssh-keys`, body: { key: a.key, ...(a.name ? { name: a.name } : {}) } })),
  copySshKeys: declareChange((a: { vm: string, from: string }) => ({ method: 'POST', path: `/vms/${a.vm}/ssh-keys`, body: { from: a.from } })),
  removeSshKey: declareChange((a: { vm: string, fingerprint: string }) =>
    ({ method: 'POST', path: `/vms/${a.vm}/ssh-keys/remove`, body: { fingerprint: a.fingerprint } })),
  // pairing a device with a VM's Orca
  revealPairingCode: declareChange((vm: string) => ({ method: 'POST', path: `/vms/${vm}/pairing-code`, body: { confirm: true } })),
  revokePairings: declareChange((vm: string) => ({ method: 'POST', path: `/vms/${vm}/pairings/revoke`, body: { confirm: vm } })),
  removePairing: declareChange((a: { vm: string, device: string }) => ({ method: 'POST', path: `/vms/${a.vm}/pairings/remove`, body: { device: a.device } })),
  // Allowlist entries and Forbids (Activity kind `allowlist`; an allow and a Forbid change both lists)
  allow: declareChange((a: { scope: string, domain?: string, regex?: string, duration: string, note: string }) => ({
    method: 'POST',
    path: '/allowlist',
    // a pattern is sent with its confirmation (its preview was shown and confirmed)
    body: { scope: a.scope, ...(a.regex ? { regex: a.regex, confirm: true } : { domain: a.domain }), duration: a.duration, ...(a.note ? { note: a.note } : {}) },
  })),
  extendEntry: declareChange((a: { entry: AllowlistEntry, duration: string }) =>
    ({ method: 'POST', path: allowlistEntryPath(a.entry, 'extend'), body: { duration: a.duration } })),
  makeEntryPermanent: declareChange((entry: AllowlistEntry) => ({ method: 'POST', path: allowlistEntryPath(entry, 'make-permanent') })),
  setEntryNote: declareChange((a: { entry: AllowlistEntry, note: string }) =>
    ({ method: 'PUT', path: allowlistEntryPath(a.entry, 'note'), body: { note: a.note } })),
  removeEntry: declareChange((entry: AllowlistEntry) => ({ method: 'DELETE', path: allowlistEntryPath(entry) })),
  forbid: declareChange((a: { vm: string, domain: string, duration: string, note: string }) => ({
    method: 'POST',
    path: `/vms/${encodeURIComponent(a.vm)}/forbids`,
    body: { domain: a.domain, duration: a.duration, ...(a.note ? { note: a.note } : {}) },
  })),
  extendForbid: declareChange((a: { forbid: ForbidEntry, duration: string }) =>
    ({ method: 'POST', path: forbidPath(a.forbid, 'extend'), body: { duration: a.duration } })),
  makeForbidPermanent: declareChange((forbid: ForbidEntry) => ({ method: 'POST', path: forbidPath(forbid, 'make-permanent') })),
  setForbidNote: declareChange((a: { forbid: ForbidEntry, note: string }) =>
    ({ method: 'PUT', path: forbidPath(a.forbid, 'note'), body: { note: a.note } })),
  removeForbid: declareChange((forbid: ForbidEntry) => ({ method: 'DELETE', path: forbidPath(forbid) })),
  // Open web: its time comes with the VM (`open_web_until`)
  openWeb: declareChange((a: { vm: string, minutes: number }) => ({ method: 'POST', path: `/vms/${a.vm}/open-web`, body: { minutes: a.minutes } })),
  closeWeb: declareChange((vm: string) => ({ method: 'DELETE', path: `/vms/${vm}/open-web` })),
  // the Watchlist kept by hand, Legitimate domains, Harmless marks, Alerts seen (each records an
  // Activity entry: watch-*, legitimate-*, mark-harmless, unmark-harmless, mark-seen)
  watchAdd: declareChange((a: { target: Target, category: string, source: string, note: string }) => ({
    method: 'POST',
    path: '/watchlist/entries',
    body: { ...targetBody(a.target), category: a.category, ...(a.source ? { source: a.source } : {}), ...(a.note ? { note: a.note } : {}) },
  })),
  setWatchNote: declareChange((a: { target: Target, note: string }) =>
    ({ method: 'PUT', path: entryPath('/watchlist/entries', a.target, 'note'), body: { note: a.note } })),
  watchRemove: declareChange((target: Target) => ({ method: 'DELETE', path: entryPath('/watchlist/entries', target) })),
  legitimateAdd: declareChange((a: { target: Target, note: string }) => ({
    method: 'POST',
    path: '/watchlist/legitimate',
    body: { ...targetBody(a.target), ...(a.note ? { note: a.note } : {}) },
  })),
  setLegitimateNote: declareChange((a: { target: Target, note: string }) =>
    ({ method: 'PUT', path: entryPath('/watchlist/legitimate', a.target, 'note'), body: { note: a.note } })),
  legitimateRemove: declareChange((target: Target) => ({ method: 'DELETE', path: entryPath('/watchlist/legitimate', target) })),
  markHarmless: declareChange((target: Target) => ({ method: 'PUT', path: entryPath('/watchlist/harmless', target) })),
  unmarkHarmless: declareChange((target: Target) => ({ method: 'DELETE', path: entryPath('/watchlist/harmless', target) })),
  markSeen: declareChange((a: { vm: string, domain?: string }) =>
    ({ method: 'POST', path: `/vms/${a.vm}/alerts/seen`, body: a.domain ? { domain: a.domain } : {} })),
}

/** A domain or a pattern in a request's body. */
function targetBody(t: Target) {
  return t.regex != null ? { regex: t.regex } : { domain: t.domain }
}

export type ChangeName = keyof typeof CHANGES
export type ChangeArgs<N extends ChangeName> = Parameters<(typeof CHANGES)[N]['request']>[0]

// ---- the cache -------------------------------------------------------------------------------

/** The cache and its wiring to one live connection. */
export function createResources(live: Pick<Live, 'on' | 'state'>, context: Context = { words: () => '' }) {
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

  // replays: events that arrived while a read of their copy was on the way (by the hash of its
  // key), applied to its answer, so that the older answer does not undo them
  const replays = new Map<string, Set<((data: any) => any)[]>>()
  // copies (by the hash of their key) to read once more when the read on the way ends: it may
  // have started before what made them stale
  const again = new Set<string>()

  async function read<N extends ResourceName>(name: N, params?: ResourceParams<N>): Promise<ResourceData<N>> {
    const queryKey = keyOf(name, params)
    const pending: ((data: any) => any)[] = []
    const set = replays.get(hashKey(queryKey)) ?? new Set()
    replays.set(hashKey(queryKey), set)
    set.add(pending)
    try {
      const declaration = RESOURCES[name] as Declaration<ResourceData<N>, unknown>
      const held = client.getQueryData<ResourceData<N>>(queryKey)
      let data: ResourceData<N>
      try {
        const answer = await api<ResourceData<N>>(declaration.path(params, held))
        data = declaration.merge ? declaration.merge(held, answer) : answer
      }
      catch (e) {
        const state = e instanceof ApiError && Object.hasOwn(declaration.states, e.code) ? declaration.states[e.code] : undefined
        if (!state) throw e
        data = state()
      }
      for (const apply of pending) {
        const next = apply(data)
        if (next === null) again.add(hashKey(queryKey))
        else data = next
      }
      if (declaration.readOn?.(data)) again.add(hashKey(queryKey))
      return data
    }
    finally {
      set.delete(pending)
      if (!set.size) replays.delete(hashKey(queryKey))
    }
  }

  /** Mark a resource stale, with every set of its parameters or only these: a view that shows a
   * copy reads it again, at once or, when a read of that copy is on the way, right after it; one
   * that is not shown reads it when it is next shown. */
  function markStale(name: ResourceName, params?: unknown): Promise<void> {
    const queryKey = keyOf(name, params)
    for (const query of client.getQueryCache().findAll({ queryKey, fetchStatus: 'fetching' })) again.add(query.queryHash)
    return client.invalidateQueries({ queryKey, predicate: query => query.state.fetchStatus !== 'fetching' }, { cancelRefetch: false })
  }

  function markStaleBy(stale: Stale, event: Concerns) {
    if (typeof stale === 'string') void markStale(stale)
    else void markStale(stale.name, stale.params(event ?? {}))
  }

  function write<N extends ResourceName>(name: N, params: unknown, apply: (data: ResourceData<N>) => ResourceData<N> | null) {
    const queryKey = keyOf(name, params)
    const pendings = replays.get(hashKey(queryKey))
    if (pendings?.size) for (const p of pendings) p.push(apply)
    const query = client.getQueryCache().find<ResourceData<N>>({ queryKey, exact: true })
    const data = query?.state.data
    if (!query || data === undefined) return
    const next = apply(data)
    if (next === null) void markStale(name, params)
    // only the data: a stale mark and a failed read's error stay (setQueryData would clear both)
    else query.setState({ data: next, dataUpdatedAt: Date.now() })
  }

  const offs = [client.getQueryCache().subscribe((event) => {
    if (event.type !== 'updated' || (event.action.type !== 'success' && event.action.type !== 'error')) return
    if (again.delete(event.query.queryHash)) void client.invalidateQueries({ queryKey: event.query.queryKey, exact: true }, { cancelRefetch: false })
  })]

  // the last of each event, for a row that compares (`services`: did the update end?), per object
  // for a row with a key (`vm`: a VM's SSH access)
  const before = new Map<string, unknown>([['services', live.state.services]])
  const slot = (eventName: string, row: EventRow, data: unknown) => (row.key ? `${eventName}:${row.key(data)}` : eventName)
  /** The objects a row compares, from the live state or a `state` event, which sends them all:
   * an object it already knew is compared as its own event would be. */
  function know(state: unknown, compare: boolean) {
    for (const [eventName, row] of Object.entries(ON_EVENT)) {
      for (const item of row.known?.(state) ?? []) {
        const key = slot(eventName, row, item)
        if (compare) for (const stale of row.stale?.(item, before.get(key)) ?? []) markStaleBy(stale, item)
        before.set(key, item)
      }
    }
  }
  know(live.state, false)
  offs.push(live.on('state', (state: unknown) => know(state, true)))
  offs.push(...Object.entries(ON_EVENT).map(([eventName, row]) => live.on(eventName, (data: unknown) => {
    for (const [name, apply] of Object.entries(row.write ?? {}) as [ResourceName, (d: any, e: any) => any][]) {
      write(name, row.writeParams?.(data), d => apply(d, data))
    }
    for (const [name, apply] of Object.entries(row.writeEach ?? {}) as [ResourceName, (d: any, e: any, p: any, c: Context) => any][]) {
      for (const query of client.getQueryCache().findAll({ queryKey: [name] })) {
        const params = query.queryKey[1]
        write(name, params, d => apply(d, data, params, context))
      }
    }
    for (const stale of row.stale?.(data, before.get(slot(eventName, row, data))) ?? []) markStaleBy(stale, data as Concerns)
    before.set(slot(eventName, row, data), data)
  })))

  offs.push(live.on('activity', (entry: (Concerns & { code?: string, action?: string }) | null) => {
    const code = entry?.code ?? entry?.action
    for (const stale of (entry && code && ON_ACTIVITY[code]) || []) markStaleBy(stale, entry!)
  }))

  // a stream id: a `hello` came before the cache was made
  let helloSeen = live.state.stream !== null
  offs.push(live.on('hello', (data: { resumed?: boolean } | null) => {
    // a reconnect that could not resume missed events: everything is stale (the first `hello`
    // of the page is no reconnect)
    const missed = helloSeen && data?.resumed === false
    helloSeen = true
    for (const query of client.getQueryCache().getAll()) {
      const [name, params] = query.queryKey as [ResourceName, unknown?]
      const gap = missed ? (RESOURCES[name] as Declaration<unknown, unknown>).missed : undefined
      if (gap) {
        // the copy, and the answer of a read on the way, have a gap
        for (const pending of replays.get(query.queryHash) ?? []) pending.push(gap)
        if (query.state.data !== undefined) query.setState({ data: gap(query.state.data) })
      }
      // the connection is there: a read that failed because the Control Room could not be
      // reached is read again
      if (missed || (query.state.status === 'error' && unreachable(query.state.error))) {
        void markStale(name, params)
      }
    }
  }))

  return {
    client,
    read,
    markStale,
    stop() {
      for (const off of offs) off()
      client.clear()
    },
  }
}

export type Resources = ReturnType<typeof createResources>
let resources: Resources | null = null

function useResources(): Resources {
  resources ??= createResources(useLive(), { words: activityWords() })
  return resources
}

/** The words of an Activity entry in the UI's language, as it shows them (useActivityText): from
 * the app's i18n, whose language may change. */
function activityWords(): Context['words'] {
  const i18n = useNuxtApp().$i18n
  const tr: Translator = { t: (key, values) => i18n.t(key, values ?? {}), te: key => i18n.te(key) }
  return entry => activityText(entry, tr)
}

/** Tests only: the cache and its wiring. */
export function resourcesForTests(): Resources {
  return useResources()
}

/** Forget every resource (signing out). */
export function clearResources() {
  resources?.stop()
  resources = null
}

// ---- what a view uses ------------------------------------------------------------------------

export interface ResourceOptions<P = undefined> {
  /** a read whose failure the view does not show (ReadFailure shows nothing) */
  extra?: boolean
  /** read only while this is true (a dialog: while it is open) */
  enabled?: MaybeRefOrGetter<boolean>
  /** the parameters of a resource that has them (a VM's: `{ vm }`); a change reads the new ones */
  params?: MaybeRefOrGetter<P>
  /** while the copy of new parameters is read, show the one before (a period or filter changed) */
  keepPrevious?: boolean
  /** read again by the clock, every this many ms (false: not): the view's own choice, never the
   * module's (the Log tab while it follows) */
  every?: MaybeRefOrGetter<number | false>
}

export interface Resource<T> {
  name: ResourceName
  /** the last answer; undefined until one came */
  data: Readonly<Ref<T | undefined>>
  /** nothing came yet and a read is on the way */
  loading: ComputedRef<boolean>
  /** a read is on the way (the first or a read again) */
  fetching: ComputedRef<boolean>
  /** with `keepPrevious`: the data shown is the copy of the parameters before, the new one is read */
  previous: ComputedRef<boolean>
  /** why the last read failed; null after one that succeeded */
  error: Readonly<Ref<unknown>>
  extra: boolean
  /** read it again now ("Retry") */
  retry: () => Promise<void>
}

/** The options of a resource: with its parameters when it has them. */
type OptionsOf<N extends ResourceName> = ResourceParams<N> extends undefined
  ? [options?: ResourceOptions]
  : [options: ResourceOptions<ResourceParams<N>> & { params: MaybeRefOrGetter<ResourceParams<N>> }]

/** A resource by name, for a view: one shared copy per resource (and set of parameters), kept
 * current by the tables above. */
export function useResource<N extends ResourceName>(name: N, ...[options = {}]: OptionsOf<N>): Resource<ResourceData<N>> {
  const r = useResources()
  const params = () => toValue(options.params)
  const query = useQuery({
    // one copy per resource, and per set of parameters for one that has them
    queryKey: computed(() => keyOf(name, params())),
    // the parameters of the copy read, from its own key
    queryFn: ({ queryKey }) => r.read(name, queryKey[1] as ResourceParams<N>),
    // one the Host sends no event about is read again whenever a view of it opens
    refetchOnMount: RESOURCES[name].fresh ? 'always' : true,
    ...(RESOURCES[name].forget ? { gcTime: 0 } : {}),
    placeholderData: options.keepPrevious ? keepPreviousData : undefined,
    // a computed, not a function: vue-query takes its change at once (stops the clock); it goes
    // on while the browser tab is in the background
    refetchInterval: computed(() => toValue(options.every) ?? false),
    refetchIntervalInBackground: true,
    enabled: () => toValue(options.enabled ?? true),
  }, r.client)
  return {
    name,
    data: query.data as Readonly<Ref<ResourceData<N> | undefined>>,
    loading: computed(() => query.isLoading.value),
    fetching: computed(() => query.isFetching.value),
    previous: computed(() => query.isPlaceholderData.value),
    error: query.error,
    extra: !!options.extra,
    retry: async () => { await query.refetch() },
  }
}

// ---- changes ---------------------------------------------------------------------------------

export interface ChangeOptions {
  /** sent from a form or a dialog: a failure appears there (ChangeFailure), else as a toast */
  form?: boolean
  /** the codes the view words itself: the module presents none of them */
  own?: string[]
  /** the toast's title for a failure (default: "That did not work") */
  failed?: () => string
}

export type ChangeResult<T> = { ok: true, data: T } | { ok: false, error: unknown }

export interface Change<A, T> {
  /** run the request; a failure is presented by its kind (utils/failures.ts). While a run is on
   * the way another does nothing (`{ ok: false, error: null }`) */
  run: (args: A) => Promise<ChangeResult<T>>
  busy: ComputedRef<boolean>
  /** the last failure; null after a success or reset() */
  error: Readonly<Ref<unknown>>
  /** the failure the form shows (ChangeFailure): none for an own code or one shown elsewhere */
  shown: ComputedRef<unknown>
  /** `shown` in the UI's words, for a view that puts it at one field (`field` of the failure) */
  text: ComputedRef<string | null>
  reset: () => void
}

/** A change by name, for a view, with the arguments its declaration takes. It runs through the
 * module (a TanStack Query mutation), marks what its declaration names stale, and presents its
 * failure where utils/failures.ts says, decided when it fails. */
export function useChange<N extends ChangeName, T = unknown>(name: N, options: ChangeOptions = {}): Change<ChangeArgs<N>, T> {
  const r = useResources()
  const live = useLive()
  const showToast = useChangeToast()
  const errorText = useErrorText()
  const declaration = CHANGES[name] as ChangeDeclaration<ChangeArgs<N>>
  const own = new Set(options.own ?? [])
  // the last failure and where it appears, decided when it failed (not again as the connection
  // changes); kept here, so a reset() while a request is on the way does not lose its answer
  const failure = ref<unknown>(null)
  const place = ref<FailurePlace | null>(null)
  const mutation = useMutation({
    mutationFn: (req: ChangeRequest) => api<T>(req.path, { method: req.method, body: req.body }),
    onSettled: async () => {
      await Promise.all(declaration.stale.map(stale => r.markStale(stale)))
    },
    // the request (a secret's value, a key) is not kept once the change is done
    gcTime: 0,
  }, r.client)
  function reset() {
    failure.value = null
    place.value = null
  }
  return {
    async run(args: ChangeArgs<N>) {
      // one at a time: a second run while the first is on the way does nothing
      if (mutation.isPending.value) return { ok: false, error: null }
      reset()
      try {
        return { ok: true, data: await mutation.mutateAsync(declaration.request(args)) }
      }
      catch (error) {
        failure.value = error
        place.value = error instanceof ApiError && own.has(error.code)
          ? null
          : failurePlace(error, { form: !!options.form, dropped: live.state.connection === 'reconnecting' })
        if (place.value === 'toast') showToast(error, options.failed?.())
        return { ok: false, error }
      }
      finally {
        mutation.reset()
      }
    },
    busy: computed(() => mutation.isPending.value),
    error: computed(() => failure.value),
    shown: computed(() => (place.value === 'form' ? failure.value : null)),
    text: computed(() => (place.value === 'form' ? errorText(failure.value) : null)),
    reset,
  }
}

