// The live connection (app/utils/live.ts), only the connection: opening the stream, handing its
// events on, and getting back on its feet after it drops or the backend refuses it. What the
// events carry is the resources module's (resources.test.ts).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLive } from '~/utils/live'
import { FakeEventSource, vm } from './fakes'

function setup(overrides: Partial<Parameters<typeof createLive>[0]> = {}) {
  let clock = 0
  const checkSession = vi.fn(async () => true)
  const beforeStart = vi.fn()
  const onSignedOut = vi.fn()
  const live = createLive({
    checkSession, beforeStart, onSignedOut,
    createEventSource: url => new FakeEventSource(url),
    backoffMs: [1000, 3000, 10000],
    deadAfterMs: 30000,
    now: () => clock,
    ...overrides,
  })
  const advance = async (ms: number) => {
    clock += ms
    await vi.advanceTimersByTimeAsync(ms)
  }
  return { live, checkSession, beforeStart, onSignedOut, advance }
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeEventSource.reset()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('the stream', () => {
  it('opens once what subscribes to it is there, goes live on hello and hands every event on', () => {
    const { live, beforeStart } = setup()
    const seen: string[] = []
    live.on('state', () => seen.push('state'))
    live.on('vm', () => seen.push('vm'))
    beforeStart.mockImplementation(() => seen.push('before'))
    live.start()
    expect(seen).toEqual(['before'])
    expect(FakeEventSource.last.url).toBe('/api/v1/events')
    expect(live.state.connection).toBe('connecting')
    const es = FakeEventSource.last
    es.emit('hello', { stream: 'ab12cd34', resumed: false })
    expect(live.state).toMatchObject({ connection: 'live', stream: 'ab12cd34' })
    es.emit('state', { vms: [vm('work')], problems: [] })
    es.emit('vm', vm('work'))
    expect(seen).toEqual(['before', 'state', 'vm'])
  })

  it('hands on no unknown event and no data that is not JSON', () => {
    const { live } = setup()
    const seen: unknown[] = []
    live.on('something-new', e => seen.push(e))
    live.on('vm', e => seen.push(e))
    live.start()
    const es = FakeEventSource.last
    es.emit('something-new', { a: 1 })
    for (const listener of (es as unknown as { listeners: Map<string, ((e: MessageEvent) => void)[]> }).listeners.get('vm') ?? []) {
      listener(new MessageEvent('vm', { data: 'not json' }))
    }
    expect(seen).toEqual([])
  })
})

describe('reconnecting', () => {
  it('shows a dropped connection while the browser retries by itself', () => {
    const { live, checkSession } = setup()
    live.start()
    const es = FakeEventSource.last
    es.emit('hello', { stream: 'x', resumed: false })
    es.drop()
    expect(live.state.connection).toBe('reconnecting')
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(checkSession).not.toHaveBeenCalled()
    es.emit('hello', { stream: 'x', resumed: true })
    expect(live.state.connection).toBe('live')
  })

  it('opens a new EventSource after an HTTP error, waiting longer each time', async () => {
    const { live, checkSession, advance } = setup()
    live.start()
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })

    FakeEventSource.last.fail()        // e.g. 503 too_many_streams: EventSource gives up
    expect(FakeEventSource.instances[0]!.closed).toBe(true)
    await vi.advanceTimersByTimeAsync(0)
    expect(checkSession).toHaveBeenCalledTimes(1)
    expect(live.state.connection).toBe('reconnecting')
    expect(live.state.retryAt).toBe(1000)

    await advance(999)
    expect(FakeEventSource.instances).toHaveLength(1)
    await advance(1)
    expect(FakeEventSource.instances).toHaveLength(2)

    FakeEventSource.last.fail()
    await advance(0)
    await advance(2999)
    expect(FakeEventSource.instances).toHaveLength(2)
    await advance(1)
    expect(FakeEventSource.instances).toHaveLength(3)

    // connected again: the next failure starts with the short delay
    FakeEventSource.last.emit('hello', { stream: 'y', resumed: false })
    expect(live.state.connection).toBe('live')
    expect(live.state.attempts).toBe(0)
    FakeEventSource.last.fail()
    await advance(1000)
    expect(FakeEventSource.instances).toHaveLength(4)
  })

  it('keeps trying while the backend cannot be reached', async () => {
    const { live, advance } = setup({ checkSession: vi.fn(async () => { throw new Error('offline') }) })
    live.start()
    FakeEventSource.last.fail()
    await advance(1000)
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(live.state.connection).toBe('reconnecting')
  })

  it('stops and reports a session that ended (401)', async () => {
    const { live, onSignedOut, advance } = setup({ checkSession: vi.fn(async () => false) })
    live.start()
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })
    FakeEventSource.last.fail()
    await advance(0)
    expect(live.state.connection).toBe('signed-out')
    expect(onSignedOut).toHaveBeenCalledTimes(1)
    await advance(60000)
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('retries at once when asked, and ignores the replaced EventSource', async () => {
    const { live, advance } = setup()
    live.start()
    const first = FakeEventSource.last
    first.fail()
    await advance(0)
    live.reconnectNow()
    expect(FakeEventSource.instances).toHaveLength(2)
    const seen: unknown[] = []
    live.on('state', e => seen.push(e))
    first.emit('state', { vms: [vm('stale')], problems: [] })
    expect(seen).toEqual([])
    await advance(5000)
    expect(FakeEventSource.instances).toHaveLength(2)    // the scheduled retry was cancelled
  })

  it('treats a stream without events for 30 s as dead once it has seen host readings', async () => {
    const { live, advance } = setup()
    live.start()
    const es = FakeEventSource.last
    es.emit('hello', { stream: 'x', resumed: false })
    await advance(60000)
    expect(es.closed).toBe(false)      // no readings: quiet is fine

    es.emit('host', { time: 't', readings: [], vms: {} })
    await advance(25000)
    es.emit('host', { time: 't', readings: [], vms: {} })
    await advance(25000)
    expect(es.closed).toBe(false)
    await advance(10000)
    expect(es.closed).toBe(true)
    expect(live.state.connection).toBe('reconnecting')
    await advance(1000)
    expect(FakeEventSource.instances).toHaveLength(2)
  })

  it('forgets everything on reset (sign-out)', () => {
    const { live } = setup()
    live.start()
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })
    live.reset()
    expect(FakeEventSource.last.closed).toBe(true)
    expect(live.state).toMatchObject({ connection: 'idle', stream: null, attempts: 0 })
  })
})
