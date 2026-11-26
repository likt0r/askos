// The resources module (ADR 0026, #214): the stale table's rows for `images` without a view, an
// event during a read, `hello`, no reading by clock or focus; then through the app: one shared
// copy for every reader, and where a failed read shows.
import { QueryObserver } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { createResources, type Resources, useResource } from '~/utils/resources'
import type { ImagesOverview } from '~/utils/images'
import { fakeApi, SIGNED_IN, vm } from './fakes'
import { byTestId, click, exists, openApp, setUp, tearDown } from './vm-helpers'

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

  it('marks it stale when a Build of an Image it does not know starts', () => {
    live.emit('build', buildEvent('1', 'running', 'new'))
    expect(data().images.map(i => i.name)).toEqual(['base'])
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

  it.each(['maintenance.versions', 'build.ok', 'update-now'])('marks it stale on the Activity code %s', (code) => {
    live.emit('activity', entry(code))
    expect(stale()).toBe(true)
  })

  it('ignores other Activity codes', () => {
    for (const code of ['start', 'build.failed', 'allowlist.add']) live.emit('activity', entry(code))
    live.emit('activity', null)
    expect(stale()).toBe(false)
  })

  it('a hello that could not resume marks everything stale, unless it is the first; one that resumed does not', () => {
    const missed = vi.fn()
    r.onMissedEvents(missed)
    live.emit('hello', { stream: 'a', resumed: false })
    expect(stale()).toBe(false)
    live.emit('hello', { stream: 'a', resumed: true })
    expect(stale()).toBe(false)
    expect(missed).not.toHaveBeenCalled()
    live.emit('hello', { stream: 'b', resumed: false })
    expect(stale()).toBe(true)
    expect(missed).toHaveBeenCalledOnce()
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
    live.emit('activity', entry('build.ok'))
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

  it('reads nothing by the clock or when the window gets the focus', async () => {
    vi.useFakeTimers()
    try {
      const api = fakeApi({ 'GET /images': OK(overview()) })
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
