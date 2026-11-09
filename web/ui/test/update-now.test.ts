// Update now (#114) on the System page: the version check at once, a Build of every Image with an
// update, then a Reset of the VMs picked onto the newest Build of their Image. The dialog is the one
// of the Update of an Image (#104): what a new Build would bring, which VMs to reset, what happens
// (sessions end, Data disks stay, a failed Build resets nothing), before anything starts.
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { fakeApi, SIGNED_IN, vm } from './fakes'
import { byTestId, exists, click, openApp, setUp, submit, tearDown, text } from './vm-helpers'

beforeEach(setUp)
afterEach(tearDown)

const OK = (body: unknown = {}) => ({ status: 200, body })
const STARTED = { status: 202, body: { unit: 'agentbox-build@update.service', started: true, reset: [], message: 'gestartet' } }
const AVAILABLE = {
  checked: '2026-11-08T02:00:00Z', package_min_age_days: 2, orca_version: null,
  tools: { 'orca': '1.4.241', 'claude-code': '2.1.7', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': null },
}
const TOOLS = { 'orca': '1.4.241', 'claude-code': '2.1.5', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': '44' }

function build(name: string, over: Record<string, unknown> = {}) {
  return {
    build: name, status: 'ok', step: 'done', started: '2026-11-07T03:00:00Z', finished: '2026-11-07T03:20:00Z',
    release: '0.1.0', commit: null, base_build: null, error: null, problems: [], actor: 'timer', current: false,
    used_by: [], pinned_by: [], updates: [], tools: {}, ...over,
  }
}

/** base has an update, rust nothing new, go no Build yet. */
function images(over: Record<string, unknown> = {}) {
  return OK({
    builds_kept: 3, disks_readable: true, release: '0.2.0', available: AVAILABLE, updating: false,
    images: [
      {
        name: 'base', base: true, in_release: true, current: '20261107-0300', vms: ['lab', 'work'], running: null,
        package_updates: 4, updates: ['claude-code', 'packages'], update: true,
        builds: [
          build('20261107-0300', { current: true, used_by: ['work'], updates: ['claude-code'], tools: TOOLS }),
          build('20261101-0300', { used_by: ['lab'], pinned_by: ['lab'] }),
        ],
      },
      {
        name: 'rust', base: false, in_release: true, current: '20261107-0400', vms: ['priv'], running: null,
        package_updates: null, updates: [], update: false,
        builds: [build('20261107-0400', { current: true, used_by: ['priv'], tools: { ...TOOLS, 'claude-code': '2.1.7' } })],
      },
      {
        name: 'go', base: false, in_release: true, current: null, vms: [], running: null,
        package_updates: null, updates: [], update: null, builds: [],
      },
    ],
    ...over,
  })
}

const dialog = () => byTestId('update-now-dialog')
const checkbox = (name: string) => dialog().find(`[data-testid="update-vm"][data-vm="${name}"]`)
async function toggle(name: string) {
  await checkbox(name).trigger('click')
  await flushPromises()
}
const disabled = (name: string) => {
  const el = checkbox(name).element
  return el.hasAttribute('disabled') || el.hasAttribute('data-disabled') || !!el.querySelector('[disabled], [data-disabled]')
}

async function openUpdateNow(routes: Record<string, unknown>) {
  const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': images(), ...routes } as never)
  await openApp('/system', [vm('work'), vm('priv'), vm('lab', 'stopped')])
  await click('images-update-now')
  return api
}

describe('Update now', () => {
  it('says what each Image would bring, offers the VMs of every Image, and asks first', async () => {
    const api = await openUpdateNow({ 'POST /updates': STARTED })
    const changes = text('update-changes')
    expect(changes).toContain('Claude Code 2.1.5 → 2.1.7')
    expect(changes).toContain('4 updates available')
    expect(dialog().find('[data-image="rust"]').text()).toContain('Nothing new: not built unless the check finds something.')
    expect(changes).toContain('The check runs again first')
    // an Image without a Build is not offered
    expect(dialog().find('[data-image="go"]').exists()).toBe(false)
    // nothing picked yet: only the check and the Builds
    expect(text('update-notice')).toContain('Only the check and the Builds run')
    expect(text('confirm-ok')).toBe('Start the update')
    // a VM pinned to a Build cannot be picked; the others say their Image
    expect(disabled('lab')).toBe(true)
    expect(disabled('priv')).toBe(false)
    expect(dialog().text()).toContain('Pinned to Build 20261101-0300')
    expect(dialog().text()).toContain('Image rust')

    await toggle('priv')
    await toggle('work')
    expect(text('update-notice')).toContain('work, priv will be reset onto the newest Build of their Image: sessions in them end and running agents stop')
    expect(text('update-notice')).toContain('Their Data disks stay')
    expect(text('update-notice')).toContain('If a Build fails, nothing is reset.')
    expect(text('confirm-ok')).toBe('Update, then reset 2 VMs')
    expect(api.calls.some(c => c.method === 'POST')).toBe(false)

    await submit('update-now-dialog')
    const post = api.calls.find(c => c.method === 'POST' && c.path === '/updates')!
    expect(post.body).toEqual({ reset: ['work', 'priv'] })
    expect(post.headers['x-agentbox']).toBe('1')
    expect(exists('update-now-dialog')).toBe(false)
  })

  it('without a VM picked it checks and builds only', async () => {
    const api = await openUpdateNow({ 'POST /updates': STARTED })
    await submit('update-now-dialog')
    expect(api.calls.find(c => c.method === 'POST')!.body).toEqual({})
  })

  it('keeps the dialog open with the reason when it cannot start', async () => {
    await openUpdateNow({ 'POST /updates': { status: 409, body: { error: { code: 'update_running', message: 'running' } } } })
    await toggle('work')
    await submit('update-now-dialog')
    expect(text('confirm-error')).toContain('An update of the Images is running already.')
    expect(exists('update-now-dialog')).toBe(true)
    expect(exists('images-start-error')).toBe(false)
  })

  it('is not offered while an update runs, and says that one does', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': images({ updating: true }) })
    await openApp('/system', [vm('work')])
    expect(byTestId('images-update-now').attributes('disabled')).toBeDefined()
    expect(byTestId('image-update-open').attributes('disabled')).toBeDefined()
    expect(text('images-updating')).toContain('An update is running')
  })

  it('follows the unit of the update in the services event, and reloads when it ends', async () => {
    let reads = 0
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => { reads++; return images() } } as never)
    const { es } = await openApp('/system', [vm('work')])
    expect(exists('images-updating')).toBe(false)
    const unit = (active: string) => ({ units: [{ unit: 'agentbox-build@update.service', load: 'loaded', active, sub: active === 'active' ? 'running' : 'dead', description: '' }] })
    es.emit('services', unit('active'))
    await flushPromises()
    expect(exists('images-updating')).toBe(true)
    expect(byTestId('images-update-now').attributes('disabled')).toBeDefined()
    const before = reads
    es.emit('services', unit('inactive'))
    await flushPromises()
    expect(exists('images-updating')).toBe(false)
    expect(reads).toBeGreaterThan(before)
  })

  it('in German', async () => {
    await openUpdateNow({ 'POST /updates': STARTED })
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    await toggle('work')
    expect(text('update-notice')).toContain('werden work auf den neuesten Build ihres Images zurückgesetzt: Sitzungen darin enden')
    expect(text('update-notice')).toContain('Scheitert ein Build, wird nichts zurückgesetzt.')
    expect(text('confirm-ok')).toBe('Aktualisieren, dann eine VM zurücksetzen')
  })
})
