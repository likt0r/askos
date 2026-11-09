// A VM's Access tab and System's GitHub App section against a fake backend: GitHub owners
// (missing Installations marked, edit), the Claude token (checked by the backend, never
// shown again), GitHub tokens and refused requests; the App's Installations, missing ones,
// the install link, replacing App ID and key (asked first, the key never shown again).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { FakeEventSource } from './fakes'
import { $, $$, changes, click, fail, must, ok, openApp, type, unmountAll, vm, waitFor } from './access-helpers'

const TOKEN = 'sk-ant-oat01-' + 'Q'.repeat(90)
const KEY = '-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretsecretsecret\n-----END RSA PRIVATE KEY-----\n'

const APP = {
  app: { id: 123456, slug: 'agentbox-titan', name: 'Agent Box titan', owner: 'likt0r', html_url: 'https://github.com/apps/agentbox-titan', install_url: 'https://github.com/apps/agentbox-titan/installations/new' },
  installations: [
    { id: 1, account: 'likt0r', type: 'User', repository_selection: 'all', suspended: false, html_url: 'https://github.com/settings/installations/1', vms: ['priv', 'work'] },
    { id: 2, account: 'old-org', type: 'Organization', repository_selection: 'selected', suspended: true, html_url: null, vms: [] },
  ],
  missing: [{ owner: 'acme-corp', vms: ['work'] }],
}

const TOKENS = {
  vm: 'work',
  repositories: [
    { repository: 'likt0r/askos', tokens: 5, last_token: '2026-11-03T11:00:00Z', refused: 1, last_refused: '2026-11-03T10:00:00Z' },
    { repository: 'evil/repo', tokens: 0, last_token: null, refused: 2, last_refused: '2026-11-03T09:00:00Z' },
  ],
  requests: [
    { time: '2026-11-03T11:00:00Z', outcome: 'issued', repository: 'likt0r/askos', request: 'likt0r/askos', reason: null, expires_at: '2026-11-03T12:00:00Z' },
    { time: '2026-11-03T10:00:00Z', outcome: 'refused', repository: 'evil/repo', request: 'evil/repo', reason: 'evil is not a GitHub owner of the VM work', reason_code: 'owner-not-allowed', expires_at: null },
    { time: '2026-11-03T09:00:00Z', outcome: 'error', repository: 'evil/repo', request: 'evil/repo', reason: 'TimeoutError(\'timed out\')', reason_code: null, expires_at: null },
  ],
  truncated: false,
  retention_days: 90,
}

function workVm(fields: Record<string, unknown> = {}) {
  return vm('work', 'running', { github_owners: ['likt0r', 'acme-corp'], claude_token_since: '2026-10-01T08:00:00Z', ...fields })
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

const ACCESS = {
  'GET /github-app': ok(APP),
  'GET /vms/work/github-tokens?since=-7d': ok(TOKENS),
  'GET /vms': ok({ vms: [workVm()], problems: [] }),
}

describe('Access tab', () => {
  it('shows the GitHub owners, marking those without an Installation, and saves changes', async () => {
    const { api } = await openApp('/vms/work/access', { ...ACCESS, 'PUT /vms/work/github-owners': ok({ vm: 'work', owners: ['likt0r', 'acme'] }) }, [workVm()])
    expect($$('[data-testid="owners-list"] [data-owner]').map(o => o.dataset.owner)).toEqual(['likt0r', 'acme-corp'])
    expect(must('[data-owner="acme-corp"]').querySelector('[data-testid="owner-missing"]')).not.toBeNull()
    expect(must('[data-owner="likt0r"]').querySelector('[data-testid="owner-missing"]')).toBeNull()

    await click('[data-testid="owners-edit"]')
    await click($$('[data-testid="owner-remove"]')[1]!)
    await type('[data-testid="owner-input"]', 'acme')
    await click('[data-testid="owners-save"]')
    expect(changes(api.calls)).toEqual([{ method: 'PUT', path: '/vms/work/github-owners', body: { owners: ['likt0r', 'acme'] }, csrf: '1' }])
  })

  it('stores a Claude token checked by the backend and never shows it again', async () => {
    const { api } = await openApp('/vms/work/access', { ...ACCESS, 'PUT /vms/work/claude-token': ok({ vm: 'work', stored: true, length: TOKEN.length }) }, [workVm()])
    expect(must('[data-testid="claude-since"]').textContent).toContain('A token is stored since')
    const input = must('[data-testid="claude-input"] input, input[data-testid="claude-input"]') as HTMLInputElement
    expect(input.type).toBe('password')
    await type('[data-testid="claude-input"]', TOKEN.slice(0, 50) + '\n' + TOKEN.slice(50))
    await click('[data-testid="claude-submit"]')

    expect(changes(api.calls)).toEqual([{ method: 'PUT', path: '/vms/work/claude-token', body: { token: TOKEN }, csrf: '1' }])
    expect(input.value).toBe('')
    expect(must('[data-testid="claude-stored"]').textContent).toContain(`Token valid and stored (${TOKEN.length} characters).`)
    expect(must('[data-testid="claude-stored"]').textContent).toContain('restart of work')
    expect(document.body.innerHTML).not.toContain(TOKEN.slice(13, 40))
  })

  it('says a refused Claude token changed nothing, and forgets it too', async () => {
    await openApp('/vms/work/access', { ...ACCESS, 'PUT /vms/work/claude-token': fail(422, 'claude_token_rejected') }, [workVm()])
    await type('[data-testid="claude-input"]', TOKEN)
    await click('[data-testid="claude-submit"]')
    const error = must('[data-testid="claude-error"]')
    expect(error.textContent).toContain('Anthropic rejected the token.')
    expect(error.textContent).toContain('The stored token stays in use.')
    expect((must('[data-testid="claude-input"] input, input[data-testid="claude-input"]') as HTMLInputElement).value).toBe('')
    expect(document.body.innerHTML).not.toContain(TOKEN.slice(13, 40))
  })

  it('refuses an API key before sending it', async () => {
    const { api } = await openApp('/vms/work/access', ACCESS, [workVm({ claude_token_since: null })])
    expect(must('[data-testid="claude-since"]').textContent).toContain('No Claude token stored.')
    await type('[data-testid="claude-input"]', 'hello')
    expect((must('[data-testid="claude-submit"]') as HTMLButtonElement).disabled).toBe(true)
    expect(changes(api.calls)).toEqual([])
  })

  it('lists the repositories the VM got tokens for and the refused requests', async () => {
    const { api, es } = await openApp('/vms/work/access', { ...ACCESS, 'GET /vms/work/github-tokens?since=-24h': ok({ ...TOKENS, repositories: [], requests: [] }) }, [workVm()])
    expect($$('[data-testid="token-repo"]').map(r => r.dataset.repository)).toEqual(['likt0r/askos'])
    expect(must('[data-testid="token-repo"]').textContent).toContain('5 tokens')
    expect(must('[data-testid="token-repo"]').textContent).toContain('one refused')
    const refused = $$('[data-testid="token-refused"]')
    expect(refused).toHaveLength(2)
    expect(refused[0]!.textContent).toContain('evil/repo')
    // why, worded from its code (#69), the token service's English text on hover; without a code that text
    const reason = refused[0]!.querySelector('[data-testid="token-reason"]')!
    expect(reason.textContent!.trim()).toBe('its owner is not a GitHub owner of the VM')
    expect(reason.getAttribute('title')).toBe('evil is not a GitHub owner of the VM work')
    expect(refused[1]!.querySelector('[data-testid="token-reason"]')!.textContent!.trim()).toBe('TimeoutError(\'timed out\')')

    // a new request arrives live
    const loads = () => api.calls.filter(c => c.path.startsWith('/vms/work/github-tokens')).length
    es.emit('github-token', { time: 't', vm: 'work', action: 'refused', outcome: 'refused', repository: 'x/y', request: 'x/y', reason: 'r', expires: null })
    await waitFor(() => expect(loads()).toBe(2))
    expect($('[data-testid="refused-none"]')).toBeNull()
  })
})

describe('GitHub App section', () => {
  it('shows the Installations, the VMs relying on each, missing ones and the install link', async () => {
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await openApp('/system', { 'GET /github-app': ok(APP) })
    const section = must('[data-testid="github-app"]')
    expect(must('[data-testid="app-info"]').textContent).toContain('Agent Box titan')
    expect(must('[data-testid="app-info"]').textContent).toContain('App ID 123456')
    expect($$('[data-testid="installation"]').map(i => i.dataset.account)).toEqual(['likt0r', 'old-org'])
    expect(must('[data-account="likt0r"]').textContent).toContain('VMs: priv, work')
    expect(must('[data-account="old-org"]').textContent).toContain('suspended')
    expect(must('[data-account="old-org"]').textContent).toContain('no VM relies on it')
    expect(must('[data-testid="app-missing"]').textContent).toContain('One missing Installation')
    expect(must('[data-testid="missing-owner"]').textContent).toContain('acme-corp')
    expect(must('[data-testid="missing-owner"]').textContent).toContain('needed by work')
    expect(must('[data-testid="install-url"]').textContent).toBe(APP.app.install_url)
    await click('[data-testid="install-copy"]')
    expect(writeText).toHaveBeenCalledWith(APP.app.install_url)
    expect(section.textContent).toContain('Copied')
  })

  it('replaces App ID and key only after confirmation; the key is gone from the page', async () => {
    const { api } = await openApp('/system', {
      'GET /github-app': ok(APP),
      'PUT /github-app': ok({ ...APP.app, id: 654321, stored: true }),
    })
    await click('[data-testid="app-replace-open"]')
    await type('[data-testid="app-id"]', '654321')
    await type('[data-testid="app-key"]', KEY)
    await click('[data-testid="app-replace-submit"]')
    expect(changes(api.calls)).toEqual([])
    expect(document.body.textContent).toContain('Replace the GitHub App?')
    await click('[data-testid="confirm-action"]')
    expect(changes(api.calls)).toEqual([{ method: 'PUT', path: '/github-app', body: { app_id: 654321, private_key: KEY }, csrf: '1' }])
    await waitFor(() => expect(must('[data-testid="app-replaced"]').textContent).toContain('GitHub App 654321 checked and stored.'))
    expect($('[data-testid="app-replace"]')).toBeNull()
    expect(document.body.innerHTML).not.toContain('MIIEsecret')
  })

  it('says a key GitHub rejected changed nothing, and forgets it', async () => {
    await openApp('/system', { 'GET /github-app': ok(APP), 'PUT /github-app': fail(422, 'github_app_rejected') })
    await click('[data-testid="app-replace-open"]')
    await type('[data-testid="app-key"]', KEY)
    await click('[data-testid="app-replace-submit"]')
    await click('[data-testid="confirm-action"]')
    const error = must('[data-testid="app-replace-error"]')
    expect(error.textContent).toContain('GitHub rejected the App ID or key. Nothing changed.')
    expect((must('[data-testid="app-key"] textarea, textarea[data-testid="app-key"]') as HTMLTextAreaElement).value).toBe('')
    expect(document.body.innerHTML).not.toContain('MIIEsecret')
  })

  it('says when no App is set up, or GitHub cannot be asked', async () => {
    await openApp('/system', { 'GET /github-app': fail(409, 'github_app_missing') })
    expect(must('[data-testid="app-error"]').textContent).toContain('No GitHub App is set up.')
    expect(must('[data-testid="app-replace-open"]')).toBeTruthy()
  })
})
