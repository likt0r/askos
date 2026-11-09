// The live connection (app/utils/live.ts): snapshot then updates, and getting back on its
// feet after the stream drops or the backend refuses it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLive } from '~/utils/live'
import { FakeEventSource, vm } from './fakes'

function setup(overrides: Partial<Parameters<typeof createLive>[0]> = {}) {
  let clock = 0
  const checkSession = vi.fn(async () => true)
  const fetchVms = vi.fn(async () => ({ vms: [vm('work', 'paused'), vm('priv', 'running', { network: 2 })], problems: [] }))
  const onSignedOut = vi.fn()
  const live = createLive({
    checkSession, fetchVms, onSignedOut,
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
  return { live, checkSession, fetchVms, onSignedOut, advance }
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeEventSource.reset()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('live state', () => {
  it('takes the VM list from the state snapshot and replaces single VMs from vm events', () => {
    const { live } = setup()
    live.start()
    expect(FakeEventSource.last.url).toBe('/api/v1/events')
    expect(live.state.connection).toBe('connecting')
    expect(live.state.loaded).toBe(false)

    const es = FakeEventSource.last
    es.emit('hello', { stream: 'ab12cd34', resumed: false })
    expect(live.state.connection).toBe('live')
    es.emit('watch', { running: true, since: '2026-11-03T12:00:00Z', restarts: 0, last_exit: null })
    es.emit('state', { vms: [vm('priv', 'stopped', { network: 2 }), vm('work')], problems: [{ vm: 'old', problem: 'broken record' }] }, 'ab12cd34-1')
    expect(live.state.loaded).toBe(true)
    expect(live.state.vms.map(v => [v.name, v.state])).toEqual([['work', 'running'], ['priv', 'stopped']])
    expect(live.state.problems).toEqual([{ vm: 'old', problem: 'broken record' }])

    es.emit('vm', vm('priv', 'running', { network: 2, agents: { working: 1 } }), 'ab12cd34-2')
    expect(live.state.vms.map(v => [v.name, v.state])).toEqual([['work', 'running'], ['priv', 'running']])
    expect(live.state.vms[1]!.agents).toEqual({ working: 1 })

    // a VM the list did not have yet goes in network order; events are whole objects, twice is harmless
    es.emit('vm', vm('lab', 'stopped', { network: 3 }))
    es.emit('vm', vm('lab', 'stopped', { network: 3 }))
    expect(live.state.vms.map(v => v.name)).toEqual(['work', 'priv', 'lab'])

    // a new snapshot (a VM was deleted) replaces everything
    es.emit('state', { vms: [vm('work')], problems: [] })
    expect(live.state.vms.map(v => v.name)).toEqual(['work'])
    expect(live.state.problems).toEqual([])
  })

  it('ignores unknown events and broken data', () => {
    const { live } = setup()
    live.start()
    const es = FakeEventSource.last
    es.emit('hello', { stream: 'x', resumed: false })
    es.emit('state', { vms: [vm('work')], problems: [] })
    es.emit('something-new', { a: 1 })
    es.emit('vm', { no: 'name' })
    expect(live.state.vms.map(v => v.name)).toEqual(['work'])
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
    first.emit('state', { vms: [vm('stale')], problems: [] })
    expect(live.state.vms).toEqual([])
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

  it('fetches the VM list again when agentbox watch runs again', async () => {
    const { live, fetchVms } = setup()
    live.start()
    const es = FakeEventSource.last
    es.emit('hello', { stream: 'x', resumed: false })
    es.emit('watch', { running: true, since: 't', restarts: 0, last_exit: null })
    es.emit('state', { vms: [vm('work')], problems: [] })
    expect(fetchVms).not.toHaveBeenCalled()
    es.emit('watch', { running: false, since: null, restarts: 1, last_exit: 1 })
    expect(live.state.watch?.running).toBe(false)
    es.emit('watch', { running: true, since: 't2', restarts: 1, last_exit: 1 })
    expect(fetchVms).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(0)
    expect(live.state.vms.map(v => [v.name, v.state])).toEqual([['work', 'paused'], ['priv', 'running']])
  })

  it('forgets everything on reset (sign-out)', () => {
    const { live } = setup()
    live.start()
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })
    FakeEventSource.last.emit('state', { vms: [vm('work')], problems: [] })
    live.reset()
    expect(FakeEventSource.last.closed).toBe(true)
    expect(live.state).toMatchObject({ connection: 'idle', loaded: false, vms: [] })
  })
})
