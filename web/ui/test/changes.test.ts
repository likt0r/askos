// Changes through the resources module (#215): where a failed change appears, by its kind
// (utils/failures.ts, without a view), and through the app: at the form that sent it, as a toast
// that stays for one without a form, nowhere for one the view words itself or that the sign-in
// page or the connection notice says; Details shows the code and the Host's message, never its
// detail. A change and its own Activity entry read once.
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '~/utils/api'
import { failureDetails, failurePlace } from '~/utils/failures'
import { fakeApi, SIGNED_IN, vm } from './fakes'
import { byTestId, click, exists, openApp, setUp, tearDown } from './vm-helpers'

const OK = (body: unknown = {}) => ({ status: 200, body })
const refusal = (status: number, code: string, message = code, detail?: string) =>
  ({ status, body: { error: { code, message, ...(detail ? { detail } : {}) } } })
const OFFLINE = () => { throw new TypeError('Failed to fetch') }

describe('where a failed change appears (without a view)', () => {
  const refused = new ApiError(409, 'vm_running', 'The VM is running')
  const network = new ApiError(0, 'network', 'The Control Room cannot be reached')
  const signedOut = new ApiError(401, 'unauthenticated', 'Not signed in')

  it('a change sent from a form shows its failure there, a refusal too', () => {
    expect(failurePlace(refused, { form: true, live: true })).toBe('form')
    expect(failurePlace(new ApiError(502, 'agentbox_failed', 'x'), { form: true, live: true })).toBe('form')
    expect(failurePlace(new TypeError('a bug'), { form: true, live: true })).toBe('form')
  })

  it('a change without a form shows its failure as a toast', () => {
    expect(failurePlace(refused, { form: false, live: true })).toBe('toast')
    expect(failurePlace(new ApiError(502, 'agentbox_failed', 'x'), { form: false, live: true })).toBe('toast')
  })

  it('an ended session is the sign-in page\'s, an unreachable Control Room the connection notice\'s while it is down', () => {
    for (const form of [true, false]) {
      expect(failurePlace(signedOut, { form, live: true })).toBe('sign-in')
      expect(failurePlace(network, { form, live: false })).toBe('connection')
      // with the stream live the connection notice says nothing: the change's own place does
      expect(failurePlace(network, { form, live: true })).toBe(form ? 'form' : 'toast')
    }
  })

  it('Details are the code and the Host\'s message, never its detail', () => {
    const e = new ApiError(500, 'action_failed', 'agentbox failed', { detail: 'VM secret: hunter2' })
    expect(failureDetails(e)).toBe('action_failed: agentbox failed')
    expect(failureDetails(new ApiError(409, 'build_running', ''))).toBe('build_running')
    expect(failureDetails(network)).toBeNull()
    expect(failureDetails(new Error('not an answer'))).toBeNull()
  })
})

const WORK = vm('work')
const IMAGES = { images: [{ name: 'base', base: true, in_release: true, current: '2', vms: ['work'], running: null, builds: [
  { build: '2', status: 'ok', file: 'base-2.qcow2', current: true, error: null, problems: [], used_by: ['work'], pinned_by: [] }] }],
builds_kept: 3, release: '1.0.0', disks_readable: true }

function toasts() {
  return useState<{ title?: string, duration?: number }[]>('toasts').value
}

describe('a failed change through the app', () => {
  beforeEach(async () => {
    await setUp()
    useState('toasts').value = []
  })
  afterEach(() => {
    tearDown()
    useState('toasts').value = []
  })

  it('without a form: a toast that stays, with Details of the code and message, not the detail', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES),
      'POST /builds': refusal(502, 'action_failed', 'agentbox failed', 'VM secret: hunter2') })
    await openApp('/system', [WORK])
    await click('images-rebuild-all')
    expect(toasts().map(t => [t.title, t.duration])).toEqual([['The Build could not be started', 0]])
    await vi.waitFor(() => expect(exists('failure-text')).toBe(true))
    expect(byTestId('failure-text').text()).toContain('agentbox failed')
    expect(byTestId('fault-message').text()).toBe('action_failed: agentbox failed')
    expect(document.body.textContent).not.toContain('hunter2')
    expect(exists('images-start-error')).toBe(false)
  })

  it('a refusal the view words itself: beside its buttons, no toast', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'POST /builds': refusal(409, 'build_running') })
    await openApp('/system', [WORK])
    await click('images-rebuild-all')
    expect(byTestId('images-start-error').text()).toContain('A Build is running already.')
    expect(toasts()).toEqual([])
  })

  it('from a dialog: in the dialog, with Details, never a toast', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'POST /updates': refusal(409, 'update_running', 'An update runs') })
    await openApp('/system', [WORK])
    await click('images-update-now')
    await click('confirm-ok')
    const dialog = byTestId('update-now-dialog').element
    expect(dialog.querySelector('[data-testid="confirm-error"]')!.textContent).toContain('An update of the Images is running already.')
    expect(dialog.querySelector('[data-testid="fault-message"]')!.textContent).toBe('update_running: An update runs')
    expect(toasts()).toEqual([])
  })

  it('the Control Room out of reach while the connection is down: only the connection notice', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'POST /builds': OFFLINE })
    const { es } = await openApp('/system', [WORK])
    es.drop()
    await flushPromises()
    await click('images-rebuild-all')
    expect(toasts()).toEqual([])
    expect(exists('connection-notice')).toBe(true)
  })

  it('the Control Room out of reach while the stream is live: the toast says it', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'POST /builds': OFFLINE })
    await openApp('/system', [WORK])
    await click('images-rebuild-all')
    expect(toasts()).toHaveLength(1)
    await vi.waitFor(() => expect(exists('failure-text')).toBe(true))
    expect(exists('fault-details')).toBe(false)       // nothing from the Host to show
  })

  it('a change and its own Activity entry read the resource once', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'POST /updates': { status: 202, body: { started: true } } })
    const { es } = await openApp('/system', [WORK])
    const reads = () => api.calls.filter(c => c.method === 'GET' && c.path === '/images').length
    await click('images-update-now')
    await click('confirm-ok')
    expect(reads()).toBe(1)
    es.emit('activity', { time: 't', vm: null, actor: 'owner', kind: 'action', action: 'update-now', code: 'update-now', outcome: 'ok', description: '' })
    await flushPromises()
    expect(reads()).toBe(2)
  })

  it('a failure keeps the place it got when it failed, also after the connection came back', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'POST /updates': OFFLINE })
    const { es } = await openApp('/system', [WORK])
    await click('images-update-now')
    await click('confirm-ok')
    const dialog = () => byTestId('update-now-dialog').element
    expect(dialog().querySelector('[data-testid="confirm-error"]')!.textContent).toContain('cannot be reached')
    es.drop()
    es.emit('hello', { stream: 'ab12cd34', resumed: true })
    await flushPromises()
    expect(dialog().querySelector('[data-testid="confirm-error"]')!.textContent).toContain('cannot be reached')

    // failed while the connection was down: the connection notice said it, and it stays unsaid
    es.drop()
    await flushPromises()
    await click('confirm-ok')
    expect(dialog().querySelector('[data-testid="confirm-error"]')).toBeNull()
    es.emit('hello', { stream: 'ab12cd34', resumed: true })
    await flushPromises()
    expect(dialog().querySelector('[data-testid="confirm-error"]')).toBeNull()
  })

  it('in German', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'POST /builds': refusal(502, 'action_failed', 'agentbox failed') })
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/system', [WORK])
    await click('images-rebuild-all')
    expect(toasts().map(t => t.title)).toEqual(['Der Build konnte nicht gestartet werden'])
  })
})

describe('the GitHub App\'s changes', () => {
  const APPS = { apps: [{ id: 777, slug: 'askos', name: 'Askos', owner: 'likt0r', html_url: null, install_url: null, installations: [], error: null }], missing: [] }
  beforeEach(async () => {
    await setUp()
    useState('toasts').value = []
  })
  afterEach(() => {
    tearDown()
    useState('toasts').value = []
  })

  it('a refused remove shows in its confirm dialog with Details, no toast; the list is not read again by the view', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'GET /github-app': OK(APPS),
      'DELETE /github-app/777': refusal(409, 'github_app_in_use', 'The App is in use') })
    await openApp('/system', [WORK])
    const reads = () => api.calls.filter(c => c.method === 'GET' && c.path === '/github-app').length
    expect(reads()).toBe(1)
    await click('app-remove-open')
    await click('confirm-action')
    const body = byTestId('confirm-body').element
    expect(body.querySelector('[data-change="failed"]')).not.toBeNull()
    expect(body.querySelector('[data-testid="fault-message"]')!.textContent).toBe('github_app_in_use: The App is in use')
    expect(toasts()).toEqual([])
    expect(reads()).toBe(1)
  })

  it('a removed App is read again through its Activity entry, once', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'GET /github-app': OK(APPS),
      'DELETE /github-app/777': OK({ id: 777, removed: true }) })
    const { es } = await openApp('/system', [WORK])
    const reads = () => api.calls.filter(c => c.method === 'GET' && c.path === '/github-app').length
    await click('app-remove-open')
    await click('confirm-action')
    expect(reads()).toBe(1)
    es.emit('activity', { time: 't', vm: null, actor: 'owner', kind: 'credentials', action: 'remove-github-app', code: 'remove-github-app', outcome: 'ok', description: '' })
    await vi.waitFor(() => expect(reads()).toBe(2), { timeout: 2000 })
  })

  it('a key file too large replaces a refusal shown before, never both', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES), 'GET /github-app': OK(APPS),
      'PUT /github-app': refusal(422, 'github_app_rejected') })
    await openApp('/system', [WORK])
    await click('app-replace-open')
    await byTestId('app-key').setValue('-----BEGIN RSA PRIVATE KEY-----\nMIIEsecret\n-----END RSA PRIVATE KEY-----')
    await flushPromises()
    await click('app-replace-submit')
    await click('confirm-action')
    expect(document.body.querySelectorAll('[data-testid="app-replace-error"]')).toHaveLength(1)
    const input = byTestId('app-key-file').element as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [new File(['x'.repeat(9000)], 'big.pem')], configurable: true })
    input.dispatchEvent(new Event('change'))
    await flushPromises()
    const errors = document.body.querySelectorAll('[data-testid="app-replace-error"]')
    expect(errors).toHaveLength(1)
    expect(errors[0]!.textContent).toContain('That is not the App\'s private key')
    expect(errors[0]!.textContent).not.toContain('GitHub rejected')
  })
})
