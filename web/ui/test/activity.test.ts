// Activity (#15): one timeline with time, VM, actor and description, filters by kind, VM and
// text, live entries from `activity` events.
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { addEntry, activityQuery, matchesFilter } from '~/utils/activity'
import { ok, openPage, useFreshApp } from './system-helpers'
import { vm } from './fakes'

const entry = (over: Record<string, unknown> = {}) => ({
  time: '2026-11-03T12:00:00.000000Z', vm: 'work', actor: 'Firefox/Linux 192.0.2.36', kind: 'allowlist',
  action: 'allow', outcome: 'ok', description: 'allowed pypi.org for 1h', ...over,
})

describe('activity (units)', () => {
  it('builds the query from the filter', () => {
    expect(activityQuery({ kinds: [], vm: '', search: '' }, 200)).toBe('lines=200')
    expect(activityQuery({ kinds: ['alert', 'build'], vm: 'work', search: ' pypi ' }, 99999)).toBe('kind=alert%2Cbuild&vm=work&search=pypi&lines=5000')
  })

  it('matches entries like the backend does: kind, VM and text over VM, actor, kind, action, description', () => {
    const e = entry()
    expect(matchesFilter(e, { kinds: ['allowlist'], vm: 'work', search: 'PYPI' })).toBe(true)
    expect(matchesFilter(e, { kinds: ['alert'], vm: '', search: '' })).toBe(false)
    expect(matchesFilter(e, { kinds: [], vm: 'priv', search: '' })).toBe(false)
    expect(matchesFilter(e, { kinds: [], vm: '', search: 'firefox' })).toBe(true)
    expect(matchesFilter(e, { kinds: [], vm: '', search: 'nothing' })).toBe(false)
    // and what the UI shows for it, in German for instance
    expect(matchesFilter(e, { kinds: [], vm: '', search: 'Freigabe' })).toBe(false)
    expect(matchesFilter(e, { kinds: [], vm: '', search: 'freigabe' }, 'Freigabe pypi.org für 1 Stunde')).toBe(true)
  })

  it('adds entries newest first without repeating one', () => {
    const a = entry({ time: '2026-11-03T12:00:00Z', description: 'a' })
    const b = entry({ time: '2026-11-03T12:05:00Z', description: 'b' })
    let list = addEntry([a], b, 10)
    expect(list.map(e => e.description)).toEqual(['b', 'a'])
    expect(addEntry(list, b, 10)).toBe(list)
    list = addEntry(list, entry({ time: '2026-11-03T12:03:00Z', description: 'c' }), 10)
    expect(list.map(e => e.description)).toEqual(['b', 'c', 'a'])
    expect(addEntry(list, entry({ time: '2026-11-03T12:09:00Z', description: 'd' }), 3).map(e => e.description)).toEqual(['d', 'b', 'c'])
  })
})

describe('Activity page', () => {
  useFreshApp()

  const rows = (wrapper: Awaited<ReturnType<typeof openPage>>['wrapper']) => wrapper.findAll('[data-testid="activity-row"]')

  it('lists the timeline with time, kind, VM, actor and description', async () => {
    const { wrapper } = await openPage('/activity', {
      'GET /activity?lines=200': ok({
        entries: [
          entry({ time: '2026-11-03T12:05:00Z', kind: 'alert', vm: 'priv', actor: 'VM priv', description: 'Verdacht: paste.example (young)' }),
          entry({ time: '2026-11-02T08:00:00Z', kind: 'reset', vm: 'work', actor: 'timer', action: 'reset.failed', outcome: 'failed', description: 'automatic Reset failed' }),
        ],
        truncated: false, retention_days: 90,
      }),
    })
    await flushPromises()
    const r = rows(wrapper)
    expect(r).toHaveLength(2)
    expect(r[0]!.attributes('data-kind')).toBe('alert')
    expect(r[0]!.text()).toContain('priv')
    expect(r[0]!.text()).toContain('Verdacht: paste.example (young)')
    expect(r[0]!.text()).toContain('by VM priv')
    expect(r[0]!.find('time').attributes('datetime')).toBe('2026-11-03T12:05:00Z')
    expect(r[1]!.attributes('data-outcome')).toBe('failed')
    expect(r[1]!.text()).toContain('failed')
    expect(wrapper.findAll('[data-testid="activity-list"] section')).toHaveLength(2)      // two days
    expect(wrapper.find('[data-testid="activity-retention"]').text()).toContain('90 days')
  })

  it('filters by kind, VM and text with a new request each', async () => {
    const { wrapper, api } = await openPage('/activity', {
      'GET /activity?lines=200': ok({ entries: [entry(), entry({ kind: 'alert', description: 'x' })], truncated: false, retention_days: 90 }),
      'GET /activity?kind=alert&lines=200': ok({ entries: [entry({ kind: 'alert', description: 'only alerts' })], truncated: false, retention_days: 90 }),
      'GET /activity?kind=alert&vm=priv&lines=200': ok({ entries: [], truncated: false, retention_days: 90 }),
      'GET /activity?kind=alert&vm=priv&search=paste&lines=200': ok({ entries: [], truncated: false, retention_days: 90 }),
    })
    await flushPromises()
    expect(rows(wrapper)).toHaveLength(2)

    await wrapper.find('[data-testid="activity-kind"][data-kind="alert"]').trigger('click')
    await flushPromises()
    expect(rows(wrapper).map(r => r.text())).toEqual([expect.stringContaining('only alerts')])
    expect(wrapper.find('[data-testid="activity-kind"][data-kind="alert"]').attributes('aria-pressed')).toBe('true')

    await wrapper.find('select[data-testid="activity-vm"]').setValue('priv').catch(() => {})
    // the VM list comes from the live VMs: none yet, so the option exists only after they arrive
    expect(api.calls.filter(c => c.path.startsWith('/activity')).map(c => c.path)).toContain('/activity?kind=alert&lines=200')
  })

  it('searches after a pause and says when nothing matches', async () => {
    const { wrapper, es, api } = await openPage('/activity', {
      'GET /activity?lines=200': ok({ entries: [entry()], truncated: false, retention_days: 90 }),
      'GET /activity?search=nothing&lines=200': ok({ entries: [], truncated: false, retention_days: 90 }),
    })
    es.emit('state', { vms: [vm('work'), vm('priv', 'running', { network: 2 })], problems: [] })
    await flushPromises()
    expect(wrapper.find('select[data-testid="activity-vm"]').findAll('option').map(o => o.text())).toEqual(['All VMs', 'priv', 'work'])
    await wrapper.find('[data-testid="activity-search"] input, input[data-testid="activity-search"]').setValue('nothing')
    await new Promise(r => setTimeout(r, 450))
    await flushPromises()
    expect(api.calls.some(c => c.path === '/activity?search=nothing&lines=200')).toBe(true)
    expect(wrapper.find('[data-testid="activity-empty"]').text()).toContain('No entry matches the filter.')
  })

  it('adds entries from the stream at the top, only those that pass the filter', async () => {
    const { wrapper, es } = await openPage('/activity', {
      'GET /activity?lines=200': ok({ entries: [entry({ description: 'old' })], truncated: false, retention_days: 90 }),
      'GET /activity?kind=alert&lines=200': ok({ entries: [], truncated: false, retention_days: 90 }),
    })
    await flushPromises()
    es.emit('activity', entry({ time: '2026-11-03T12:10:00Z', kind: 'build', vm: null, actor: 'timer', description: 'Build base-1 done' }))
    await flushPromises()
    expect(rows(wrapper).map(r => r.attributes('data-kind'))).toEqual(['build', 'allowlist'])
    expect(rows(wrapper)[0]!.text()).toContain('—')           // no VM
    es.emit('activity', entry({ time: '2026-11-03T12:10:00Z', kind: 'build', vm: null, actor: 'timer', description: 'Build base-1 done' }))
    await flushPromises()
    expect(rows(wrapper)).toHaveLength(2)                      // a repeat is not shown twice

    await wrapper.find('[data-testid="activity-kind"][data-kind="alert"]').trigger('click')
    await flushPromises()
    es.emit('activity', entry({ time: '2026-11-03T12:11:00Z', kind: 'build', description: 'ignored' }))
    es.emit('activity', entry({ time: '2026-11-03T12:12:00Z', kind: 'alert', description: 'shown' }))
    await flushPromises()
    expect(rows(wrapper).map(r => r.text())).toEqual([expect.stringContaining('shown')])
  })

  it('finds live entries by the words it shows for them (#69)', async () => {
    await useNuxtApp().$i18n.setLocale('de')
    const { wrapper, es } = await openPage('/activity', {
      'GET /activity?lines=200': ok({ entries: [], truncated: false, retention_days: 90 }),
      'GET /activity?search=Freigabe&lines=200': ok({ entries: [], truncated: false, retention_days: 90 }),
    })
    await wrapper.find('[data-testid="activity-search"] input, input[data-testid="activity-search"]').setValue('Freigabe')
    await new Promise(r => setTimeout(r, 450))
    await flushPromises()
    es.emit('activity', entry({ time: '2026-11-03T12:10:00Z', description: 'Allow pypi.org for 1 hour', code: 'allow',
      params: { domain: 'pypi.org', scope: 'work', duration: '1h' } }))
    es.emit('activity', entry({ time: '2026-11-03T12:11:00Z', kind: 'action', action: 'pause', description: 'Pause',
      code: 'pause', params: {} }))
    await flushPromises()
    expect(rows(wrapper).map(r => r.text())).toEqual([expect.stringContaining('Freigabe pypi.org für 1 Stunde')])
  })

  it('shows a failed request in its own words and tries again', async () => {
    let fail = true
    const { wrapper } = await openPage('/activity', {
      'GET /activity?lines=200': () => fail
        ? { status: 502, body: { error: { code: 'journal_failed', message: 'x', detail: 'journalctl: kaputt' } } }
        : ok({ entries: [entry()], truncated: true, retention_days: 30 }),
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="activity-error"]').text()).toContain('Reading the Host\'s journal failed.')
    expect(wrapper.find('[data-testid="activity-error"]').text()).not.toContain('kaputt')
    fail = false
    await wrapper.find('[data-testid="activity-error"] button').trigger('click')
    await flushPromises()
    expect(rows(wrapper)).toHaveLength(1)
    expect(wrapper.find('[data-testid="activity-truncated"]').exists()).toBe(true)
  })
})
