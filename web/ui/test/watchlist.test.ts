// The Watchlist view against a fake backend: Alerts across VMs (new first, mark as seen,
// live), checking a domain (Signals and why), Harmless marks (add, remove after asking),
// the hand-maintained Watchlist and the Legitimate domains (add, note, remove; what only
// the Host may do) and the Feeds' state.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { FakeEventSource } from './fakes'
import { $, $$, changes, click, fail, must, ok, openApp, settle, type, unmountAll, waitFor } from './access-helpers'

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
  domain: 'npm-cache.example', port: 443, alert: true, strong: true, harmless: null, legitimate: null, feeds: {},
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
    'GET /watchlist/harmless': ok(['githbu.com']),
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
          { signal: 'port', strong: true, suppressed: false, reason: 'port 22 instead of 443', code: 'port', params: {} },
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
      'port 22 instead of 443',
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
    let harmless = ['githbu.com']
    const { api } = await openApp('/watchlist', routes({
      'GET /watchlist/check?domain=npm-cache.example': () => ok(harmless.includes('npm-cache.example')
        ? { ...CHECK, harmless: 'npm-cache.example', alert: false, signals: CHECK.signals.map(s => ({ ...s, suppressed: true })) }
        : CHECK),
      'GET /watchlist/harmless': () => ok(harmless),
      'PUT /watchlist/harmless/npm-cache.example': () => { harmless = [...harmless, 'npm-cache.example']; return ok({}) },
    }))
    await type('[data-testid="check-input"]', ' HTTPS://npm-cache.example/x ')
    await click('[data-testid="check-submit"]')
    const result = must('[data-testid="check-result"]')
    expect(result.dataset.strong).toBe('true')
    expect(result.textContent).toContain('npm-cache.example: strongly suspicious')
    expect(result.textContent).toContain('Reaching it raises an Alert.')
    expect(result.textContent).toContain('It stays blocked in Open web.')
    expect(result.textContent).toContain('Only the CLI on the Host can allow it (--force).')
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
    await waitFor(() => expect(must('[data-testid="check-result"]').textContent).toContain('marked harmless (npm-cache.example)'))
    expect(must('[data-testid="check-result"]').textContent).toContain('It raises no Alerts (harmless mark).')
    expect(must('[data-testid="check-result"]').textContent).toContain('It stays blocked in Open web.')
    await waitFor(() => expect($$('[data-testid="harmless-row"]').map(r => r.dataset.domain)).toEqual(['githbu.com', 'npm-cache.example']))
  })

  it('adds a Harmless mark and removes one only after confirmation', async () => {
    const { api } = await openApp('/watchlist', routes({
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
    // the Alerts are read again: harmless destinations raise none
    expect(api.calls.filter(c => c.path === '/vms/work/alerts').length).toBeGreaterThanOrEqual(3)
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
    const { api } = await openApp('/watchlist', routes({
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
    // the list is read again after each change
    expect(api.calls.filter(c => c.method === 'GET' && c.path === '/watchlist').length).toBeGreaterThanOrEqual(3)
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

  it('says when the Watchlist on the Host cannot be read and Open web stays closed; the Feeds still show', async () => {
    await openApp('/watchlist', routes({
      'GET /watchlist': ok({ ...WATCHLIST, entries: [], unreadable: '/etc/agentbox/watchlist.json: not valid JSON' }),
    }))
    const alert = must('[data-testid="hand-list-unreadable"]')
    expect(alert.textContent).toContain('Open web stays closed until it is fixed on the Host.')
    expect(alert.textContent).toContain('/etc/agentbox/watchlist.json: not valid JSON')
    expect($('[data-testid="hand-list-error"]')).toBeNull()
    expect($$('[data-testid="watchlist-entry"]')).toEqual([])
    expect($$('[data-testid="feed"]').map(f => f.dataset.feed)).toEqual(['threat-intelligence', 'newly-registered'])
    expect($('[data-testid="feeds-error"]')).toBeNull()
  })

  it('shows the command for the Host when a missing Watchlist would be created again', async () => {
    await openApp('/watchlist', routes({
      'GET /watchlist': ok({ ...WATCHLIST, entries: [], unreadable: '/etc/agentbox/watchlist.json missing' }),
      'POST /watchlist/entries': fail(409, 'watchlist_recreate_force_required', 'Creating the Watchlist again would reopen Open web',
        'Watchlist unreadable, Open web closed: /etc/agentbox/watchlist.json is missing; agentbox watch-add with --force on the Host creates it again and so opens Open web again. Creating it again only with --force'),
    }))
    await type('[data-testid="watch-add-domain"]', 'drop.example')
    await click('[data-testid="watch-add-submit"]')
    const refusal = must('[data-testid="watch-add-error"]')
    expect(refusal.textContent).toContain('There is no Watchlist on the Host.')
    expect(refusal.textContent).toContain('Creating it again would reopen Open web, so only the Host can do it, with --force:')
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
  })

  it('opens the check for an Alert\'s destination', async () => {
    const { api } = await openApp('/watchlist', routes({ 'GET /watchlist/check?domain=npm-cache.example': ok(CHECK) }))
    Element.prototype.scrollIntoView = () => {}
    await click($$('[data-testid="alert-row"]')[0]!.querySelector('[data-testid="alert-check"]')!)
    expect(api.calls.some(c => c.path === '/watchlist/check?domain=npm-cache.example')).toBe(true)
    await waitFor(() => expect($('[data-testid="check-result"]')).not.toBeNull())
  })
})
