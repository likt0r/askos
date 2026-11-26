// useReloadOnActivity: a view loads again when an Activity entry that changes its data
// arrives, and after a reconnect that could not resume.
import { effectScope, type EffectScope } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetLiveForTests, useLive } from '~/composables/useLive'
import { useReloadOnActivity, type ReloadOptions } from '~/composables/useReloadOnActivity'
import { FakeEventSource } from './fakes'

const entry = (fields: Record<string, unknown>) => ({ time: '2026-11-03T12:00:00Z', vm: 'work', actor: 'root', outcome: 'ok', description: '', ...fields })

let scope: EffectScope | null = null

beforeEach(() => {
  vi.useFakeTimers()
  resetLiveForTests()
  FakeEventSource.reset()
  vi.stubGlobal('EventSource', FakeEventSource)
})
afterEach(() => {
  scope?.stop()
  scope = null
  resetLiveForTests()
  vi.useRealTimers()
})

/** A view with its own load, created while the stream is connecting (or live already). */
function view(options: ReloadOptions, liveAlready = false) {
  const live = useLive()
  live.start()
  const es = FakeEventSource.last
  if (liveAlready) es.emit('hello', { stream: 'a', resumed: false })
  const load = vi.fn()
  scope = effectScope()
  scope.run(() => useReloadOnActivity(load, options))
  return { es, load }
}

describe('useReloadOnActivity', () => {
  it('loads on a listed code and on a listed kind, not on others', async () => {
    const { es, load } = view({ codes: ['watch-add'], kinds: ['allowlist'] })
    es.emit('activity', entry({ kind: 'watchlist', action: 'watch-add', code: 'watch-add' }))
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(1)
    es.emit('activity', entry({ kind: 'allowlist', action: 'allow', code: 'allow' }))
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(2)
    es.emit('activity', entry({ kind: 'action', action: 'start', code: 'start' }))
    es.emit('activity', entry({ kind: 'watchlist', action: 'watch-remove', code: 'watch-remove' }))
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('counts entries of both outcomes', async () => {
    const { es, load } = view({ kinds: ['allowlist'] })
    es.emit('activity', entry({ kind: 'allowlist', action: 'allow', outcome: 'failed' }))
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('reads the action when an entry has no code', async () => {
    const { es, load } = view({ codes: ['delete'] })
    es.emit('activity', entry({ kind: 'action', action: 'delete' }))
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('asks match about the entry', async () => {
    const { es, load } = view({ codes: ['observe'], match: e => e.vm === 'work' })
    es.emit('activity', entry({ kind: 'pairing', action: 'observe', vm: 'priv' }))
    await vi.advanceTimersByTimeAsync(400)
    expect(load).not.toHaveBeenCalled()
    es.emit('activity', entry({ kind: 'pairing', action: 'observe', vm: 'work' }))
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('gathers a burst into one load', async () => {
    const { es, load } = view({ kinds: ['allowlist'] })
    for (let i = 0; i < 5; i++) {
      es.emit('activity', entry({ kind: 'allowlist', action: 'allow' }))
      await vi.advanceTimersByTimeAsync(100)
    }
    expect(load).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('loads on a hello that could not resume, but not on the first', async () => {
    const { es, load } = view({ kinds: ['allowlist'] })
    es.emit('hello', { stream: 'a', resumed: false })
    await vi.advanceTimersByTimeAsync(400)
    expect(load).not.toHaveBeenCalled()
    es.emit('hello', { stream: 'a', resumed: true })
    await vi.advanceTimersByTimeAsync(400)
    expect(load).not.toHaveBeenCalled()
    es.emit('hello', { stream: 'b', resumed: false })
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('counts the next hello as a later one when the view was created on a live stream', async () => {
    const { es, load } = view({ kinds: ['allowlist'] }, true)
    es.emit('hello', { stream: 'b', resumed: false })
    await vi.advanceTimersByTimeAsync(400)
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('stops with its scope', async () => {
    const { es, load } = view({ kinds: ['allowlist'] }, true)
    es.emit('activity', entry({ kind: 'allowlist', action: 'allow' }))
    scope!.stop()
    es.emit('activity', entry({ kind: 'allowlist', action: 'allow' }))
    es.emit('hello', { stream: 'b', resumed: false })
    await vi.advanceTimersByTimeAsync(400)
    expect(load).not.toHaveBeenCalled()
  })
})
