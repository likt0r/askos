// Subscribers take the stream's events with live.on(): those that carry no state of their own
// (activity, build, notification, ...) and the pushed objects (the resources module's).
import { describe, expect, it } from 'vitest'
import { createLive } from '~/utils/live'
import { FakeEventSource } from './fakes'

function setup() {
  FakeEventSource.reset()
  const live = createLive({
    checkSession: async () => true,
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

  it('hands the pushed objects whole to subscribers, and keeps none itself', () => {
    const { live, es } = setup()
    const seen: unknown[] = []
    for (const name of ['state', 'vm', 'host', 'services', 'version', 'watch', 'host-reboot']) live.on(name, e => seen.push([name, e]))
    es.emit('host', { time: 1, readings: [], vms: {} })
    es.emit('state', { vms: [], problems: [] })
    es.emit('host-reboot', { needed: false })
    expect(seen).toEqual([['host', { time: 1, readings: [], vms: {} }], ['state', { vms: [], problems: [] }], ['host-reboot', { needed: false }]])
    expect(Object.keys(live.state).sort()).toEqual(['attempts', 'connection', 'retryAt', 'stream'])
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
