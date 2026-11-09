// The host bar (#16): key readings and memory per VM in one line, readings over their
// threshold highlighted also when collapsed, expanded tiles with the 30-minute history, the
// collapsed state remembered in this browser.
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { appendSample, barReadings, hostSeries, sortReadings, sparkPoints, vmMemory } from '~/utils/host'
import { hostSample, ok, openPage, reading, useFreshApp } from './system-helpers'

const GIB = 1024 ** 3

describe('host readings (units)', () => {
  const readings = [
    reading('cpu.load', 'cpu_load', 12), reading('cpu.temp', 'cpu_temp', 55), reading('memory', 'memory', 40),
    reading('gpu0.temp', 'gpu_temp', 40), reading('nvme0n1.temp', 'nvme_temp', 75), reading('nvme1n1.temp', 'nvme_temp', 40),
    reading('gpu0.power', 'gpu_power', 30), reading('cpu.power', 'cpu_power', null),
  ]

  it('shows the key readings, and any other reading that is over its threshold', () => {
    expect(barReadings(readings as never).map(r => r.id)).toEqual(['cpu.load', 'cpu.temp', 'memory', 'gpu0.temp', 'nvme0n1.temp'])
  })

  it('shows the swap flow next to the swap use and flags it over its threshold', () => {
    const swap = [reading('swap_activity', 'swap_activity', 12), reading('gpu0.temp', 'gpu_temp', 40), reading('swap', 'swap', 5)]
    expect(sortReadings(swap as never).map(r => r.id)).toEqual(['swap', 'swap_activity', 'gpu0.temp'])
    expect(swap[0]).toMatchObject({ unit: 'MiB/s', threshold: 10, over: true })
  })

  it('sums the memory of the VMs, largest first', () => {
    const m = vmMemory({ work: { memory_bytes: 8 * GIB }, priv: { memory_bytes: 2 * GIB }, lab: { memory_bytes: 8 * GIB } })
    expect(m.rows.map(r => r.name)).toEqual(['lab', 'work', 'priv'])
    expect(m.total).toBe(18 * GIB)
  })

  it('keeps 30 minutes of history, newest last, and skips a repeated sample', () => {
    let history = [{ time: 100, values: { a: 1 }, vms: {} }]
    history = appendSample(history, hostSample([reading('a', 'cpu_load', 2)], {}, 1950), 1800)
    expect(history.map(p => p.time)).toEqual([1950])           // 100 is older than 1950 - 1800
    expect(appendSample(history, hostSample([reading('a', 'cpu_load', 3)], {}, 1950), 1800)).toBe(history)
    expect(hostSeries(history, 'a')).toEqual([{ time: 1950, value: 2 }])
  })

  it('draws a line within the box, and nothing for one point', () => {
    expect(sparkPoints([{ time: 0, value: 1 }], 100, 20)).toBe('')
    const points = sparkPoints([{ time: 0, value: 0 }, { time: 10, value: 100 }], 100, 20, { min: 0, max: 100 })
    expect(points).toBe('0.0,18.0 100.0,2.0')
  })
})

describe('host bar', () => {
  useFreshApp()

  const snapshot = {
    interval: 5, history_seconds: 1800, thresholds: {}, time: 1000,
    readings: [reading('cpu.load', 'cpu_load', 10)], vms: {},
    history: [
      { time: 940, values: { 'cpu.load': 5 }, vms: {} },
      { time: 970, values: { 'cpu.load': 8 }, vms: {} },
      { time: 1000, values: { 'cpu.load': 10 }, vms: {} },
    ],
  }

  it('shows readings and memory per VM collapsed, highlights what is over its threshold', async () => {
    const { wrapper, es } = await openPage('/vms', { 'GET /host': ok(snapshot) })
    es.emit('host', hostSample([
      reading('cpu.load', 'cpu_load', 12), reading('cpu.temp', 'cpu_temp', 91),
      reading('memory', 'memory', 50), reading('nvme0n1.temp', 'nvme_temp', 72, { device: 'Samsung 980' }),
    ], { work: 12 * GIB, priv: 3 * GIB }))
    await flushPromises()
    const bar = wrapper.find('[data-testid="host-bar"]')
    expect(bar.attributes('data-open')).toBe('false')
    expect(wrapper.find('[data-testid="host-bar-details"]').exists()).toBe(false)
    const chips = Object.fromEntries(bar.findAll('[data-testid="host-chip"]').map(c => [c.attributes('data-id'), c.attributes('data-over')]))
    expect(chips).toEqual({ 'cpu.load': 'false', 'cpu.temp': 'true', memory: 'false', 'nvme0n1.temp': 'true' })
    expect(bar.attributes('data-over')).toBe('true')
    const memory = bar.findAll('[data-testid="host-bar-memory-row"]').map(r => [r.attributes('data-vm'), r.attributes('data-bytes')])
    expect(memory).toEqual([['work', String(12 * GIB)], ['priv', String(3 * GIB)]])
    // not highlighted: a Host within its thresholds
    es.emit('host', hostSample([reading('cpu.load', 'cpu_load', 12), reading('cpu.temp', 'cpu_temp', 60)]))
    await flushPromises()
    expect(wrapper.find('[data-testid="host-bar"]').attributes('data-over')).toBe('false')
  })

  it('expands to tiles with the history, highlighting there too, and remembers its state', async () => {
    const { wrapper, es } = await openPage('/vms', { 'GET /host': ok(snapshot) })
    es.emit('host', hostSample([reading('cpu.load', 'cpu_load', 12), reading('gpu0.temp', 'gpu_temp', 90, { device: 'TITAN RTX' })], { work: 4 * GIB }, 1030))
    await flushPromises()
    await wrapper.find('[data-testid="host-bar-toggle"]').trigger('click')
    expect(localStorage.getItem('agentbox-hostbar-open')).toBe('1')
    const tiles = wrapper.findAll('[data-testid="host-bar-details"] [data-testid="host-tile"]')
    expect(tiles.map(t => [t.attributes('data-id'), t.attributes('data-over')])).toEqual([['cpu.load', 'false'], ['gpu0.temp', 'true']])
    expect(tiles[1]!.text()).toContain('TITAN RTX')
    expect(tiles[1]!.text()).toContain('90 °C')
    expect(tiles[1]!.text()).toContain('Over 87 °C')
    // 3 fetched points + the live one: a line
    expect(tiles[0]!.find('polyline').attributes('points')!.split(' ')).toHaveLength(4)
    expect(wrapper.find('[data-testid="host-vm-row"]').text()).toContain('work')

    await wrapper.find('[data-testid="host-bar-toggle"]').trigger('click')
    expect(localStorage.getItem('agentbox-hostbar-open')).toBe('0')
    expect(wrapper.find('[data-testid="host-bar-details"]').exists()).toBe(false)
  })

  it('starts expanded when it was left expanded in this browser', async () => {
    localStorage.setItem('agentbox-hostbar-open', '1')
    const { wrapper, es } = await openPage('/vms', { 'GET /host': ok(snapshot) })
    es.emit('host', hostSample([reading('cpu.load', 'cpu_load', 12)]))
    await flushPromises()
    expect(wrapper.find('[data-testid="host-bar"]').attributes('data-open')).toBe('true')
    expect(wrapper.find('[data-testid="host-bar-details"]').exists()).toBe(true)
  })

  it('says so when the Host has no readings', async () => {
    const { wrapper } = await openPage('/vms', { 'GET /host': { status: 503, body: { error: { code: 'readings_unavailable', message: 'x' } } } })
    expect(wrapper.find('[data-testid="host-bar-empty"]').text()).toContain('not available')
  })
})
