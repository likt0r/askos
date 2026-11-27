// A VM's Web tab against a fake backend: Open web (open for a time within the Settings'
// limit, close), blocked destinations with their Signals and "allow for <duration>" or "forbid
// for <duration>", the entries that apply to the VM and its Forbids.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { FakeEventSource } from './fakes'
import { $, $$, changes, click, fail, must, ok, openApp, settle, type, unmountAll, vm, waitFor } from './access-helpers'

const now = () => Math.floor(Date.now() / 1000)
const SETTINGS = ok({ settings: { open_web_max_minutes: { value: 60, default: 240, is_default: false, applies_later: null, allowed: '1-1440' } } })

function blocked() {
  return [
    { domain: 'paste.example', count: 3, last: now() - 120, reason: 'suspicious: paste,young', code: 'suspicious', params: { signals: 'paste, young' }, suspicious: ['paste', 'young'] },
    { domain: 'static.docs.rs', count: 1, last: now() - 3600, reason: 'not on the Allowlist', code: 'not-allowed', params: {}, suspicious: [] },
    { domain: 'pypi.org', count: 7, last: now() - 7200, reason: 'something new', code: null, params: {}, suspicious: [] },
    // a connection the proxy took no destination from: the VM's own address
    { domain: '10.77.1.2', count: 2, last: now() - 9000, reason: 'not a TLS handshake', code: 'not-tls', params: {}, suspicious: [] },
  ]
}

function vmForbids() {
  return [
    { domain: 'ads.example', scope: 'work', expires: null, note: 'tracking', added_by: 'root', added: now() - 60, expired: false, remaining: null, refused_24h: 2 },
    { domain: 'telemetry.example', scope: 'work', expires: now() + 600, note: '', added_by: 'root', added: now() - 60, expired: false, remaining: 600, refused_24h: 0 },
  ]
}

function vmEntries() {
  return [{ domain: 'docs.rs', scope: 'work', expires: now() + 3600, note: '', added_by: 'root', added: now() - 60, expired: false, remaining: 3600, uses_24h: 0 }]
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

function blockedRow(domain: string) {
  return must(`[data-testid="blocked-row"][data-domain="${domain}"]`)
}

const ROUTES = {
  'GET /settings': SETTINGS,
  'GET /vms/work/blocked': ok(blocked()),
  'GET /vms/work/allowlist': ok(vmEntries()),
  'GET /vms/work/forbids': ok([]),
  'GET /vms': ok({ vms: [vm('work')], problems: [] }),
}

/** The Activity entry of an Allowlist or Forbid change made in this browser. */
const allowEntry = (code: string) => ({ time: 't', vm: 'work', actor: 'owner', kind: 'allowlist', action: code, code, outcome: 'ok', description: '' })

describe('Web tab', () => {
  it('lists blocked destinations with Signals, marks those allowed by now, and the VM\'s entries', async () => {
    await openApp('/vms/work/web', ROUTES)
    expect($$('[data-testid="blocked-row"]').map(r => r.dataset.domain)).toEqual(['paste.example', 'static.docs.rs', 'pypi.org', '10.77.1.2'])
    const paste = blockedRow('paste.example')
    expect([...paste.querySelectorAll('[data-signal]')].map(b => b.textContent)).toEqual(['Paste service', 'Young domain'])
    expect(paste.textContent).toContain('3 times')
    expect(paste.textContent).toContain('last 2 min ago')
    // why, worded from the reason's code (#69); a reason without one as the proxy said it
    const reason = (domain: string) => blockedRow(domain).querySelector('[data-testid="blocked-reason"]')!
    expect(reason('paste.example').textContent).toBe('strong Signals: Paste service, Young domain')
    expect(reason('paste.example').getAttribute('title')).toBe('suspicious: paste,young')
    expect(reason('static.docs.rs').textContent).toBe('not on the Allowlist')
    expect(reason('pypi.org').textContent).toBe('something new')
    // docs.rs covers static.docs.rs: allowed by now, no allow button
    expect(blockedRow('static.docs.rs').querySelector('[data-testid="blocked-allowed"]')).not.toBeNull()
    expect(blockedRow('static.docs.rs').querySelector('[data-testid="blocked-allow"]')).toBeNull()
    // the VM's own address is nothing to allow
    expect(blockedRow('10.77.1.2').querySelector('[data-testid="blocked-allow"]')).toBeNull()
    expect(blockedRow('pypi.org').querySelector('[data-testid="blocked-allow"]')).not.toBeNull()
    expect($$('[data-testid="vm-entries"] [data-testid="allowlist-entry"]').map(r => r.dataset.domain)).toEqual(['docs.rs'])
  })

  it('allows a blocked destination for the VM for a chosen time', async () => {
    let entries = vmEntries()
    const { api, es } = await openApp('/vms/work/web', {
      ...ROUTES,
      'GET /vms/work/allowlist': () => ok(entries),
      'POST /allowlist': (call) => {
        const b = call.body as { domain: string, scope: string }
        entries = [...entries, { ...vmEntries()[0]!, domain: b.domain, scope: b.scope }]
        return { status: 201, body: entries.at(-1) }
      },
    })
    await click(blockedRow('pypi.org').querySelector('[data-testid="blocked-allow"]')!)
    const input = must('[data-testid="add-domain"] input, input[data-testid="add-domain"]') as HTMLInputElement
    expect(input.value).toBe('pypi.org')
    expect(input.disabled).toBe(true)
    expect(must('[data-testid="add-scope"]').textContent).toContain('Only work')
    await click('[data-testid="add-duration"] [data-duration="15min"]')
    await click('[data-testid="add-submit"]')
    expect(changes(api.calls)).toEqual([{ method: 'POST', path: '/allowlist', csrf: '1', body: { scope: 'work', domain: 'pypi.org', duration: '15min' } }])
    // both lists are read again through the change's own Activity entry
    es.emit('activity', allowEntry('allow'))
    await waitFor(() => expect(blockedRow('pypi.org').querySelector('[data-testid="blocked-allowed"]')).not.toBeNull())
  })

  it('forbids a blocked destination for the VM for a chosen time', async () => {
    let forbids: ReturnType<typeof vmForbids> = []
    const { api, es } = await openApp('/vms/work/web', {
      ...ROUTES,
      'GET /vms/work/forbids': () => ok(forbids),
      'POST /vms/work/forbids': (call) => {
        const b = call.body as { domain: string }
        forbids = [{ ...vmForbids()[1]!, domain: b.domain, expires: now() + 8 * 3600 }]
        return { status: 201, body: { ...forbids[0], allowlist_removed: false } }
      },
    })
    expect(must('[data-testid="forbids-empty"]').textContent).toContain('Nothing is forbidden for this VM.')
    // also where a parent domain's entry allows it (a Forbid of the subdomain is more specific),
    // never for the VM's own address
    expect(blockedRow('static.docs.rs').querySelector('[data-testid="blocked-forbid"]')).not.toBeNull()
    expect(blockedRow('10.77.1.2').querySelector('[data-testid="blocked-forbid"]')).toBeNull()
    await click(blockedRow('pypi.org').querySelector('[data-testid="blocked-forbid"]')!)
    expect(must('[data-testid="forbid-domain"]').textContent).toContain('pypi.org')
    // the durations of an Allowlist entry, permanent included
    expect([...must('[data-testid="forbid-duration"]').querySelectorAll('[data-duration]')].map(b => (b as HTMLElement).dataset.duration))
      .toEqual(['15min', '1h', '8h', '7d', 'permanent'])
    await click('[data-testid="forbid-duration"] [data-duration="8h"]')
    await click('[data-testid="forbid-submit"]')
    expect(changes(api.calls)).toEqual([{ method: 'POST', path: '/vms/work/forbids', csrf: '1', body: { domain: 'pypi.org', duration: '8h' } }])
    es.emit('activity', allowEntry('forbid'))
    await waitFor(() => expect(blockedRow('pypi.org').querySelector('[data-testid="blocked-forbidden"]')).not.toBeNull())
    expect(blockedRow('pypi.org').querySelector('[data-testid="blocked-forbid"]')).toBeNull()
    // still Allow…: an entry of the domain for the VM takes the Forbid's place
    expect(blockedRow('pypi.org').querySelector('[data-testid="blocked-allow"]')).not.toBeNull()
    expect($$('[data-testid="vm-forbids"] [data-testid="forbid-entry"]').map(r => r.dataset.domain)).toEqual(['pypi.org'])
  })

  it('names the VMs whose more specific Forbid still wins over a new entry', async () => {
    const { api } = await openApp('/vms/work/web', {
      ...ROUTES,
      'POST /allowlist': () => ({ status: 201, body: { ...vmEntries()[0]!, domain: 'pypi.org', scope: 'work', forbids_lifted: [], forbidden_for: [{ vm: 'work', domain: 'files.pypi.org' }] } }),
    })
    await click(blockedRow('pypi.org').querySelector('[data-testid="blocked-allow"]')!)
    await click('[data-testid="add-submit"]')
    expect(changes(api.calls).map(c => c.path)).toEqual(['/allowlist'])
    // the dialog stays open and says so, with nothing more to submit
    expect(must('[data-testid="add-still-forbidden"]').textContent).toContain('a more specific Forbid still wins for: work (files.pypi.org)')
    expect($('[data-testid="add-submit"]')).toBeNull()
  })

  it('words a block while the Forbids cannot be read', async () => {
    const unreadable = { domain: 'new.example', count: 1, last: now() - 5, reason: 'forbids unreadable', code: 'forbids-unreadable', params: {}, suspicious: [] }
    await openApp('/vms/work/web', { ...ROUTES, 'GET /vms/work/blocked': ok([unreadable]) })
    expect(blockedRow('new.example').querySelector('[data-testid="blocked-reason"]')!.textContent)
      .toBe('the Forbids cannot be read (the proxy refuses what no entry allows)')
    expect(blockedRow('new.example').querySelector('[data-testid="blocked-forbidden"]')).toBeNull()
  })

  it('marks what the more specific of an entry and a Forbid decides (ADR 0024)', async () => {
    const row = (domain: string) => ({ ...blocked()[1]!, domain })
    const entries = [
      { ...vmEntries()[0]!, domain: 'docs.rs', scope: 'all' },
      { ...vmEntries()[0]!, domain: 'api.static.docs.rs', scope: 'all' },
      { ...vmEntries()[0]!, domain: 'x.example', scope: 'all' },
    ]
    const forbids = [{ ...vmForbids()[0]!, domain: 'static.docs.rs' }, { ...vmForbids()[0]!, domain: 'x.example' }]
    await openApp('/vms/work/web', {
      ...ROUTES,
      'GET /vms/work/blocked': ok([row('static.docs.rs'), row('api.static.docs.rs'), row('x.example'), row('other.docs.rs')]),
      'GET /vms/work/allowlist': ok(entries),
      'GET /vms/work/forbids': ok(forbids),
    })
    const marks = (domain: string) => ({
      allowed: !!blockedRow(domain).querySelector('[data-testid="blocked-allowed"]'),
      forbidden: !!blockedRow(domain).querySelector('[data-testid="blocked-forbidden"]'),
    })
    // a Forbid of a subdomain beats the parent's entry; an entry of a subdomain beats the Forbid
    expect(marks('static.docs.rs')).toEqual({ allowed: false, forbidden: true })
    expect(marks('api.static.docs.rs')).toEqual({ allowed: true, forbidden: false })
    // at the same domain the VM's Forbid beats an entry for all VMs
    expect(marks('x.example')).toEqual({ allowed: false, forbidden: true })
    expect(marks('other.docs.rs')).toEqual({ allowed: true, forbidden: false })
    expect(blockedRow('x.example').querySelector('[data-testid="blocked-allow"]')).not.toBeNull()
    expect(blockedRow('api.static.docs.rs').querySelector('[data-testid="blocked-allow"]')).toBeNull()
  })

  it('lists the VM\'s Forbids, marks the destinations they cover, extends and removes one', async () => {
    const blockedForbidden = { domain: 'eu.ads.example', count: 4, last: now() - 30, reason: 'forbidden', code: 'forbidden', params: {}, suspicious: [] }
    const { api } = await openApp('/vms/work/web', {
      ...ROUTES,
      'GET /vms/work/blocked': ok([blockedForbidden, ...blocked()]),
      'GET /vms/work/forbids': ok(vmForbids()),
      'POST /vms/work/forbids/telemetry.example/extend': ok({}),
      'DELETE /vms/work/forbids/ads.example': ok({ scope: 'work', domain: 'ads.example', removed: true, message: 'x' }),
    })
    const row = blockedRow('eu.ads.example')
    expect(row.querySelector('[data-testid="blocked-reason"]')!.textContent).toBe('forbidden for this VM')
    expect(row.querySelector('[data-testid="blocked-forbidden"]')).not.toBeNull()
    expect(row.querySelector('[data-testid="blocked-forbid"]')).toBeNull()
    const rows = $$('[data-testid="forbid-entry"]')
    expect(rows.map(r => r.dataset.domain)).toEqual(['ads.example', 'telemetry.example'])
    expect(rows[0]!.querySelector('[data-testid="forbid-left"]')!.textContent).toBe('Permanent')
    expect(rows[0]!.querySelector('[data-testid="forbid-refused"]')!.textContent).toBe('refused 2 times in 24 h')
    expect(rows[0]!.querySelector('[data-testid="forbid-extend"]')).toBeNull()
    await click(rows[1]!.querySelector('[data-testid="forbid-extend"]')!)
    await click('[data-testid="forbid-extend-choices"] [data-duration="1h"]')
    await click(must('[data-testid="forbid-entry"][data-domain="ads.example"] [data-testid="forbid-remove"]'))
    await click('[data-testid="confirm-action"]')
    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/vms/work/forbids/telemetry.example/extend', csrf: '1', body: { duration: '1h' } },
      { method: 'DELETE', path: '/vms/work/forbids/ads.example', csrf: '1', body: undefined },
    ])
  })

  it('opens the web for a time within the longest Open web, then closes it', async () => {
    const until = now() + 3600
    const { api } = await openApp('/vms/work/web', {
      ...ROUTES,
      'POST /vms/work/open-web': ok({ vm: 'work', open_until: until, message: 'x' }),
      'DELETE /vms/work/open-web': ok({ vm: 'work', message: 'x' }),
    })
    const box = must('[data-testid="open-web"]')
    expect(box.dataset.open).toBe('false')
    // the Settings allow at most 60 minutes
    expect([...box.querySelectorAll('[data-minutes]')].map(b => (b as HTMLElement).dataset.minutes)).toEqual(['15', '30', '60'])
    await click(box.querySelector('[data-minutes="60"]')!)
    await click('[data-testid="open-web-open"]')
    expect(must('[data-testid="open-web"]').dataset.open).toBe('true')
    expect(must('[data-testid="open-web-state"]').textContent).toMatch(/Open until .* \((59 min|1 h) left\)/)
    await click('[data-testid="open-web-close"]')
    expect(must('[data-testid="open-web"]').dataset.open).toBe('false')
    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/vms/work/open-web', body: { minutes: 60 }, csrf: '1' },
      { method: 'DELETE', path: '/vms/work/open-web', body: undefined, csrf: '1' },
    ])
  })

  it('shows Open web from the VM object, and a failure to close it as a toast', async () => {
    useState('toasts').value = []
    const vms = [vm('work', 'running', { open_web_until: new Date(Date.now() + 20 * 60000).toISOString() })]
    await openApp('/vms/work/web', { ...ROUTES, 'DELETE /vms/work/open-web': fail(500, 'action_failed') }, vms)
    expect(must('[data-testid="open-web"]').dataset.open).toBe('true')
    expect(must('[data-testid="open-web-state"]').textContent).toMatch(/\((19|20) min left\)/)
    await click('[data-testid="open-web-close"]')
    // closing is one button, not a form: its failure is a toast that stays (#215)
    expect(useState<{ title?: string, duration?: number }[]>('toasts').value.map(t => [t.title, t.duration]))
      .toEqual([['Open web could not be closed', 0]])
    expect(must('[data-testid="open-web"]').querySelector('[data-change="failed"]')).toBeNull()
    expect(must('[data-testid="open-web"]').dataset.open).toBe('true')
  })

  it('shows a refused opening at its form, with Details', async () => {
    useState('toasts').value = []
    await openApp('/vms/work/web', { ...ROUTES, 'POST /vms/work/open-web': fail(400, 'invalid_minutes', 'Invalid minutes') })
    await click('[data-testid="open-web-open"]')
    const failure = must('[data-testid="open-web"] [data-change="failed"]')
    expect(failure.querySelector('[data-testid="fault-message"]')!.textContent).toBe('invalid_minutes: Invalid minutes')
    expect(useState<unknown[]>('toasts').value).toEqual([])
  })

  it('shows a refused Forbid in its dialog; a refused change of a Forbid in its own, cleared for the next', async () => {
    useState('toasts').value = []
    await openApp('/vms/work/web', { ...ROUTES,
      'GET /vms/work/forbids': ok(vmForbids()),
      'POST /vms/work/forbids': fail(409, 'forbid_exists', 'A Forbid exists'),
      'PUT /vms/work/forbids/ads.example/note': fail(400, 'invalid_note', 'Bad note') })
    await click(blockedRow('static.docs.rs').querySelector('[data-testid="blocked-forbid"]')!)
    await click('[data-testid="forbid-submit"]')
    expect(must('[data-testid="forbid-error"]').querySelector('[data-testid="fault-message"]')!.textContent).toBe('forbid_exists: A Forbid exists')
    document.body.querySelectorAll<HTMLElement>('button').forEach((b) => { if (b.textContent?.trim() === 'Cancel') b.click() })
    await settle()

    const ads = must('[data-testid="forbid-entry"][data-domain="ads.example"]')
    await click(ads.querySelector('[data-testid="forbid-note-edit"]')!)
    await type('[data-testid="forbid-note-input"]', 'x')
    await click('[data-testid="forbid-note-save"]')
    expect(must('[data-change="failed"] [data-testid="fault-message"]').textContent).toBe('invalid_note: Bad note')
    document.body.querySelectorAll<HTMLElement>('button').forEach((b) => { if (b.textContent?.trim() === 'Cancel') b.click() })
    await settle()
    await click(ads.querySelector('[data-testid="forbid-remove"]')!)
    expect($('[data-change="failed"]')).toBeNull()
    expect(useState<unknown[]>('toasts').value).toEqual([])
  })

  it('shows a held destination at once, with Allow…, and reloads when one is held', async () => {
    // #39: the proxy holds it for an entry; the row comes with the HELD line, before any block
    const held = { domain: 'new.example', count: 0, last: now() - 2, reason: 'not on the Allowlist', code: 'not-allowed', params: {}, suspicious: [], held_until: now() + 28 }
    const over = { ...blocked()[1]!, held_until: now() - 60 }
    const { api, es } = await openApp('/vms/work/web', { ...ROUTES, 'GET /vms/work/blocked': ok([held, over]) })
    const row = blockedRow('new.example')
    const left = Number(row.querySelector('[data-testid="blocked-held"]')!.textContent!.match(/held for (\d+) s more/)![1])
    expect(left).toBeGreaterThanOrEqual(20)
    expect(left).toBeLessThanOrEqual(28)
    expect(row.querySelector('[data-testid="blocked-allow"]')).not.toBeNull()
    expect(row.textContent).not.toContain('never')
    // a hold that is over shows no badge
    expect(blockedRow('static.docs.rs').querySelector('[data-testid="blocked-held"]')).toBeNull()
    const loads = () => api.calls.filter(c => c.path === '/vms/work/blocked').length
    expect(loads()).toBe(1)
    // this VM's event first, another VM's right after: the reload still comes
    es.emit('held', { time: '2026-11-03T12:00:00Z', vm: 'work', domain: 'other.example', seconds: 30 })
    es.emit('held', { time: '2026-11-03T12:00:00Z', vm: 'priv', domain: 'x.example', seconds: 30 })
    await waitFor(() => expect(loads()).toBe(2))
  })

  it('reloads the entries and Forbids on an Allowlist change, not on others, and after a reconnect without resume', async () => {
    const { api, es } = await openApp('/vms/work/web', ROUTES)
    const loads = () => [api.calls.filter(c => c.path === '/vms/work/allowlist').length, api.calls.filter(c => c.path === '/vms/work/forbids').length]
    expect(loads()).toEqual([1, 1])
    const entry = (kind: string, action: string) => ({ time: '2026-11-03T12:00:00Z', vm: 'work', actor: 'root', kind, action, code: action, outcome: 'ok', description: '' })
    es.emit('activity', entry('action', 'start'))
    await new Promise(r => setTimeout(r, 500))
    expect(loads()).toEqual([1, 1])
    es.emit('activity', entry('allowlist', 'forbid'))
    await waitFor(() => expect(loads()).toEqual([2, 2]))
    es.emit('hello', { stream: 'y', resumed: false })
    await waitFor(() => expect(loads()).toEqual([3, 3]))
  })

  it('reloads the blocked list on an Alert of this VM', async () => {
    const { api, es } = await openApp('/vms/work/web', ROUTES)
    const loads = () => api.calls.filter(c => c.path === '/vms/work/blocked').length
    expect(loads()).toBe(1)
    es.emit('alert', { time: '2026-11-03T12:00:00Z', vm: 'priv', domain: 'x.example', signals: ['young'] })
    es.emit('alert', { time: '2026-11-03T12:00:00Z', vm: 'work', domain: 'paste.example', signals: ['paste'] })
    await waitFor(() => expect(loads()).toBe(2))
    expect($('[data-testid="blocked-empty"]')).toBeNull()
  })

  it('reads the blocked list at most once a second through a burst of this VM\'s events', async () => {
    const { api, es } = await openApp('/vms/work/web', ROUTES)
    const loads = () => api.calls.filter(c => c.path === '/vms/work/blocked').length
    expect(loads()).toBe(1)
    for (let i = 0; i < 5; i++) es.emit('alert', { time: 't', vm: 'work', domain: `x${i}.example`, signals: ['young'] })
    await waitFor(() => expect(loads()).toBe(2))
    await new Promise(r => setTimeout(r, 300))
    expect(loads()).toBe(2)
  })

  it('takes Open web\'s longest duration from the Settings, and follows a change of them', async () => {
    let longest = 60
    const { es } = await openApp('/vms/work/web', { ...ROUTES, 'GET /settings': () => ok({ settings: { open_web_max_minutes: { value: longest } } }) })
    const minutes = () => [...must('[data-testid="open-web"]').querySelectorAll('[data-minutes]')].map(b => (b as HTMLElement).dataset.minutes)
    expect(minutes()).toEqual(['15', '30', '60'])
    longest = 240
    es.emit('activity', { time: 't', vm: null, actor: 'root', kind: 'settings', action: 'set-settings', code: 'set-settings', outcome: 'ok', description: '' })
    await waitFor(() => expect(minutes()).toEqual(['15', '30', '60', '120', '240']))
  })

  it('shows a failed read of the VM\'s entries with Retry', async () => {
    let failing = true
    await openApp('/vms/work/web', { ...ROUTES, 'GET /vms/work/allowlist': () => (failing ? fail(502, 'action_failed') : ok(vmEntries())) })
    const failure = must('[data-testid="vm-entries-error"]')
    expect(failure.textContent).toContain('Could not read the VM\'s Allowlist entries.')
    failing = false
    await click([...failure.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Retry')!)
    await settle()
    expect($('[data-testid="vm-entries-error"]')).toBeNull()
    expect(must('[data-testid="vm-entries"]').textContent).toContain('docs.rs')
  })

  it('reads the blocked list, the entries and the Forbids again whenever the tab opens (counts change with no event)', async () => {
    const { api } = await openApp('/vms/work/web', ROUTES)
    const loads = () => ['blocked', 'allowlist', 'forbids'].map(p => api.calls.filter(c => c.path === `/vms/work/${p}`).length)
    expect(loads()).toEqual([1, 1, 1])
    await useRouter().push('/vms/work/access')
    await settle()
    await useRouter().push('/vms/work/web')
    await settle()
    expect(loads()).toEqual([2, 2, 2])
  })

  it('shows a failed read of the blocked list and of the Forbids with Retry', async () => {
    await openApp('/vms/work/web', { ...ROUTES, 'GET /vms/work/blocked': fail(502, 'action_failed'), 'GET /vms/work/forbids': fail(502, 'action_failed') })
    expect(must('[data-testid="blocked-error"]').textContent).toContain('Could not read the blocked destinations.')
    expect(must('[data-testid="vm-forbids-error"]').textContent).toContain('Could not read the Forbids.')
  })
})
