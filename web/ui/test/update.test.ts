// The Update of an Image (#104) on the System page: a new Build and, when it succeeds, a Reset of
// the VMs picked onto it. The dialog says what a new Build would bring and what happens (sessions
// end, Data disks stay, a failed Build resets nothing) before anything starts.
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { fakeApi, SIGNED_IN, vm } from './fakes'
import { byTestId, click, exists, openApp, setUp, submit, tearDown, text } from './vm-helpers'

beforeEach(setUp)
afterEach(tearDown)

const OK = (body: unknown = {}) => ({ status: 200, body })
const STARTED = { status: 202, body: { image: 'base', unit: 'agentbox-build@base.service', started: true, reset: [], message: 'gestartet' } }
const AVAILABLE = {
  checked: '2026-11-08T04:00:00Z', package_min_age_days: 2, orca_version: null,
  tools: { 'orca': '1.4.241', 'claude-code': '2.1.7', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': null },
}

function build(name: string, over: Record<string, unknown> = {}) {
  return {
    build: name, status: 'ok', step: 'done', started: '2026-11-07T03:00:00Z', finished: '2026-11-07T03:20:00Z',
    release: '0.1.0', commit: null, base_build: null, error: null, problems: [], actor: 'timer', current: false,
    used_by: [], pinned_by: [], updates: [], tools: {}, ...over,
  }
}

function images(over: Record<string, unknown> = {}) {
  return OK({
    builds_kept: 3, disks_readable: true, release: '0.2.0', available: AVAILABLE,
    images: [{
      name: 'base', base: true, in_release: true, current: '20261107-0300', vms: ['lab', 'priv', 'work'], running: null,
      package_updates: 4, updates: ['claude-code', 'packages'], update: true,
      builds: [
        build('20261107-0300', {
          current: true, used_by: ['priv', 'work'], updates: ['claude-code'],
          tools: { 'orca': '1.4.241', 'claude-code': '2.1.5', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': '44' },
        }),
        build('20261101-0300', { used_by: ['lab'], pinned_by: ['lab'] }),
      ],
      ...over,
    }],
  })
}

const checkbox = (name: string) => byTestId('update-dialog').find(`[data-testid="update-vm"][data-vm="${name}"]`)
async function toggle(name: string) {
  await checkbox(name).trigger('click')
  await flushPromises()
}
const disabled = (name: string) => {
  const el = checkbox(name).element
  return el.hasAttribute('disabled') || el.hasAttribute('data-disabled') || !!el.querySelector('[disabled], [data-disabled]')
}

async function openUpdate(routes: Record<string, unknown>) {
  const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': images(), ...routes } as never)
  await openApp('/system', [vm('work'), vm('priv'), vm('lab', 'stopped')])
  await click('image-update-open')
  return api
}

describe('updating an Image', () => {
  it('says what a new Build brings, resets only the VMs picked, and asks first', async () => {
    const api = await openUpdate({ 'POST /builds': { ...STARTED, body: { ...STARTED.body, reset: ['work'] } } })
    expect(text('update-changes')).toContain('Claude Code 2.1.5 → 2.1.7')
    expect(text('update-changes')).toContain('4 updates available')
    // nothing picked yet: only the Build
    expect(text('update-notice')).toContain('Only the Build runs')
    expect(text('confirm-ok')).toBe('Start the Build')
    // a VM pinned to a Build cannot be picked: its Reset would keep that Build
    expect(disabled('lab')).toBe(true)
    expect(disabled('work')).toBe(false)
    expect(byTestId('update-dialog').text()).toContain('Pinned to Build 20261101-0300')

    await toggle('work')
    await toggle('priv')
    await toggle('priv')       // and not after all
    expect(text('update-notice')).toContain('work will be reset onto it: sessions in them end and running agents stop')
    expect(text('update-notice')).toContain('Their Data disks stay')
    expect(text('update-notice')).toContain('If it fails, nothing is reset.')
    expect(text('confirm-ok')).toBe('Build, then reset one VM')
    expect(api.calls.some(c => c.method === 'POST')).toBe(false)

    await submit('update-dialog')
    const post = api.calls.find(c => c.method === 'POST' && c.path === '/builds')!
    expect(post.body).toEqual({ image: 'base', reset: ['work'] })
    expect(post.headers['x-agentbox']).toBe('1')
    expect(exists('update-dialog')).toBe(false)
  })

  it('without a VM picked it is a Build of the Image', async () => {
    const api = await openUpdate({ 'POST /builds': STARTED })
    await submit('update-dialog')
    expect(api.calls.find(c => c.method === 'POST')!.body).toEqual({ image: 'base' })
  })

  it('names every VM picked, in the order of the Image\'s VMs', async () => {
    const api = await openUpdate({ 'POST /builds': STARTED })
    await toggle('work')
    await toggle('priv')
    expect(text('update-notice')).toContain('priv, work will be reset')
    expect(text('confirm-ok')).toBe('Build, then reset 2 VMs')
    await submit('update-dialog')
    expect(api.calls.find(c => c.method === 'POST')!.body).toEqual({ image: 'base', reset: ['priv', 'work'] })
  })

  it('keeps the dialog open with the reason when it cannot start', async () => {
    await openUpdate({ 'POST /builds': { status: 409, body: { error: { code: 'vm_pinned', message: 'pinned' } } } })
    await toggle('work')
    await submit('update-dialog')
    expect(text('confirm-error')).toContain('pinned to a Build')
    expect(exists('update-dialog')).toBe(true)
    expect(exists('images-start-error')).toBe(false)
  })

  it('says when nothing new is known', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': images({ update: false, updates: [], package_updates: 0,
      builds: [build('20261107-0300', { current: true })] }) })
    await openApp('/system', [vm('work')])
    await click('image-update-open')
    expect(text('update-changes')).toContain('Nothing new by the last check')
  })

  it('says when it is not known', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': images({ update: null, updates: [], package_updates: null,
      builds: [build('20261107-0300', { current: true })] }) })
    await openApp('/system', [vm('work')])
    await click('image-update-open')
    expect(text('update-changes')).toContain('Not known yet')
  })

  it('is not offered while a Build runs', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': images({ running: { build: '20261108-0300', step: 'build', started: null } }) })
    await openApp('/system', [vm('work')])
    expect(byTestId('image-update-open').attributes('disabled')).toBeDefined()
  })

  it('in German', async () => {
    await openUpdate({ 'POST /builds': STARTED })
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    await toggle('work')
    expect(text('update-notice')).toContain('work darauf zurückgesetzt: Sitzungen darin enden')
    expect(text('update-notice')).toContain('Scheitert er, wird nichts zurückgesetzt.')
    expect(text('confirm-ok')).toBe('Bauen, dann eine VM zurücksetzen')
  })
})
