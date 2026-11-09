// A VM's Web tab against a fake backend: Open web (open for a time within the Settings'
// limit, close), blocked destinations with their Signals and "allow for <duration>", the
// entries that apply to the VM.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { FakeEventSource } from './fakes'
import { $, $$, changes, click, fail, must, ok, openApp, unmountAll, vm, waitFor } from './access-helpers'

const now = () => Math.floor(Date.now() / 1000)
const SETTINGS = ok({ settings: { open_web_max_minutes: { value: 60, default: 240, is_default: false, applies_later: null, allowed: '1-1440' } } })

function blocked() {
  return [
    { domain: 'paste.example', count: 3, last: now() - 120, reason: 'suspicious: paste,young', code: 'suspicious', params: { signals: 'paste, young' }, suspicious: ['paste', 'young'] },
    { domain: 'static.docs.rs', count: 1, last: now() - 3600, reason: 'not on the Allowlist', code: 'not-allowed', params: {}, suspicious: [] },
    { domain: 'pypi.org', count: 7, last: now() - 7200, reason: 'something new', code: null, params: {}, suspicious: [] },
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
  'GET /vms': ok({ vms: [vm('work')], problems: [] }),
}

describe('Web tab', () => {
  it('lists blocked destinations with Signals, marks those allowed by now, and the VM\'s entries', async () => {
    await openApp('/vms/work/web', ROUTES)
    expect($$('[data-testid="blocked-row"]').map(r => r.dataset.domain)).toEqual(['paste.example', 'static.docs.rs', 'pypi.org'])
    const paste = blockedRow('paste.example')
    expect([...paste.querySelectorAll('[data-signal]')].map(b => b.textContent)).toEqual(['Paste service', 'Young domain'])
    expect(paste.textContent).toContain('3 times')
    expect(paste.textContent).toContain('last 2 min ago')
    // why, worded from the reason's code (#69); a reason without one as the proxy said it
    const reason = (domain: string) => blockedRow(domain).querySelector('[data-testid="blocked-reason"]')!
    expect(reason('paste.example').textContent).toBe('strong Signals in Open web: Paste service, Young domain')
    expect(reason('paste.example').getAttribute('title')).toBe('suspicious: paste,young')
    expect(reason('static.docs.rs').textContent).toBe('not on the Allowlist')
    expect(reason('pypi.org').textContent).toBe('something new')
    // docs.rs covers static.docs.rs: allowed by now, no allow button
    expect(blockedRow('static.docs.rs').querySelector('[data-testid="blocked-allowed"]')).not.toBeNull()
    expect(blockedRow('static.docs.rs').querySelector('[data-testid="blocked-allow"]')).toBeNull()
    expect($$('[data-testid="vm-entries"] [data-testid="allowlist-entry"]').map(r => r.dataset.domain)).toEqual(['docs.rs'])
  })

  it('allows a blocked destination for the VM for a chosen time', async () => {
    let entries = vmEntries()
    const { api } = await openApp('/vms/work/web', {
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
    await waitFor(() => expect(blockedRow('pypi.org').querySelector('[data-testid="blocked-allowed"]')).not.toBeNull())
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

  it('shows Open web from the VM object and a refusal of the time', async () => {
    const vms = [vm('work', 'running', { open_web_until: new Date(Date.now() + 20 * 60000).toISOString() })]
    await openApp('/vms/work/web', { ...ROUTES, 'DELETE /vms/work/open-web': fail(500, 'action_failed') }, vms)
    expect(must('[data-testid="open-web"]').dataset.open).toBe('true')
    expect(must('[data-testid="open-web-state"]').textContent).toMatch(/\((19|20) min left\)/)
    await click('[data-testid="open-web-close"]')
    expect(must('[data-testid="open-web"]').textContent).toContain('agentbox reported an error.')
    expect(must('[data-testid="open-web"]').dataset.open).toBe('true')
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
})
