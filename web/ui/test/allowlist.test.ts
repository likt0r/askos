// The Allowlist view against a fake backend: entries with scope, remaining time, uses and
// note; filter by VM; add, extend, make permanent, remove (asked first); strong-Signal
// destinations refused with their reason; changes from elsewhere reload the list.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { FakeEventSource } from './fakes'
import { $, $$, changes, click, fail, must, ok, openApp, settle, type, unmountAll, waitFor } from './access-helpers'

const now = () => Math.floor(Date.now() / 1000)

function entries() {
  return [
    { domain: 'crates.io', scope: 'all', expires: null, note: 'Rust crates', added_by: 'Firefox/Linux 192.0.2.36', added: now() - 86400, expired: false, remaining: null, uses_24h: 12 },
    { domain: 'docs.rs', scope: 'work', expires: now() + 2 * 3600 + 330, note: '', added_by: 'root', added: now() - 600, expired: false, remaining: 7500, uses_24h: 1 },
    { domain: 'old.example', scope: 'priv', expires: now() - 10, note: '', added_by: 'root', added: now() - 4000, expired: true, remaining: 0, uses_24h: null },
  ]
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

function row(domain: string) {
  return must(`[data-testid="allowlist-entry"][data-domain="${domain}"]`)
}

describe('Allowlist view', () => {
  it('lists every entry with scope, remaining time, uses and note', async () => {
    await openApp('/allowlist', { 'GET /allowlist': ok(entries()) })
    expect($$('[data-testid="allowlist-entry"]').map(r => [r.dataset.domain, r.dataset.scope]))
      .toEqual([['crates.io', 'all'], ['docs.rs', 'work'], ['old.example', 'priv']])
    expect(must('[data-testid="allowlist-count"]').textContent).toContain('3 entries · 2 temporary')

    const crates = row('crates.io')
    expect(crates.querySelector('[data-testid="entry-scope"]')!.textContent).toBe('All VMs')
    expect(crates.querySelector('[data-testid="entry-left"]')!.textContent).toBe('Permanent')
    expect(crates.querySelector('[data-testid="entry-uses"]')!.textContent).toBe('used 12 times in 24 h')
    expect(crates.querySelector('[data-testid="entry-note"]')!.textContent).toContain('Rust crates')
    expect(crates.textContent).toContain('added by Firefox/Linux 192.0.2.36')
    // permanent: nothing to extend
    expect(crates.querySelector('[data-testid="entry-extend"]')).toBeNull()

    const docs = row('docs.rs')
    expect(docs.querySelector('[data-testid="entry-scope"]')!.textContent).toBe('Only work')
    expect(docs.querySelector('[data-testid="entry-left"]')!.textContent).toBe('2 h 5 min left')
    expect(docs.querySelector('[data-testid="entry-uses"]')!.textContent).toBe('used once in 24 h')

    const old = row('old.example')
    expect(old.querySelector('[data-testid="entry-left"]')!.textContent).toBe('Expired')
    expect(old.querySelector('[data-testid="entry-uses"]')!.textContent).toBe('uses unknown')
  })

  it('filters by VM through the query', async () => {
    const { api } = await openApp('/allowlist?vm=work', { 'GET /allowlist?vm=work': ok(entries().slice(0, 2)) })
    expect(api.calls.filter(c => c.path.startsWith('/allowlist')).map(c => c.path)).toEqual(['/allowlist?vm=work'])
    expect($$('[data-testid="allowlist-entry"]')).toHaveLength(2)
    expect(must('[data-testid="allowlist-filter"]').textContent).toContain('Applying to work')
  })

  it('adds an entry with scope, duration and note', async () => {
    let list = entries().slice(0, 1)
    const { api } = await openApp('/allowlist?vm=work', {
      'GET /allowlist?vm=work': () => ok(list),
      'POST /allowlist': (call) => {
        const b = call.body as Record<string, string>
        list = [...list, { domain: b.domain!, scope: b.scope!, expires: now() + 8 * 3600, note: b.note ?? '', added_by: 'Firefox/Linux', added: now(), expired: false, remaining: 8 * 3600, uses_24h: 0 }]
        return { status: 201, body: list.at(-1) }
      },
    })
    await click('[data-testid="allowlist-add-open"]')
    await type('[data-testid="add-domain"]', ' https://Docs.RS/crate/serde ')
    await click('[data-testid="add-duration"] [data-duration="8h"]')
    await type('[data-testid="add-note"]', 'Rust docs')
    expect(must('[data-testid="add-scope"]').textContent).toContain('Only work')
    await click('[data-testid="add-submit"]')

    expect(changes(api.calls)).toEqual([{ method: 'POST', path: '/allowlist', csrf: '1',
      body: { scope: 'work', domain: 'docs.rs', duration: '8h', note: 'Rust docs' } }])
    await waitFor(() => expect($$('[data-testid="allowlist-entry"]').map(r => r.dataset.domain)).toEqual(['crates.io', 'docs.rs']))
    expect($('[data-testid="allowlist-add"]')).toBeNull()
  })

  it('refuses a destination with strong Signals and says why and that only the CLI can force it', async () => {
    const { api } = await openApp('/allowlist', {
      'GET /allowlist': ok([]),
      'POST /allowlist': fail(409, 'domain_suspicious'),
      'GET /watchlist/check?domain=webhook.site': ok({
        domain: 'webhook.site', port: 443, alert: true, strong: true, harmless: null, legitimate: null, feeds: {},
        signals: [{ signal: 'exfil', strong: true, suppressed: false, reason: 'Watchlist: webhook.site (Shai-Hulud 1)',
          code: 'watchlist', params: { domain: 'webhook.site', source: 'Shai-Hulud 1' } }],
      }),
    })
    expect(must('[data-testid="allowlist-empty"]').textContent).toContain('The Allowlist is empty.')
    await click('[data-testid="allowlist-add-open"]')
    await type('[data-testid="add-domain"]', 'webhook.site')
    await click('[data-testid="add-duration"] [data-duration="15min"]')
    await click('[data-testid="add-submit"]')

    const refusal = must('[data-testid="refusal"]')
    expect(refusal.textContent).toContain('webhook.site cannot be allowed here')
    expect(refusal.querySelector('[data-signal="exfil"]')!.textContent).toContain('Exfiltration')
    expect(refusal.textContent).toContain('Watchlist: webhook.site (Shai-Hulud 1)')
    expect(must('[data-testid="refusal-cli"]').textContent).toBe('sudo agentbox allow all webhook.site --force')
    // the form stays open; nothing was added
    expect($('[data-testid="allowlist-add"]')).not.toBeNull()
    expect(changes(api.calls).map(c => c.path)).toEqual(['/allowlist'])
  })

  it('extends, makes permanent, edits the note and removes only after confirmation', async () => {
    const { api } = await openApp('/allowlist', {
      'GET /allowlist': ok(entries()),
      'POST /allowlist/work/docs.rs/extend': ok({}),
      'POST /allowlist/work/docs.rs/make-permanent': ok({}),
      'PUT /allowlist/all/crates.io/note': ok({}),
      'DELETE /allowlist/all/crates.io': ok({ removed: true }),
    })
    await click(row('docs.rs').querySelector('[data-testid="entry-extend"]')!)
    await click('[data-testid="extend-choices"] [data-duration="1h"]')
    await click(row('docs.rs').querySelector('[data-testid="entry-permanent"]')!)
    await click('[data-testid="confirm-action"]')
    await click(row('crates.io').querySelector('[data-testid="entry-note-edit"]')!)
    await type('[data-testid="note-input"]', 'Rust crates and sparse index ')
    await click('[data-testid="note-save"]')

    await click(row('crates.io').querySelector('[data-testid="entry-remove"]')!)
    expect(document.body.textContent).toContain('Remove crates.io?')
    expect(document.body.textContent).toContain('open tunnels to it are closed')
    expect(changes(api.calls).some(c => c.method === 'DELETE')).toBe(false)
    await click('[data-testid="confirm-action"]')

    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/allowlist/work/docs.rs/extend', body: { duration: '1h' }, csrf: '1' },
      { method: 'POST', path: '/allowlist/work/docs.rs/make-permanent', body: undefined, csrf: '1' },
      { method: 'PUT', path: '/allowlist/all/crates.io/note', body: { note: 'Rust crates and sparse index' }, csrf: '1' },
      { method: 'DELETE', path: '/allowlist/all/crates.io', body: undefined, csrf: '1' },
    ])
    // each change reloads the list
    expect(api.calls.filter(c => c.method === 'GET' && c.path === '/allowlist')).toHaveLength(5)
  })

  it('cancelling the removal sends nothing', async () => {
    const { api } = await openApp('/allowlist', { 'GET /allowlist': ok(entries()) })
    await click(row('crates.io').querySelector('[data-testid="entry-remove"]')!)
    await click(must('[data-testid="confirm-cancel"]'))
    expect(changes(api.calls)).toEqual([])
  })

  it('shows an extension refused for strong Signals with the CLI command', async () => {
    await openApp('/allowlist', {
      'GET /allowlist': ok(entries()),
      'POST /allowlist/work/docs.rs/extend': fail(409, 'domain_suspicious'),
      'GET /watchlist/check?domain=docs.rs': ok({ domain: 'docs.rs', port: 443, alert: true, strong: true, harmless: null, legitimate: null, feeds: {}, signals: [] }),
    })
    await click(row('docs.rs').querySelector('[data-testid="entry-extend"]')!)
    await click('[data-testid="extend-choices"] [data-duration="7d"]')
    expect(must('[data-testid="refusal-cli"]').textContent).toBe('sudo agentbox extend work docs.rs --for 1h --force')
  })

  it('reloads when an Allowlist change arrives live (CLI, expiry timer)', async () => {
    const { api, es } = await openApp('/allowlist', { 'GET /allowlist': ok(entries()) })
    const loads = () => api.calls.filter(c => c.path === '/allowlist').length
    expect(loads()).toBe(1)
    es.emit('activity', { time: '2026-11-03T12:00:00Z', vm: 'work', actor: 'timer', kind: 'build', action: 'x', outcome: 'ok', description: '' })
    es.emit('activity', { time: '2026-11-03T12:00:00Z', vm: 'work', actor: 'timer', kind: 'allowlist', action: 'allowlist.expired', outcome: 'ok', description: '' })
    await waitFor(() => expect(loads()).toBe(2))
  })

  it('shows a failed load with its translated error', async () => {
    await openApp('/allowlist', { 'GET /allowlist': fail(502, 'journal_failed') })
    expect(must('[data-testid="allowlist-error"]').textContent).toContain('Reading the Host\'s journal failed.')
    await settle()
  })
})
