// The resources module (ADR 0026, #214) without a view: the stale table's rows for `images`, an
// event during a read, `hello`, no reading by clock or focus. Through the app:
// images-resource.test.ts.
import { QueryObserver } from '@tanstack/vue-query'
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createResources, type Resources } from '~/utils/resources'
import type { ImagesOverview } from '~/utils/images'
import { fakeApi } from './fakes'

const OK = (body: unknown) => ({ status: 200, body })

function overview(builds: { build: string, status: string, file?: string | null }[] = [], extra: Record<string, unknown> = {}): ImagesOverview {
  return {
    images: [{ name: 'base', base: true, in_release: true, current: builds[0]?.build ?? null, vms: [], running: null,
      builds: builds.map(b => ({ file: `base-${b.build}.qcow2`, error: null, problems: [], actor: null, started: null, finished: null,
        step: null, commit: null, base_build: null, ...b })) }],
    builds_kept: 3, release: '1.0.0', disks_readable: true, ...extra,
  } as ImagesOverview
}

const buildEvent = (build: string, status: string, image = 'base') =>
  ({ image, build, status, step: status === 'running' ? 'build' : 'done', started: 't', finished: null, error: null, problems: [], actor: 'owner', unit: 'u' })
const entry = (code: string) => ({ time: 't', vm: null, actor: 'timer', kind: 'action', action: code, code, outcome: 'ok', description: '' })

/** A stand-in for the live connection: `emit` delivers an event to the subscribers. */
function fakeLive() {
  const listeners = new Map<string, Set<(data: any) => void>>()
  return {
    state: { stream: null as string | null, services: null as unknown },
    on(name: string, fn: (data: any) => void) {
      const set = listeners.get(name) ?? new Set()
      set.add(fn)
      listeners.set(name, set)
      return () => { set.delete(fn) }
    },
    emit(name: string, data: unknown) {
      for (const fn of listeners.get(name) ?? []) fn(data)
    },
  }
}

describe('the stale table for images (without a view)', () => {
  let live: ReturnType<typeof fakeLive>
  let r: Resources
  beforeEach(() => {
    live = fakeLive()
    r = createResources(live as never)
    r.client.setQueryData(['images'], overview([{ build: '1', status: 'ok' }]))
  })
  afterEach(() => r.stop())

  const data = () => r.client.getQueryData<ImagesOverview>(['images'])!
  const stale = () => r.client.getQueryState(['images'])!.isInvalidated

  it('writes a running Build into the cache and keeps it fresh', () => {
    live.emit('build', buildEvent('2', 'running'))
    expect(data().images[0]!.builds.map(b => [b.build, b.status])).toEqual([['2', 'running'], ['1', 'ok']])
    expect(data().images[0]!.running?.build).toBe('2')
    expect(stale()).toBe(false)
  })

  it('marks it stale when a Build ends, after writing it', () => {
    live.emit('build', buildEvent('2', 'failed'))
    expect(data().images[0]!.builds[0]).toMatchObject({ build: '2', status: 'failed' })
    expect(stale()).toBe(true)
  })

  it('keeps a stale mark through a later write', () => {
    live.emit('build', buildEvent('2', 'ok'))
    live.emit('build', buildEvent('3', 'running'))
    expect(data().images[0]!.builds.map(b => b.build)).toEqual(['3', '2', '1'])
    expect(stale()).toBe(true)
  })

  it('marks it stale when a Build of an Image it does not know starts', () => {
    live.emit('build', buildEvent('1', 'running', 'new'))
    expect(data().images.map(i => i.name)).toEqual(['base'])
    expect(stale()).toBe(true)
  })

  it('marks it stale when the version check ends, also one started by hand, not while it runs or stops', () => {
    const check = (state: string) => ({ units: [{ unit: 'agentbox-versions.service', state }], updating: false })
    live.emit('services', check('stopped'))
    live.emit('services', check('running'))
    live.emit('services', check('stopping'))
    expect(stale()).toBe(false)
    live.emit('services', check('stopped'))
    expect(stale()).toBe(true)
  })

  it('marks it stale when the update of every Image ends, not while it runs or stops', () => {
    live.emit('services', { units: [], updating: false })
    live.emit('services', { units: [], updating: true })
    live.emit('services', { units: [], updating: true })
    expect(stale()).toBe(false)
    live.emit('services', { units: [], updating: false })
    expect(stale()).toBe(true)
  })

  it('knows an update that ran before the cache was made', () => {
    r.stop()
    live = fakeLive()
    live.state.services = { units: [], updating: true }
    r = createResources(live as never)
    r.client.setQueryData(['images'], overview())
    live.emit('services', { units: [], updating: false })
    expect(stale()).toBe(true)
  })

  it.each(['maintenance.versions', 'update-now', 'recipe.set', 'recipe.restore', 'recipe.remove',
    'create', 'delete', 'set-image', 'reset', 'reset.automatic', 'reset.update'])('marks it stale on the Activity code %s', (code) => {
    live.emit('activity', entry(code))
    expect(stale()).toBe(true)
  })

  it('ignores other Activity codes', () => {
    // a Build's end comes as its `build` event
    for (const code of ['start', 'build.ok', 'build.failed', 'allowlist.add']) live.emit('activity', entry(code))
    live.emit('activity', null)
    expect(stale()).toBe(false)
  })

  it('a hello that could not resume marks everything stale, unless it is the first; one that resumed does not', () => {
    live.emit('hello', { stream: 'a', resumed: false })
    expect(stale()).toBe(false)
    live.emit('hello', { stream: 'a', resumed: true })
    expect(stale()).toBe(false)
    live.emit('hello', { stream: 'b', resumed: false })
    expect(stale()).toBe(true)
  })

  it('a hello after the cache was made is no first one when the stream was live before', () => {
    r.stop()
    live = fakeLive()
    live.state.stream = 'a'
    r = createResources(live as never)
    r.client.setQueryData(['images'], overview())
    live.emit('hello', { stream: 'b', resumed: false })
    expect(stale()).toBe(true)
  })
})

describe('reading a resource', () => {
  let live: ReturnType<typeof fakeLive>
  let r: Resources
  beforeEach(() => {
    live = fakeLive()
    r = createResources(live as never)
  })
  afterEach(() => r.stop())

  /** GET /images answers when `answer` is called. */
  function heldAnswer() {
    let answer!: (body: unknown) => void
    const api = fakeApi({ 'GET /images': () => new Promise(resolve => { answer = body => resolve(OK(body)) }) })
    return { api, answer: (body: unknown) => answer(body) }
  }

  it('applies a Build event that arrives while the read is on the way to its answer', async () => {
    const { answer } = heldAnswer()
    const read = r.client.fetchQuery({ queryKey: ['images'], queryFn: () => r.read('images') })
    await flushPromises()
    live.emit('build', buildEvent('2', 'running'))
    answer(overview([{ build: '1', status: 'ok' }]))      // the Host's answer from before the event
    const data = await read
    expect(data.images[0]!.builds.map(b => b.build)).toEqual(['2', '1'])
    expect(r.client.getQueryData<ImagesOverview>(['images'])!.images[0]!.running?.build).toBe('2')
  })

  it('reads once more after the read on the way when something made it stale meanwhile, once for a burst', async () => {
    const answers: ((body: unknown) => void)[] = []
    const api = fakeApi({ 'GET /images': () => new Promise(resolve => answers.push(body => resolve(OK(body)))) })
    const observer = new QueryObserver(r.client, { queryKey: ['images'], queryFn: () => r.read('images') })
    const off = observer.subscribe(() => {})        // a view shows it
    await flushPromises()
    expect(api.calls).toHaveLength(1)
    live.emit('activity', entry('maintenance.versions'))
    live.emit('activity', entry('update-now'))
    live.emit('build', buildEvent('2', 'ok'))
    await flushPromises()
    expect(api.calls).toHaveLength(1)                 // not while the first is on the way
    answers[0]!(overview([{ build: '1', status: 'ok' }]))
    await flushPromises()
    expect(api.calls).toHaveLength(2)
    answers[1]!(overview([{ build: '2', status: 'ok' }, { build: '1', status: 'ok' }]))
    await flushPromises()
    expect(api.calls).toHaveLength(2)
    expect(r.client.getQueryData<ImagesOverview>(['images'])!.images[0]!.builds.map(b => b.build)).toEqual(['2', '1'])
    off()
  })

  it('reads again with every hello, the first too, what could not reach the Control Room, and nothing else that failed', async () => {
    let answer: () => unknown = () => { throw new TypeError('Failed to fetch') }
    const api = fakeApi({ 'GET /images': () => answer() as never })
    const observer = new QueryObserver(r.client, { queryKey: ['images'], queryFn: () => r.read('images') })
    const off = observer.subscribe(() => {})
    await flushPromises()
    expect(r.client.getQueryState(['images'])!.status).toBe('error')
    answer = () => OK(overview())
    live.emit('hello', { stream: 'a', resumed: false })      // the first
    await flushPromises()
    expect(api.calls).toHaveLength(2)
    expect(r.client.getQueryState(['images'])!.status).toBe('success')

    // a failure of the Host: shown with Retry, not read again by a hello that resumed
    answer = () => ({ status: 502, body: { error: { code: 'agentbox_failed', message: 'x' } } })
    await observer.refetch()
    expect(api.calls).toHaveLength(3)
    live.emit('hello', { stream: 'a', resumed: true })
    await flushPromises()
    expect(api.calls).toHaveLength(3)
    off()
  })

  it('keeps a failed read\'s error through a later write, and reads it again when shown next', async () => {
    let answer: () => unknown = () => OK(overview([{ build: '1', status: 'ok' }]))
    const api = fakeApi({ 'GET /images': () => answer() as never })
    const observer = new QueryObserver(r.client, { queryKey: ['images'], queryFn: () => r.read('images') })
    let off = observer.subscribe(() => {})
    await flushPromises()
    answer = () => ({ status: 502, body: { error: { code: 'agentbox_failed', message: 'x' } } })
    await observer.refetch()
    live.emit('build', buildEvent('2', 'running'))
    const state = r.client.getQueryState<ImagesOverview>(['images'])!
    expect(state.status).toBe('error')
    expect(state.error).not.toBeNull()
    expect(state.data!.images[0]!.builds.map(b => b.build)).toEqual(['2', '1'])
    expect(observer.getCurrentResult().data!.images[0]!.builds.map(b => b.build)).toEqual(['2', '1'])

    // nobody shows it while it turns stale and an event is written: shown again, it is read
    off()
    answer = () => OK(overview([{ build: '3', status: 'ok' }]))
    live.emit('build', buildEvent('3', 'ok'))
    live.emit('build', buildEvent('4', 'running'))
    expect(api.calls).toHaveLength(2)
    off = observer.subscribe(() => {})
    await flushPromises()
    expect(api.calls).toHaveLength(3)
    off()
  })

  it('reads nothing by the clock or when the window gets the focus', async () => {
    vi.useFakeTimers()
    try {
      const api = fakeApi({ 'GET /images': OK(overview()) })
      expect(r.client.getDefaultOptions().queries).toMatchObject({ staleTime: Infinity, retry: false,
        refetchOnWindowFocus: false, refetchOnReconnect: false, refetchInterval: false })
      const observer = new QueryObserver(r.client, { queryKey: ['images'], queryFn: () => r.read('images') })
      const off = observer.subscribe(() => {})
      await vi.advanceTimersByTimeAsync(0)
      expect(api.calls).toHaveLength(1)
      window.dispatchEvent(new Event('focus'))
      document.dispatchEvent(new Event('visibilitychange'))
      window.dispatchEvent(new Event('online'))
      await vi.advanceTimersByTimeAsync(3 * 60 * 60 * 1000)
      expect(api.calls).toHaveLength(1)
      off()
    }
    finally {
      vi.useRealTimers()
    }
  })
})

describe('a resource with parameters (a VM\'s)', () => {
  let live: ReturnType<typeof fakeLive>
  let r: Resources
  beforeEach(() => {
    live = fakeLive()
    r = createResources(live as never)
    for (const vm of ['work', 'priv']) r.client.setQueryData(['pairings', { vm }], { vm, devices: [], observer_known: true, single_removal: false })
  })
  afterEach(() => r.stop())

  const stale = (vm: string) => r.client.getQueryState(['pairings', { vm }])!.isInvalidated

  it.each(['observe', 'unpair', 'revoke-pairings'])('%s marks only the entry\'s VM\'s pairings stale', (code) => {
    live.emit('activity', { ...entry(code), vm: 'work' })
    expect([stale('work'), stale('priv')]).toEqual([true, false])
  })

  it('an entry without a VM marks every VM\'s', () => {
    live.emit('activity', { ...entry('observe'), vm: null })
    expect([stale('work'), stale('priv')]).toEqual([true, true])
  })

  it('reads its own path from its parameters', async () => {
    const api = fakeApi({ 'GET /vms/priv/pairings': OK({ vm: 'priv', devices: [] }) })
    await r.read('pairings', { vm: 'priv' })
    expect(api.calls.map(c => c.path)).toEqual(['/vms/priv/pairings'])
  })
})

describe('a resource with parameters in a view', () => {
  beforeEach(async () => {
    const { setUp } = await import('./vm-helpers')
    await setUp()
  })
  afterEach(async () => {
    const { tearDown } = await import('./vm-helpers')
    tearDown()
  })

  it('reads the copy of the new parameters when they change', async () => {
    const { mount } = await import('@vue/test-utils')
    const { defineComponent, h, ref } = await import('vue')
    const { useResource } = await import('~/utils/resources')
    const api = fakeApi({
      'GET /vms/work/pairings': OK({ vm: 'work', devices: [] }),
      'GET /vms/priv/pairings': OK({ vm: 'priv', devices: [] }),
    })
    const vm = ref('work')
    const view = mount(defineComponent({
      setup() {
        const pairings = useResource('pairings', { params: () => ({ vm: vm.value }) })
        return () => h('p', pairings.data.value?.vm ?? '')
      },
    }))
    await flushPromises()
    expect(view.text()).toBe('work')
    vm.value = 'priv'
    await flushPromises()
    expect(view.text()).toBe('priv')
    expect(api.calls.map(c => c.path)).toEqual(['/vms/work/pairings', '/vms/priv/pairings'])
    view.unmount()
  })
})

describe('keepPrevious', () => {
  beforeEach(async () => {
    const { setUp } = await import('./vm-helpers')
    await setUp()
  })
  afterEach(async () => {
    const { tearDown } = await import('./vm-helpers')
    tearDown()
  })

  it('shows the copy of the parameters before while the new one is read', async () => {
    const { mount } = await import('@vue/test-utils')
    const { defineComponent, h, ref } = await import('vue')
    const { useResource } = await import('~/utils/resources')
    let release!: () => void
    fakeApi({
      'GET /vms/work/github-tokens?since=-7d': OK({ repositories: [{ repository: 'a/b' }], requests: [] }),
      'GET /vms/work/github-tokens?since=-24h': () => new Promise(resolve => { release = () => resolve(OK({ repositories: [], requests: [] })) }),
    })
    const since = ref('-7d')
    const view = mount(defineComponent({
      setup() {
        const tokens = useResource('githubTokens', { params: () => ({ vm: 'work', since: since.value }), keepPrevious: true })
        return () => h('p', String(tokens.data.value?.repositories.length ?? 'none'))
      },
    }))
    await flushPromises()
    expect(view.text()).toBe('1')
    since.value = '-24h'
    await flushPromises()
    expect(view.text()).toBe('1')
    release()
    await flushPromises()
    expect(view.text()).toBe('0')
    view.unmount()
  })
})

describe('a VM\'s copy marked stale while it is read', () => {
  it('is read once more after that read', async () => {
    const live = fakeLive()
    const r = createResources(live as never)
    const answers: (() => void)[] = []
    const api = fakeApi({ 'GET /vms/work/pairings': () => new Promise(resolve => answers.push(() => resolve(OK({ vm: 'work', devices: [] })))) })
    const observer = new QueryObserver(r.client, { queryKey: ['pairings', { vm: 'work' }], queryFn: () => r.read('pairings', { vm: 'work' }) })
    const off = observer.subscribe(() => {})
    await flushPromises()
    live.emit('activity', { ...entry('observe'), vm: 'work' })
    answers[0]!()
    await flushPromises()
    expect(api.calls).toHaveLength(2)
    answers[1]!()
    await flushPromises()
    off()
    r.stop()
  })
})

describe('the stale table for the GitHub Apps', () => {
  it.each(['set-github-app', 'remove-github-app', 'set-owners', 'create', 'delete'])('%s marks githubApp stale', (code) => {
    const live = fakeLive()
    const r = createResources(live as never)
    r.client.setQueryData(['githubApp'], { apps: [], missing: [] })
    live.emit('activity', entry(code))
    expect(r.client.getQueryState(['githubApp'])!.isInvalidated).toBe(true)
    r.stop()
  })
})

describe('a code the Host answers for a state', () => {
  it('is read as the declared data, not as a failure', async () => {
    const r = createResources(fakeLive() as never)
    fakeApi({ 'GET /github-app': { status: 409, body: { error: { code: 'github_app_missing', message: 'x' } } } })
    expect(await r.read('githubApp')).toEqual({ apps: [], missing: [], none: true })
    fakeApi({ 'GET /github-app': { status: 502, body: { error: { code: 'github_unavailable', message: 'x' } } } })
    await expect(r.read('githubApp')).rejects.toMatchObject({ code: 'github_unavailable' })
    r.stop()
  })
})

describe('a failure code named like a built-in property', () => {
  it('stays a failure', async () => {
    const r = createResources(fakeLive() as never)
    fakeApi({ 'GET /github-app': { status: 500, body: { error: { code: 'constructor', message: 'x' } } } })
    await expect(r.read('githubApp')).rejects.toMatchObject({ code: 'constructor' })
    r.stop()
  })
})

describe('a VM\'s GitHub tokens and connection help', () => {
  it('a github-token event marks only its VM\'s tokens stale, whatever their period', () => {
    const live = fakeLive()
    const r = createResources(live as never)
    for (const vm of ['work', 'priv']) r.client.setQueryData(['githubTokens', { vm, since: '-7d' }], { repositories: [], requests: [] })
    live.emit('github-token', { vm: 'work', action: 'issued' })
    const stale = (vm: string) => r.client.getQueryState(['githubTokens', { vm, since: '-7d' }])!.isInvalidated
    expect([stale('work'), stale('priv')]).toEqual([true, false])
    r.stop()
  })

  it('a VM\'s event that turns SSH access on or off, or brings a new host key, marks only its help stale', () => {
    const live = fakeLive()
    const vmOf = (name: string, enabled: boolean, key: string | null = null) => ({ name, state: 'running', ssh: { enabled, port: 2201, host_key_fingerprint: key, keys: [], pending: false } })
    ;(live.state as Record<string, unknown>).vms = [vmOf('work', false), vmOf('priv', false)]
    const r = createResources(live as never)
    for (const vm of ['work', 'priv']) r.client.setQueryData(['sshConfig', { vm }], { mode: 'local', hosts: [] })
    const stale = (vm: string) => r.client.getQueryState(['sshConfig', { vm }])!.isInvalidated
    live.emit('vm', { ...vmOf('work', false), state: 'paused' })          // nothing of SSH changed
    expect([stale('work'), stale('priv')]).toEqual([false, false])
    live.emit('vm', vmOf('priv', true))
    expect([stale('work'), stale('priv')]).toEqual([false, true])
    // a `state` event tells the VMs anew, and what it changes counts as their own events would
    r.client.setQueryData(['sshConfig', { vm: 'priv' }], { mode: 'local', hosts: [] })
    live.emit('state', { vms: [vmOf('work', true), vmOf('priv', true)], problems: [] })
    expect([stale('work'), stale('priv')]).toEqual([true, false])
    r.client.setQueryData(['sshConfig', { vm: 'work' }], { mode: 'local', hosts: [] })
    live.emit('vm', vmOf('work', true, 'SHA256:x'))
    expect(stale('work')).toBe(true)
    r.stop()
  })
})

describe('an Image\'s Containerfile', () => {
  let live: ReturnType<typeof fakeLive>
  let r: Resources
  beforeEach(() => {
    live = fakeLive()
    r = createResources(live as never)
    for (const image of ['base', 'rust']) r.client.setQueryData(['containerfile', { image }], { image, path: 'p', release: null, url: null, text: '', size: 0, truncated: false })
  })
  afterEach(() => r.stop())

  const stale = (image: string) => r.client.getQueryState(['containerfile', { image }])!.isInvalidated

  it.each(['recipe.set', 'recipe.restore', 'recipe.remove'])('%s marks only the Recipe\'s Image\'s Containerfile stale', (code) => {
    live.emit('activity', { ...entry(code), params: { name: 'rust' } })
    expect([stale('base'), stale('rust')]).toEqual([false, true])
  })

  it('an entry without a name marks every Image\'s', () => {
    live.emit('activity', { ...entry('recipe.set'), params: {} })
    expect([stale('base'), stale('rust')]).toEqual([true, true])
  })

  it('ignores other codes of the Image', () => {
    live.emit('activity', { ...entry('set-image'), params: { name: 'rust' } })
    expect([stale('base'), stale('rust')]).toEqual([false, false])
  })

  it('reads its own path, the name encoded', async () => {
    const api = fakeApi({ 'GET /images/a%20b/containerfile': OK({ image: 'a b' }) })
    await r.read('containerfile', { image: 'a b' })
    expect(api.calls.map(c => c.path)).toEqual(['/images/a%20b/containerfile'])
  })
})

describe('the Host\'s readings', () => {
  let live: ReturnType<typeof fakeLive>
  let r: Resources
  beforeEach(() => {
    live = fakeLive()
    r = createResources(live as never)
  })
  afterEach(() => r.stop())

  const point = (time: number) => ({ time, values: { 'cpu.load': time / 100 }, vms: {} })
  const snapshot = (times: number[]) => ({ interval: 5, history_seconds: 60, thresholds: {}, time: times.at(-1) ?? null, readings: [], vms: {}, history: times.map(point) })
  const sample = (time: number | null) => ({ time, readings: [{ id: 'cpu.load', kind: 'cpu_load', value: 1 }], vms: {} })
  const history = () => r.client.getQueryData<{ history: { time: number }[] }>(['host'])!.history.map(p => p.time)

  it('appends each sample to the history, within its window, and skips a repeated one', () => {
    r.client.setQueryData(['host'], snapshot([940, 970, 1000]))
    live.emit('host', sample(1005))
    expect(history()).toEqual([970, 1000, 1005])
    const before = r.client.getQueryData(['host'])
    live.emit('host', sample(1005))
    live.emit('host', sample(null))
    expect(r.client.getQueryData(['host'])).toBe(before)
    expect(r.client.getQueryState(['host'])!.isInvalidated).toBe(false)
  })

  it('appends a sample that arrives while the history is read to its answer', async () => {
    let answer!: () => void
    fakeApi({ 'GET /host': () => new Promise(resolve => { answer = () => resolve(OK(snapshot([940, 970, 1000]))) }) })
    const observer = new QueryObserver(r.client, { queryKey: ['host'], queryFn: () => r.read('host') })
    const off = observer.subscribe(() => {})
    await flushPromises()
    live.emit('host', sample(1005))
    answer()
    await flushPromises()
    expect(history()).toEqual([970, 1000, 1005])
    off()
  })

  it('reads a Host without a sampler as a state', async () => {
    fakeApi({ 'GET /host': { status: 503, body: { error: { code: 'readings_unavailable', message: 'x' } } } })
    expect(await r.read('host')).toMatchObject({ unavailable: true, readings: [], history: [] })
  })
})

describe('a Build\'s log', () => {
  let live: ReturnType<typeof fakeLive>
  let r: Resources
  const line = (n: number) => ({ time: 't', message: `line ${n}`, cursor: `c${n}` })
  const log = (ns: number[]) => ({ lines: ns.map(line), cursor: ns.length ? `c${ns.at(-1)}` : null })
  const event = (build: string, ns: number[], truncated = false) =>
    ({ unit: 'u', target: 'ml', image: 'ml', build, entries: ns.map(line), cursor: `c${ns.at(-1)}`, truncated })
  const key = (build: string) => ['buildLog', { image: 'ml', build }]
  const lines = (build: string) => r.client.getQueryData<{ lines: { cursor: string }[] }>(key(build))!.lines.map(l => l.cursor)
  const stale = (build: string) => r.client.getQueryState(key(build))!.isInvalidated
  beforeEach(() => {
    live = fakeLive()
    r = createResources(live as never)
    r.client.setQueryData(key('r1'), log([1, 2]))
    r.client.setQueryData(key('r2'), log([1]))
  })
  afterEach(() => r.stop())

  it('appends the lines of a build-log event to its Build\'s log only', () => {
    live.emit('build-log', event('r1', [2, 3]))
    expect([lines('r1'), lines('r2')]).toEqual([['c1', 'c2', 'c3'], ['c1']])
    expect([stale('r1'), stale('r2')]).toEqual([false, false])
  })

  it('after lines were skipped keeps no later lines and reads after the held cursor', () => {
    live.emit('build-log', event('r1', [5], true))
    live.emit('build-log', event('r1', [6]))
    expect(lines('r1')).toEqual(['c1', 'c2'])
    expect([stale('r1'), stale('r2')]).toEqual([true, false])
  })

  it('marks the log of a Build that ended stale, not that of another', () => {
    live.emit('build', { image: 'ml', build: 'r2', status: 'ok' })
    expect([stale('r1'), stale('r2')]).toEqual([false, true])
  })

  it('reads on after the held cursor and joins the answer to the held lines', async () => {
    const api = fakeApi({ 'GET /images/ml/log?build=r1&lines=500&after_cursor=c2': OK({ entries: [line(3)], cursor: 'c3', status: 'ok' }) })
    expect((await r.read('buildLog', { image: 'ml', build: 'r1' })).lines.map(l => l.cursor)).toEqual(['c1', 'c2', 'c3'])
    expect(api.calls).toHaveLength(1)
  })

  it('an event of another Build\'s log is not applied to the answer of this one', async () => {
    let answer!: () => void
    fakeApi({ 'GET /images/ml/log?build=r3&lines=500': () => new Promise(resolve => { answer = () => resolve(OK({ entries: [line(1)], cursor: 'c1' })) }) })
    const observer = new QueryObserver(r.client, { queryKey: key('r3'), queryFn: () => r.read('buildLog', { image: 'ml', build: 'r3' }) })
    const off = observer.subscribe(() => {})
    await flushPromises()
    live.emit('build-log', event('r1', [3]))
    live.emit('build-log', event('r3', [2]))
    answer()
    await flushPromises()
    expect(lines('r3')).toEqual(['c1', 'c2'])
    off()
  })
})
