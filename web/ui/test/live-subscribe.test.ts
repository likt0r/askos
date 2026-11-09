// Views subscribe to stream events that carry no state of their own (activity, build,
// notification, ...) with live.on().
import { describe, expect, it } from 'vitest'
import { createLive } from '~/utils/live'
import { FakeEventSource } from './fakes'

function setup() {
  FakeEventSource.reset()
  const live = createLive({
    checkSession: async () => true,
    fetchVms: async () => ({ vms: [], problems: [] }),
    createEventSource: url => new FakeEventSource(url),
  })
  live.start()
  return { live, es: FakeEventSource.last }
}

describe('live.on', () => {
  it('hands whole events to subscribers until they unsubscribe', () => {
    const { live, es } = setup()
    const seen: unknown[] = []
    const off = live.on('activity', e => seen.push(e))
    es.emit('activity', { kind: 'action', description: 'one' })
    es.emit('activity', { kind: 'action', description: 'two' })
    off()
    es.emit('activity', { kind: 'action', description: 'three' })
    expect(seen).toEqual([{ kind: 'action', description: 'one' }, { kind: 'action', description: 'two' }])
  })

  it('also works for events that update state, after the state changed', () => {
    const { live, es } = setup()
    const seen: unknown[] = []
    live.on('host', () => seen.push(live.state.host))
    es.emit('host', { time: 1, readings: [], vms: {} })
    expect(seen).toEqual([{ time: 1, readings: [], vms: {} }])
  })

  it('does not let a failing subscriber stop the others or the stream', () => {
    const { live, es } = setup()
    const seen: unknown[] = []
    live.on('notification', () => { throw new Error('bug') })
    live.on('notification', n => seen.push(n))
    es.emit('notification', { kind: 'alert' })
    es.emit('hello', { stream: 'x', resumed: false })
    expect(seen).toEqual([{ kind: 'alert' }])
    expect(live.state.connection).toBe('live')
  })

  it('ignores events of a replaced connection', () => {
    const { live, es } = setup()
    const seen: unknown[] = []
    live.on('build', e => seen.push(e))
    live.stop()
    es.emit('build', { image: 'base' })
    expect(seen).toEqual([])
  })
})
