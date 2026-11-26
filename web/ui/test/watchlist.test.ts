// The Watchlist view against a fake backend: Alerts across VMs (new first, mark as seen,
// live), checking a domain (Signals and why), Harmless marks (add, remove after asking),
// the hand-maintained Watchlist and the Legitimate domains (add, note, remove; what only
// the Host may do) and the Feeds' state.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { FakeEventSource } from './fakes'
import { $, $$, changes, click, fail, must, ok, openApp, settle, type, unmountAll, waitFor, withData } from './access-helpers'

const now = () => Math.floor(Date.now() / 1000)

const WATCHLIST = {
  entries: [
    { domain: 'webhook.site', category: 'exfil', strong: true, source: 'Shai-Hulud 1 (09/2025)', note: '', added: 1793700000, added_by: 'migration' },
    { domain: 'jsonkeeper.com', category: 'payload', strong: false, source: 'Contagious Interview (11/2025)', note: 'nur Warnung', added: 1793700000, added_by: 'bewr' },
  ],
  unreadable: null,
  feeds: {
    'newly-registered': { present: true, entries: 812345, updated: now() - 3 * 3600, attempted: now() - 3 * 3600, error: null, days: 14 },
    'threat-intelligence': { present: true, entries: 450000, updated: now() - 30 * 3600, attempted: now() - 600, error: 'tif-onlydomains.txt: HTTP 503' },
  },
  feed_interval_hours: 12,
  young_domain_days: 14,
}

const LEGITIMATE = [
  { domain: 'github.com', note: '', added: null, added_by: null, builtin: true },
  { domain: 'pnpm.io', note: 'pnpm registry', added: 1793700000, added_by: 'bewr', builtin: false },
]

const CHECK = {
  domain: 'npm-cache.example', alert: true, strong: true, harmless: null, legitimate: null, feeds: {},
  signals: [
    { signal: 'lookalike', strong: true, suppressed: false, reason: 'brand name npm in the foreign domain npm-cache.example',
      code: 'brand', params: { brand: 'npm', domain: 'npm-cache.example' } },
    { signal: 'young', strong: false, suppressed: false, reason: 'newly registered (HaGeZi NRD): npm-cache.example',
      code: 'young-feed', params: { domain: 'npm-cache.example' } },
  ],
}

function routes(extra: Record<string, unknown> = {}) {
  return {
    'GET /vms/work/alerts': ok([
      { domain: 'paste.example', tags: ['paste'], count: 2, first: now() - 900, last: now() - 300, seen: true },
      { domain: 'npm-cache.example', tags: ['lookalike', 'young'], count: 1, first: now() - 60, last: now() - 60, seen: false },
    ]),
    'GET /vms/priv/alerts': ok([
      { domain: '169.254.169.254', tags: ['metadata'], count: 4, first: now() - 7200, last: now() - 3600, seen: false },
    ]),
    'GET /watchlist/harmless': ok([{ domain: 'githbu.com', regex: null }]),
    'GET /watchlist': ok(WATCHLIST),
    'GET /watchlist/legitimate': ok(LEGITIMATE),
    ...extra,
  } as Parameters<typeof openApp>[1]
}

beforeEach(async () => {
  resetSessionForTests()
  resetLiveForTests()
  FakeEventSource.reset()
  vi.stubGlobal('EventSource', FakeEventSource)
  await useNuxtApp().$i18n.setLocale('en')
})
afterEach(() => {
  unmountAll()
  resetLiveForTests()
  resetSessionForTests()
})

function alertRows() {
  return $$('[data-testid="alert-row"]').map(r => [r.dataset.vm, r.dataset.domain, r.dataset.seen])
}

/** The Activity entry of a Watchlist change made in this browser. */
const watchEntry = (code: string) => ({ time: 't', vm: null, actor: 'owner', kind: 'action', action: code, code, outcome: 'ok', description: '' })

describe('Watchlist view', () => {
  it('lists the Alerts of every VM, new ones first, and marks them as seen', async () => {
    let seenAll = false
    const { api } = await openApp('/watchlist', routes({
      'POST /vms/work/alerts/seen': ok({}),
      'POST /vms/priv/alerts/seen': () => { seenAll = true; return ok({}) },
      'GET /vms/priv/alerts': () => ok([{ domain: '169.254.169.254', tags: ['metadata'], count: 4, first: now() - 7200, last: now() - 3600, seen: seenAll }]),
    }))
    expect(alertRows()).toEqual([
      ['work', 'npm-cache.example', 'false'],
      ['priv', '169.254.169.254', 'false'],
      ['work', 'paste.example', 'true'],
    ])
    expect(must('[data-testid="alerts-new"]').textContent).toBe('2 new')
    const first = $$('[data-testid="alert-row"]')[0]!
    expect([...first.querySelectorAll('[data-signal]')].map(b => b.textContent)).toEqual(['Lookalike', 'Young domain'])

    await click(first.querySelector('[data-testid="alert-seen"]')!)
    await click('[data-testid="alerts-all-seen"]')
    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/vms/work/alerts/seen', body: { domain: 'npm-cache.example' }, csrf: '1' },
      { method: 'POST', path: '/vms/work/alerts/seen', body: {}, csrf: '1' },
      { method: 'POST', path: '/vms/priv/alerts/seen', body: {}, csrf: '1' },
    ])
  })

  it('reloads the Alerts when a new one arrives live', async () => {
    const { api, es } = await openApp('/watchlist', routes())
    const loads = () => api.calls.filter(c => c.path === '/vms/work/alerts').length
    const before = loads()
    es.emit('alert', { time: '2026-11-03T12:00:00Z', vm: 'work', domain: 'x.example', signals: ['young'] })
    await waitFor(() => expect(loads()).toBe(before + 1))
  })

  describe('follows the Activity', () => {
    const entry = (action: string) => ({ time: '2026-11-03T12:00:00Z', vm: null, actor: 'root', kind: 'watchlist', action, code: action, outcome: 'ok', description: '' })
    async function open() {
      const opened = await openApp('/watchlist', routes())
      const loads = () => ({
        harmless: opened.api.calls.filter(c => c.path === '/watchlist/harmless').length,
        watchlist: opened.api.calls.filter(c => c.path === '/watchlist').length,
        legitimate: opened.api.calls.filter(c => c.path === '/watchlist/legitimate').length,
        alerts: opened.api.calls.filter(c => c.path === '/vms/work/alerts').length,
      })
      return { ...opened, loads, before: loads() }
    }

    it.each([
      ['watch-add', 'watchlist'], ['watch-remove', 'watchlist'], ['watch-set-note', 'watchlist'], ['maintenance.feeds', 'watchlist'],
      ['legitimate-add', 'legitimate'], ['legitimate-remove', 'legitimate'], ['legitimate-set-note', 'legitimate'],
      ['mark-harmless', 'harmless'], ['unmark-harmless', 'harmless'], ['mark-seen', 'alerts'],
    ] as const)('%s loads the %s and nothing else', async (code, list) => {
      const { es, loads, before } = await open()
      es.emit('activity', entry(code))
      await waitFor(() => expect(loads()[list]).toBe(before[list] + 1))
      await new Promise(r => setTimeout(r, 400))
      const after = loads()
      for (const other of ['harmless', 'watchlist', 'legitimate', 'alerts'] as const) {
        // a Harmless mark also reloads the Alerts (they leave out harmless destinations)
        if (other !== list && !(list === 'harmless' && other === 'alerts')) expect(after[other], other).toBe(before[other])
      }
    })

    it('migrate-lists loads the Watchlist and the Legitimate domains', async () => {
      const { es, loads, before } = await open()
      es.emit('activity', entry('migrate-lists'))
      await waitFor(() => expect(loads()).toMatchObject({ watchlist: before.watchlist + 1, legitimate: before.legitimate + 1, harmless: before.harmless }))
    })

    it('ignores other entries, and a hello that resumed', async () => {
      const { es, loads, before } = await open()
      es.emit('activity', entry('something-else'))
      es.emit('hello', { stream: 'x', resumed: true })
      await new Promise(r => setTimeout(r, 500))
      expect(loads()).toEqual(before)
    })

    it('loads all three lists and the Alerts after a reconnect without resume', async () => {
      const { es, loads, before } = await open()
      es.emit('hello', { stream: 'y', resumed: false })
      await waitFor(() => expect(loads()).toMatchObject({
        harmless: before.harmless + 1, watchlist: before.watchlist + 1, legitimate: before.legitimate + 1,
      }))
      expect(loads().alerts).toBeGreaterThan(before.alerts)
    })
  })

  it('words the reasons of the Signals in German, and keeps the English text it cannot word', async () => {
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/watchlist', routes({
      'GET /watchlist/check?domain=odd.example': ok({
        ...CHECK, domain: 'odd.example',
        signals: [
          ...CHECK.signals,
          { signal: 'exfil', strong: true, suppressed: false, reason: 'Watchlist: odd.example (Shai-Hulud 1)', code: 'watchlist', params: { domain: 'odd.example', source: 'Shai-Hulud 1' } },
          { signal: 'raw-ip', strong: true, suppressed: false, reason: 'IP address instead of a domain', code: 'raw-ip', params: {} },
          // a code without the parameter it needs, a code and a Signal this UI does not know,
          // a reason without a code (a classifier from before), a Signal from before #69
          { signal: 'c2', strong: true, suppressed: false, reason: 'Watchlist: no domain given', code: 'watchlist', params: {} },
          { signal: 'tld', strong: false, suppressed: false, reason: 'something new', code: 'made-up', params: {} },
          { signal: 'shiny', strong: false, suppressed: false, reason: 'a new Signal', code: 'tld', params: { tld: 'x' } },
          { signal: 'lookalike', strong: true, suppressed: false, reason: 'typo of github (distance 1)' },
          { signal: 'roh-ip', strong: true, suppressed: false, reason: 'IP-Adresse statt Domain', code: 'raw-ip', params: {} },
        ],
      }),
    }))
    await type('[data-testid="check-input"]', 'odd.example')
    await click('[data-testid="check-submit"]')
    await waitFor(() => expect($('[data-testid="check-result"]')).not.toBeNull())
    expect($$('[data-testid="signal-reason"]').map(r => r.textContent!.trim())).toEqual([
      'Der Markenname npm in einer fremden Domain, npm-cache.example',
      'HaGeZi-Liste neu registrierter Domains: npm-cache.example',
      'Watchlist: odd.example (Shai-Hulud 1)',
      'Eine IP-Adresse statt einer Domain',
      'Watchlist: no domain given',
      'something new',
      'a new Signal',
      'typo of github (distance 1)',
      'IP-Adresse statt Domain',
    ])
    // the renamed Signals have names, the old ones show as they are
    const item = (signal: string) => must(`[data-testid="signal-list"] [data-signal="${signal}"]`).textContent!
    expect(item('raw-ip')).toContain('IP-Adresse')
    expect(item('roh-ip')).toContain('roh-ip')
  })

  it('says which VM\'s Alerts could not be read', async () => {
    await openApp('/watchlist', routes({ 'GET /vms/priv/alerts': fail(502, 'journal_failed') }))
    expect(must('[data-testid="alerts"]').textContent).toContain('Alerts of priv could not be read: Reading the Host\'s journal failed.')
    expect(alertRows().map(r => r[0])).toEqual(['work', 'work'])
  })

  it('checks a domain: Signals and why, what it means, and marks it harmless', async () => {
    let harmless = [{ domain: 'githbu.com', regex: null as string | null }]
    const { api, es } = await openApp('/watchlist', routes({
      'GET /watchlist/check?domain=npm-cache.example': () => ok(harmless.some(m => m.domain === 'npm-cache.example')
        ? { ...CHECK, harmless: 'npm-cache.example', alert: false, signals: CHECK.signals.map(s => ({ ...s, suppressed: true })) }
        : CHECK),
      'GET /watchlist/harmless': () => ok(harmless),
      'PUT /watchlist/harmless/npm-cache.example': () => { harmless = [...harmless, { domain: 'npm-cache.example', regex: null }]; return ok({}) },
    }))
    await type('[data-testid="check-input"]', ' HTTPS://npm-cache.example/x ')
    await click('[data-testid="check-submit"]')
    const result = must('[data-testid="check-result"]')
    expect(result.dataset.strong).toBe('true')
    expect(result.textContent).toContain('npm-cache.example: strongly suspicious')
    expect(result.textContent).toContain('Reaching it raises an Alert.')
    expect(result.textContent).toContain('It stays blocked, also with an Allowlist entry or in Open web.')
    expect(result.textContent).toContain('Only lifting its Signals on the Host lets it through (--force).')
    expect([...result.querySelectorAll('[data-signal]')].map(s => (s as HTMLElement).dataset.signal)).toEqual(['lookalike', 'young'])
    // each reason worded from its code and parameters, the classifier's English text on hover
    expect($$('[data-testid="signal-reason"]').map(r => r.textContent!.trim())).toEqual([
      'The brand name npm in a foreign domain, npm-cache.example',
      'HaGeZi\'s list of newly registered domains: npm-cache.example',
    ])
    expect($$('[data-testid="signal-reason"]').map(r => r.getAttribute('title'))).toEqual([
      'brand name npm in the foreign domain npm-cache.example', 'newly registered (HaGeZi NRD): npm-cache.example',
    ])

    await click('[data-testid="check-mark-harmless"]')
    expect(changes(api.calls)).toEqual([{ method: 'PUT', path: '/watchlist/harmless/npm-cache.example', body: undefined, csrf: '1' }])
    // the check and the marks are read again through the change's own Activity entry
    es.emit('activity', watchEntry('mark-harmless'))
    await waitFor(() => expect(must('[data-testid="check-result"]').textContent).toContain('marked harmless (npm-cache.example)'))
    expect(must('[data-testid="check-result"]').textContent).toContain('It raises no Alerts (harmless mark).')
    expect(must('[data-testid="check-result"]').textContent).toContain('It stays blocked, also with an Allowlist entry or in Open web.')
    await waitFor(() => expect($$('[data-testid="harmless-row"]').map(r => r.dataset.domain)).toEqual(['githbu.com', 'npm-cache.example']))
  })

  it('adds a Harmless mark and removes one only after confirmation', async () => {
    const { api, es } = await openApp('/watchlist', routes({
      'PUT /watchlist/harmless/false-positive.example': ok({}),
      'DELETE /watchlist/harmless/githbu.com': ok({}),
    }))
    await type('[data-testid="harmless-input"]', 'False-Positive.example')
    await click('[data-testid="harmless-add"]')
    await click('[data-testid="harmless-row"][data-domain="githbu.com"] [data-testid="harmless-remove"]')
    expect(document.body.textContent).toContain('Remove the harmless mark of githbu.com?')
    expect(changes(api.calls).filter(c => c.method === 'DELETE')).toEqual([])
    await click('[data-testid="confirm-action"]')
    expect(changes(api.calls)).toEqual([
      { method: 'PUT', path: '/watchlist/harmless/false-positive.example', body: undefined, csrf: '1' },
      { method: 'DELETE', path: '/watchlist/harmless/githbu.com', body: undefined, csrf: '1' },
    ])
    // the Alerts are read again on the changes' Activity entries, not by the view: harmless
    // destinations raise none
    const alertReads = () => api.calls.filter(c => c.path === '/vms/work/alerts').length
    const before = alertReads()
    es.emit('activity', watchEntry('unmark-harmless'))
    await waitFor(() => expect(alertReads()).toBe(before + 1))
  })

  it('shows the Feeds\' state and the hand-maintained Watchlist', async () => {
    await openApp('/watchlist', routes())
    const feeds = must('[data-testid="watchlist-feeds"]')
    expect(feeds.textContent).toContain('Fetched every 12 hours.')
    expect($$('[data-testid="feed"]').map(f => f.dataset.feed)).toEqual(['threat-intelligence', 'newly-registered'])
    const ti = must('[data-feed="threat-intelligence"]')
    expect(ti.querySelector('[data-testid="feed-state"]')!.textContent).toBe('last fetch failed')
    expect(ti.querySelector('[data-testid="feed-error"]')!.textContent).toContain('tif-onlydomains.txt: HTTP 503')
    expect(ti.textContent).toContain('450,000 entries')
    const nrd = must('[data-feed="newly-registered"]')
    expect(nrd.querySelector('[data-testid="feed-state"]')!.textContent).toBe('current')
    expect(nrd.textContent).toContain('registered in the last 14 days')
    expect(nrd.textContent).toContain('updated 3 h ago')
    expect($$('[data-testid="watchlist-entry"]').map(e => e.dataset.domain)).toEqual(['webhook.site', 'jsonkeeper.com'])
    expect(must('[data-testid="watchlist-entry"][data-domain="jsonkeeper.com"]').textContent).toContain('warning only')
  })

  it('adds a Watchlist entry and changes a note', async () => {
    const { api, es } = await openApp('/watchlist', routes({
      'POST /watchlist/entries': ok({}),
      'PUT /watchlist/entries/jsonkeeper.com/note': ok({}),
    }))
    await type('[data-testid="watch-add-domain"]', ' Drop.Example ')
    await type('[data-testid="watch-add-source"]', 'incident 11/2026')
    await click('[data-testid="watch-add-submit"]')
    await click('[data-testid="watchlist-entry"][data-domain="jsonkeeper.com"] [data-testid="watch-note-edit"]')
    await type('[data-testid="watch-note-input"]', ' seen in work ')
    await click('[data-testid="watch-note-save"]')
    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/watchlist/entries', body: { domain: 'drop.example', category: 'exfil', source: 'incident 11/2026' }, csrf: '1' },
      { method: 'PUT', path: '/watchlist/entries/jsonkeeper.com/note', body: { note: 'seen in work' }, csrf: '1' },
    ])
    // the list is read again through the changes' Activity entries, not by the view
    const reads = () => api.calls.filter(c => c.method === 'GET' && c.path === '/watchlist').length
    expect(reads()).toBe(1)
    es.emit('activity', watchEntry('watch-set-note'))
    await waitFor(() => expect(reads()).toBe(2))
  })

  it('removes a weak Watchlist entry after asking; a strong one only shows the command for the Host', async () => {
    const { api } = await openApp('/watchlist', routes({ 'DELETE /watchlist/entries/jsonkeeper.com': ok({}) }))
    const strong = must('[data-testid="watchlist-entry"][data-domain="webhook.site"]')
    expect(strong.querySelector('[data-testid="watch-remove"]')).toBeNull()
    await click(strong.querySelector('[data-testid="watch-remove-locked"]')!)
    expect(must('[data-testid="watch-remove-cli"]').textContent).toContain('sudo agentbox watch-remove webhook.site --force')
    expect($('[data-testid="confirm-action"]')).toBeNull()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await settle()

    await click('[data-testid="watchlist-entry"][data-domain="jsonkeeper.com"] [data-testid="watch-remove"]')
    expect(document.body.textContent).toContain('Remove jsonkeeper.com from the Watchlist?')
    await click('[data-testid="confirm-action"]')
    expect(changes(api.calls)).toEqual([{ method: 'DELETE', path: '/watchlist/entries/jsonkeeper.com', body: undefined, csrf: '1' }])
  })

  it('says when the Watchlist on the Host cannot be read and the proxy lets nothing through; the Feeds still show', async () => {
    await openApp('/watchlist', routes({
      'GET /watchlist': ok({ ...WATCHLIST, entries: [], unreadable: '/etc/agentbox/watchlist.json: not valid JSON' }),
    }))
    const alert = must('[data-testid="hand-list-unreadable"]')
    expect(alert.textContent).toContain('The proxy lets nothing through until it is fixed on the Host.')
    expect(alert.textContent).toContain('/etc/agentbox/watchlist.json: not valid JSON')
    expect($('[data-testid="hand-list-error"]')).toBeNull()
    expect($$('[data-testid="watchlist-entry"]')).toEqual([])
    expect($$('[data-testid="feed"]').map(f => f.dataset.feed)).toEqual(['threat-intelligence', 'newly-registered'])
    expect($('[data-testid="feeds-error"]')).toBeNull()
  })

  it('shows the command for the Host when a missing Watchlist would be created again', async () => {
    await openApp('/watchlist', routes({
      'GET /watchlist': ok({ ...WATCHLIST, entries: [], unreadable: '/etc/agentbox/watchlist.json missing' }),
      'POST /watchlist/entries': fail(409, 'watchlist_recreate_force_required', 'Creating the Watchlist again would let the proxy pass connections again',
        'Watchlist unreadable, the proxy lets nothing through: /etc/agentbox/watchlist.json is missing; agentbox watch-add with --force on the Host creates it again and so lets the proxy pass connections again. Creating it again only with --force'),
    }))
    await type('[data-testid="watch-add-domain"]', 'drop.example')
    await click('[data-testid="watch-add-submit"]')
    const refusal = must('[data-testid="watch-add-error"]')
    expect(refusal.textContent).toContain('There is no Watchlist on the Host.')
    expect(refusal.textContent).toContain('Creating it again would let the proxy pass connections again, so only the Host can do it, with --force:')
    expect(refusal.textContent).not.toContain('removed or weakened')
    expect(refusal.textContent).toContain('sudo agentbox watch-add drop.example --category exfil --force')
  })

  it('says when lowering a strong entry needs the Host', async () => {
    await openApp('/watchlist', routes({ 'POST /watchlist/entries': fail(409, 'force_required') }))
    await type('[data-testid="watch-add-domain"]', 'webhook.site')
    await click('[data-testid="watch-add-submit"]')
    const refusal = must('[data-testid="watch-add-error"]')
    expect(refusal.textContent).toContain('only the CLI on the Host can do it')
    expect(refusal.textContent).toContain('A strong entry can only be removed or weakened on the Host:')
    expect(refusal.textContent).toContain('sudo agentbox watch-add webhook.site --category exfil --force')
    // the view words it: once, and nothing of the module's own
    expect($$('[data-testid="watch-add-error"]')).toHaveLength(1)
    expect($('[data-change="failed"]')).toBeNull()
  })

  it('lists the Legitimate domains, the built-in ones without actions', async () => {
    await openApp('/watchlist', routes())
    expect($$('[data-testid="legitimate-row"]').map(r => [r.dataset.domain, r.dataset.builtin])).toEqual([
      ['pnpm.io', 'false'],
      ['github.com', 'true'],
    ])
    const builtin = must('[data-testid="legitimate-row"][data-domain="github.com"]')
    expect(builtin.textContent).toContain('built in')
    expect(builtin.querySelector('[data-testid="legitimate-remove"]')).toBeNull()
    expect(must('[data-testid="legitimate-row"][data-domain="pnpm.io"]').textContent).toContain('pnpm registry')
  })

  it('adds, annotates and removes a Legitimate domain', async () => {
    const { api } = await openApp('/watchlist', routes({
      'POST /watchlist/legitimate': ok({ domain: 'mirror.example', builtin: false }),
      'PUT /watchlist/legitimate/pnpm.io/note': ok({}),
      'DELETE /watchlist/legitimate/pnpm.io': ok({}),
    }))
    await type('[data-testid="legitimate-add-domain"]', 'https://Mirror.Example/simple')
    await type('[data-testid="legitimate-add-note"]', 'Spiegel')
    await click('[data-testid="legitimate-add-submit"]')
    await click('[data-testid="legitimate-row"][data-domain="pnpm.io"] [data-testid="legitimate-note-edit"]')
    await type('[data-testid="legitimate-note-input"]', 'npm mirror')
    await click('[data-testid="legitimate-note-save"]')
    await click('[data-testid="legitimate-row"][data-domain="pnpm.io"] [data-testid="legitimate-remove"]')
    expect(document.body.textContent).toContain('Remove pnpm.io from the Legitimate domains?')
    await click('[data-testid="confirm-action"]')
    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/watchlist/legitimate', body: { domain: 'mirror.example', note: 'Spiegel' }, csrf: '1' },
      { method: 'PUT', path: '/watchlist/legitimate/pnpm.io/note', body: { note: 'npm mirror' }, csrf: '1' },
      { method: 'DELETE', path: '/watchlist/legitimate/pnpm.io', body: undefined, csrf: '1' },
    ])
  })

  it('shows the command for the Host when a Legitimate domain would lift strong Signals', async () => {
    await openApp('/watchlist', routes({ 'POST /watchlist/legitimate': fail(409, 'force_required') }))
    await type('[data-testid="legitimate-add-domain"]', 'npm-mirror.example')
    await click('[data-testid="legitimate-add-submit"]')
    const refusal = must('[data-testid="legitimate-add-error"]')
    expect(refusal.textContent).toContain('sudo agentbox legitimate-add npm-mirror.example --force')
    expect($$('[data-testid="legitimate-add-error"]')).toHaveLength(1)
    expect($('[data-change="failed"]')).toBeNull()
  })

  it('adds a Regex entry to each list, tested first, and marks it as one', async () => {
    const drop = '^drop[0-9]+\\.(xyz|top)$'
    const mirrors = '^mirror[0-9]+\\.vendor\\.example$'
    const ci = '^ci[0-9]+\\.builds\\.example$'
    const enc = encodeURIComponent
    const { api } = await openApp('/watchlist', routes({
      'GET /watchlist': ok({ ...WATCHLIST, entries: [...WATCHLIST.entries,
        { domain: null, regex: drop, category: 'paste', strong: true, source: '', note: '', added: 1793700000, added_by: 'bewr' },
        { domain: null, regex: '-cdn.example', category: 'c2', strong: true, source: '', note: '', added: 1793700000, added_by: 'bewr' }] }),
      'GET /watchlist/harmless': ok([{ domain: 'githbu.com', regex: null }, { domain: null, regex: ci }]),
      [`GET /regex/check?list=watchlist&regex=${enc(drop)}&host=drop7.xyz`]: ok({ list: 'watchlist', regex: drop, scope: null, matches: { 'drop7.xyz': true }, preview: null }),
      'POST /watchlist/entries': ok({}),
      [`PUT /watchlist/harmless/regex?regex=${enc(ci)}`]: ok({}),
      [`DELETE /watchlist/harmless/regex?regex=${enc(ci)}`]: ok({}),
      'POST /watchlist/legitimate': ok({ domain: null, regex: mirrors, builtin: false }),
    }))
    // listed verbatim, marked as Regex entries; a strong one only on the Host, with --regex
    const entry = withData('[data-testid="watchlist-entry"]', 'regex', drop)
    expect(entry.querySelector('[data-testid="entry-regex"]')!.textContent).toContain('Regex')
    expect(entry.textContent).toContain(drop)
    await click(entry.querySelector('[data-testid="watch-remove-locked"]')!)
    // the pattern in one word with the option, quoted for the shell where needed
    expect(must('[data-testid="watch-remove-cli"]').textContent).toContain(`sudo agentbox watch-remove --regex='${drop}' --force`)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await settle()
    // ... so that a pattern starting with - is never taken for an option
    await click(withData('[data-testid="watchlist-entry"]', 'regex', '-cdn.example').querySelector('[data-testid="watch-remove-locked"]')!)
    expect(must('[data-testid="watch-remove-cli"]').textContent).toContain('sudo agentbox watch-remove --regex=-cdn.example --force')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await settle()

    // the Watchlist: tested against a host, then added
    const form = must('[data-testid="watch-add"]')
    await click(form.querySelector('[data-testid="entry-kind"] [data-kind="regex"]')!)
    await type('[data-testid="watch-add-regex"]', ` ${drop} `)
    await type('[data-testid="watch-add"] [data-testid="regex-host"]', 'Drop7.xyz')
    await click(form.querySelector('[data-testid="regex-test"]')!)
    const match = must('[data-testid="watch-add"] [data-testid="regex-match"]')
    expect([match.dataset.match, match.textContent!.trim()]).toEqual(['true', 'It matches drop7.xyz.'])
    await click('[data-testid="watch-add-submit"]')

    // a Harmless mark
    const harmless = must('[data-testid="harmless"]')
    expect(withData('[data-testid="harmless-row"]', 'regex', ci).querySelector('[data-testid="entry-regex"]')).not.toBeNull()
    await click(harmless.querySelector('[data-testid="entry-kind"] [data-kind="regex"]')!)
    await type('[data-testid="harmless-regex"]', ci)
    await click('[data-testid="harmless-add"]')
    await click(withData('[data-testid="harmless-row"]', 'regex', ci).querySelector('[data-testid="harmless-remove"]')!)
    await click('[data-testid="confirm-action"]')

    // a Legitimate domain: it lifts only weak Signals, and says so
    const legitimate = must('[data-testid="legitimate-add"]')
    await click(legitimate.querySelector('[data-testid="entry-kind"] [data-kind="regex"]')!)
    expect(legitimate.textContent).toContain('A Regex entry lifts only weak Signals')
    await type('[data-testid="legitimate-add-regex"]', mirrors)
    await click('[data-testid="legitimate-add-submit"]')

    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/watchlist/entries', body: { regex: drop, category: 'exfil' }, csrf: '1' },
      { method: 'PUT', path: `/watchlist/harmless/regex?regex=${enc(ci)}`, body: undefined, csrf: '1' },
      { method: 'DELETE', path: `/watchlist/harmless/regex?regex=${enc(ci)}`, body: undefined, csrf: '1' },
      { method: 'POST', path: '/watchlist/legitimate', body: { regex: mirrors }, csrf: '1' },
    ])
  })

  it('says why a pattern cannot be an entry, in the UI language', async () => {
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/watchlist', routes({
      'GET /regex/check?list=harmless&regex=(a%2B)%2B': { status: 400, body: { error: { code: 'invalid_pattern', message: 'This pattern cannot be a Regex entry', params: { reason: 'nested-quantifier' } } } },
      'PUT /watchlist/harmless/regex?regex=.*': { status: 400, body: { error: { code: 'pattern_too_broad', message: 'This pattern is too broad', params: { host: 'google.com' } } } },
    }))
    const harmless = must('[data-testid="harmless"]')
    await click(harmless.querySelector('[data-testid="entry-kind"] [data-kind="regex"]')!)
    await type('[data-testid="harmless-regex"]', '(a+)+')
    await click(harmless.querySelector('[data-testid="regex-test"]')!)
    expect(must('[data-testid="harmless"] [data-testid="regex-error"]').textContent)
      .toContain('Dieser Ausdruck kann kein Eintrag sein: ein Quantor oder eine Alternative in einer wiederholten Gruppe')
    await type('[data-testid="harmless-regex"]', '.*')
    expect($('[data-testid="harmless"] [data-testid="regex-error"]')).toBeNull()
    await click('[data-testid="harmless-add"]')
    expect(must('[data-testid="harmless-error"]').textContent).toContain('Dieser Ausdruck ist zu breit: er passt auf google.com')
  })

  it('names a Regex entry in the check of a domain', async () => {
    await openApp('/watchlist', routes({
      'GET /watchlist/check?domain=drop7.xyz': ok({
        ...CHECK, domain: 'drop7.xyz', harmless: '^drop[0-9]+\\.xyz$', harmless_regex: true, alert: false,
        signals: [{ signal: 'paste', strong: true, suppressed: true, reason: 'Watchlist: pattern ^drop[0-9]+\\.xyz$ (Vorfall)', code: 'watchlist-regex', params: { pattern: '^drop[0-9]+\\.xyz$', source: 'Vorfall' } }],
      }),
    }))
    await type('[data-testid="check-input"]', 'drop7.xyz')
    await click('[data-testid="check-submit"]')
    const result = must('[data-testid="check-result"]')
    expect(result.textContent).toContain('drop7.xyz: marked harmless (Regex entry ^drop[0-9]+\\.xyz$)')
    expect($$('[data-testid="signal-reason"]').map(r => r.textContent!.trim())).toEqual(['Watchlist, Regex entry: ^drop[0-9]+\\.xyz$ (Vorfall)'])
    // a pattern is no mark of the domain itself: removed in the list of marks
    expect($('[data-testid="check-unmark-harmless"]')).toBeNull()
  })

  it('opens the check for an Alert\'s destination', async () => {
    const { api } = await openApp('/watchlist', routes({ 'GET /watchlist/check?domain=npm-cache.example': ok(CHECK) }))
    Element.prototype.scrollIntoView = () => {}
    await click($$('[data-testid="alert-row"]')[0]!.querySelector('[data-testid="alert-check"]')!)
    expect(api.calls.some(c => c.path === '/watchlist/check?domain=npm-cache.example')).toBe(true)
    await waitFor(() => expect($('[data-testid="check-result"]')).not.toBeNull())
  })
})

describe('Watchlist changes and their failures (#215)', () => {
  beforeEach(async () => {
    resetSessionForTests()
    resetLiveForTests()
    FakeEventSource.reset()
    vi.stubGlobal('EventSource', FakeEventSource)
    await useNuxtApp().$i18n.setLocale('en')
    useState('toasts').value = []
  })
  afterEach(() => {
    unmountAll()
    resetLiveForTests()
    resetSessionForTests()
    useState('toasts').value = []
  })
  const toasts = () => useState<{ title?: string, duration?: number }[]>('toasts').value.map(t => [t.title, t.duration])

  it('a failed Alert marked as seen is a toast that stays, with Details', async () => {
    await openApp('/watchlist', routes({ 'POST /vms/work/alerts/seen': fail(502, 'action_failed', 'agentbox failed') }))
    await click($$('[data-testid="alert-row"]')[0]!.querySelector('[data-testid="alert-seen"]')!)
    expect(toasts()).toEqual([['The Alert could not be marked as seen', 0]])
    await waitFor(() => expect($('[data-testid="fault-message"]')?.textContent).toBe('action_failed: agentbox failed'))
  })

  it('a failed harmless mark from the check is a toast that stays', async () => {
    await openApp('/watchlist', routes({
      'GET /watchlist/check?domain=npm-cache.example': ok(CHECK),
      'PUT /watchlist/harmless/npm-cache.example': fail(502, 'action_failed', 'agentbox failed'),
    }))
    await type('[data-testid="check-input"]', 'npm-cache.example')
    await click('[data-testid="check-submit"]')
    await click('[data-testid="check-mark-harmless"]')
    expect(toasts()).toEqual([['The domain could not be marked as harmless', 0]])
  })

  it('a refused harmless mark from the list shows at its form, a refused removal in its dialog', async () => {
    await openApp('/watchlist', routes({
      'PUT /watchlist/harmless/x.example': fail(400, 'invalid_domain', 'Invalid domain'),
      'DELETE /watchlist/harmless/githbu.com': fail(502, 'action_failed', 'agentbox failed'),
    }))
    await type('[data-testid="harmless-input"]', 'x.example')
    await click('[data-testid="harmless-add"]')
    expect(must('[data-testid="harmless-error"] [data-testid="fault-message"]').textContent).toBe('invalid_domain: Invalid domain')
    await click('[data-testid="harmless-row"][data-domain="githbu.com"] [data-testid="harmless-remove"]')
    await click('[data-testid="confirm-action"]')
    expect(must('[data-testid="confirm-body"] [data-testid="fault-message"]').textContent).toBe('action_failed: agentbox failed')
    expect(toasts()).toEqual([])
  })
})
