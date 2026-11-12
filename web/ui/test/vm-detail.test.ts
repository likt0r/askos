// The VM detail page against a fake backend: the overview follows the stream, the
// lifecycle actions send the right requests (with the CSRF header) and ask first where
// they destroy something, the Image dialog pins a Build, the memory limit says when it
// applies, create and delete keep the CLI's rules.
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeApi, SIGNED_IN, vm } from './fakes'
import { byTestId, click, exists, openApp, setUp, submit, tearDown, text, type } from './vm-helpers'

beforeEach(setUp)
afterEach(tearDown)

const OK = (body: unknown = {}) => ({ status: 200, body })
const WORK = vm('work', 'running', {
  address: '10.77.1.2', memory_mib: 26624, data_disk_gib: 300, github_owners: ['likt0r'], created: '2026-11-01T10:00:00Z',
  image: 'base', build_pin: null, image_build: '20261102-1200', next_build: '20261102-1200',
  system_disk: { since: '2026-10-25T09:00:00Z', age_days: 9, file: 'base-20261020.qcow2', image: 'base', build: '20261020' },
  reset_changes_build: true, reset_due: true,
  agents: { working: 1, waiting: 2 }, devices: [{ address: '192.0.2.36', connections: 2 }],
})
const HOST = { time: 't', readings: [], vms: { work: { memory_bytes: 12.5 * 1024 ** 3 } } }
const POSTPONED = OK({ entries: [{ action: 'reset.postponed', reason: 'agents-working', kind: 'reset', vm: 'work' }] })

describe('VM overview', () => {
  it('shows state, memory, agents, devices, System disk, Image and Build, and why a Reset waits', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /activity?kind=reset&vm=work&since=-14d&lines=1': POSTPONED })
    await openApp('/vms/work/web', [WORK], HOST)
    expect(text('overview-memory')).toContain('12.5 GiB in use of 26 GiB')
    expect(text('overview-agents')).toBe('1 working · 2 waiting')
    expect(text('overview-devices')).toContain('192.0.2.36')
    expect(text('overview-devices')).toContain('2 connections')
    const disk = text('overview-disk')
    expect(disk).toContain('9 days old')
    expect(disk).toContain('Reset due')
    expect(disk).toContain('from Build 20261020 of Image base')
    expect(text('overview-postponed')).toBe('Automatic Reset postponed: agents are working')
    expect(text('overview-pin')).toContain('none, always the current Build')
    expect(text('overview-next-reset')).toContain('20261102-1200')
    expect(text('overview-next-reset')).toContain('Reset changes the Build')
    expect(api.calls.some(c => c.path.startsWith('/activity'))).toBe(true)
  })

  it('follows vm events: state, buttons and pin change without reloading', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /activity?kind=reset&vm=work&since=-14d&lines=1': POSTPONED })
    const { es } = await openApp('/vms/work/web', [WORK])
    expect(byTestId('vm-detail').find('[data-testid="vm-state"]').attributes('data-state')).toBe('running')
    expect(exists('action-pause')).toBe(true)
    expect(exists('action-start')).toBe(false)

    es.emit('vm', { ...WORK, state: 'paused', build_pin: '20261020', next_build: '20261020', reset_changes_build: false, reset_due: false, agents: {} })
    await flushPromises()
    expect(byTestId('vm-detail').find('[data-testid="vm-state"]').attributes('data-state')).toBe('paused')
    expect(exists('action-pause')).toBe(false)
    expect(text('action-start')).toBe('Wake')
    expect(text('overview-pin')).toBe('20261020')
    expect(text('overview-next-reset')).toContain('the Build it runs now')
    expect(exists('overview-reset-due')).toBe(false)
    expect(exists('overview-postponed')).toBe(false)
    expect(text('overview-agents')).toBe('none')

    es.emit('vm', { ...WORK, state: 'stopped' })
    await flushPromises()
    expect(text('action-start')).toBe('Start')
  })

  it('can be folded away and remembers it', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    localStorage.removeItem('agentbox-vm-overview')
    await openApp('/vms/work/log', [{ ...WORK, reset_due: false }])
    expect(exists('vm-overview')).toBe(true)
    await click('overview-toggle')
    expect(exists('vm-overview')).toBe(false)
    expect(localStorage.getItem('agentbox-vm-overview')).toBe('0')
    localStorage.removeItem('agentbox-vm-overview')
  })
})

describe('a VM that does not respond (#54)', () => {
  const SINCE = '2026-11-04T18:15:02Z'
  const LAST = '2026-11-05T08:14:00Z'
  const STUCK = {
    ...WORK, state: 'not_responding', reset_due: false, agents: null,
    responding: { guest_agent: false, orca: false, since: SINCE },
    failed_wakes: { count: 12, last: LAST, address: '192.0.2.36' },
  }
  // as useWhen().at shows a time: without the date on the same day
  const at = (iso: string) => {
    const d = new Date(iso)
    const sameDay = d.toDateString() === new Date().toDateString()
    return new Intl.DateTimeFormat('en', sameDay ? { timeStyle: 'short' } : { dateStyle: 'medium', timeStyle: 'short' }).format(d)
  }

  it('says what does not answer, since when, the failed connections, and what to do', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await openApp('/vms/work/web', [STUCK])
    expect(byTestId('vm-detail').find('[data-testid="vm-state"]').text()).toBe('Not responding')
    const alert = byTestId('vm-not-responding')
    expect(alert.attributes('data-what')).toBe('system')
    expect(alert.text()).toContain('The system did not boot: neither Orca nor the guest agent answers.')
    expect(text('not-responding-since')).toBe(`No answer since ${at(SINCE)}.`)
    expect(text('not-responding-wakes')).toBe(`12 connection attempts failed, the last at ${at(LAST)}.`)
    expect(text('not-responding-hint')).toBe('Restart the VM first. If it stays like this, Reset it: the System disk is replaced, the Data disk stays.')
    // Restart and Reset, not Start
    expect(exists('action-restart')).toBe(true)
    expect(exists('action-reset')).toBe(true)
    expect(exists('action-start')).toBe(false)
    expect(exists('action-pause')).toBe(false)
  })

  it('tells a system that runs without Orca, and leaves out what it does not know', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await openApp('/vms/work/web', [{ ...STUCK, responding: { guest_agent: true, orca: false, since: null },
      failed_wakes: { count: 1, last: LAST, address: null } }])
    expect(byTestId('vm-not-responding').text()).toContain('The system runs, but Orca does not answer.')
    expect(exists('not-responding-since')).toBe(false)
    expect(text('not-responding-wakes')).toBe(`One connection attempt failed, at ${at(LAST)}.`)
  })

  it('in German', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/vms/work/web', [{ ...STUCK, failed_wakes: { count: 0, last: null, address: null } }])
    expect(byTestId('vm-detail').find('[data-testid="vm-state"]').text()).toBe('Antwortet nicht')
    expect(byTestId('vm-not-responding').text()).toContain('Das System ist nicht hochgefahren')
    expect(exists('not-responding-wakes')).toBe(false)
    expect(text('not-responding-hint')).toContain('Starte die VM zuerst neu.')
    await useNuxtApp().$i18n.setLocale('en')
  })

  it('goes away when the VM answers again; a starting VM has no hint but the same actions', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    const { es } = await openApp('/vms/work/web', [STUCK])
    expect(exists('vm-not-responding')).toBe(true)
    es.emit('vm', { ...STUCK, state: 'starting' })
    await flushPromises()
    expect(byTestId('vm-detail').find('[data-testid="vm-state"]').attributes('data-state')).toBe('starting')
    expect(exists('vm-not-responding')).toBe(false)
    expect([exists('action-restart'), exists('action-reset'), exists('action-start')]).toEqual([true, true, false])
    es.emit('vm', { ...STUCK, state: 'running', responding: { guest_agent: null, orca: true, since: null } })
    await flushPromises()
    expect(exists('vm-not-responding')).toBe(false)
    expect(exists('action-pause')).toBe(true)
  })

  it('starts the VM again after a Reset by default', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'POST /vms/work/reset': OK({ vm: 'work', state: 'running' }) })
    await openApp('/vms/work/web', [STUCK])
    await click('action-reset')
    await submit('reset-dialog')
    expect(api.calls.find(c => c.path === '/vms/work/reset')!.body).toEqual({ start: true })
  })
})

describe('lifecycle actions', () => {
  it('pauses with the CSRF header; the state comes from the stream', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'POST /vms/work/pause': OK({ vm: 'work', state: 'paused' }) })
    const { es } = await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('action-pause')
    const call = api.calls.find(c => c.path === '/vms/work/pause')!
    expect(call.method).toBe('POST')
    expect(call.headers['x-agentbox']).toBe('1')
    // still running until the Host says otherwise
    expect(exists('action-pause')).toBe(true)
    es.emit('vm', { ...WORK, state: 'paused', reset_due: false })
    await flushPromises()
    expect(exists('action-pause')).toBe(false)
  })

  it('starts a stopped VM', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'POST /vms/work/start': OK({ vm: 'work', state: 'running' }) })
    await openApp('/vms/work/web', [{ ...WORK, state: 'stopped', reset_due: false }])
    await click('action-start')
    expect(api.calls.filter(c => c.method === 'POST').map(c => c.path)).toEqual(['/vms/work/start'])
  })

  it('asks before a Reset, says what Build it brings, and can leave the VM off', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'POST /vms/work/reset': OK({ vm: 'work', state: 'shut off' }) })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('action-reset')
    expect(api.calls.some(c => c.path === '/vms/work/reset')).toBe(false)
    expect(text('reset-changes')).toBe('This Reset brings Build 20261102-1200 of Image base (now 20261020).')
    expect(text('reset-agents')).toBe('One agent is working right now.')
    await click('reset-start')        // untick "start after the Reset"
    await submit('reset-dialog')
    const call = api.calls.find(c => c.path === '/vms/work/reset')!
    expect(call.body).toEqual({ start: false })
    expect(exists('reset-dialog')).toBe(false)
  })

  it('shows a refused Reset in its dialog, translated', async () => {
    fakeApi({
      'GET /session': SIGNED_IN,
      'POST /vms/work/reset': { status: 409, body: { error: { code: 'image_has_no_build', message: 'x', detail: 'deutsch' } } },
    })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('action-reset')
    await submit('reset-dialog')
    expect(text('confirm-error')).toBe('The Image has no Build yet.')
    expect(exists('reset-dialog')).toBe(true)
  })

  it('asks before a Restart', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'POST /vms/work/restart': OK() })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('action-restart')
    expect(api.calls.some(c => c.path === '/vms/work/restart')).toBe(false)
    await click('confirm-cancel')
    expect(api.calls.some(c => c.path === '/vms/work/restart')).toBe(false)
    await click('action-restart')
    await submit('restart-dialog')
    expect(api.calls.filter(c => c.path === '/vms/work/restart')).toHaveLength(1)
  })

  it('focuses a VM, and offers Focus off while others are paused', async () => {
    const api = fakeApi({
      'GET /session': SIGNED_IN,
      'POST /vms/work/focus': OK({ focus: 'work', others: [{ vm: 'priv', state: 'paused' }] }),
      'DELETE /focus': OK({ focus: 'off', others: [] }),
    })
    const { es } = await openApp('/vms/work/web', [{ ...WORK, reset_due: false }, vm('priv', 'running', { network: 2 })])
    expect(exists('action-focus-off')).toBe(false)
    await click('action-focus')
    expect(api.calls.find(c => c.path === '/vms/work/focus')?.method).toBe('POST')
    expect(document.body.textContent).toContain('Paused: priv')
    es.emit('vm', vm('priv', 'paused', { network: 2 }))
    await flushPromises()
    await click('action-focus-off')
    const off = api.calls.find(c => c.path === '/focus')!
    expect([off.method, off.headers['x-agentbox']]).toEqual(['DELETE', '1'])
  })
})

describe('Image and Build pin', () => {
  const IMAGES = OK({ images: [
    { name: 'base', base: true, current: '20261102-1200', builds: [
      { build: '20261102-1200', status: 'ok', file: 'base-20261102-1200.qcow2', current: true, agents: { claude: '2.1.0' } },
      { build: '20261101-1200', status: 'failed', file: null },
      { build: '20261020', status: 'ok', file: 'base-20261020.qcow2' }] },
    { name: 'rust', current: '20261101-0900', builds: [{ build: '20261101-0900', status: 'ok', file: 'rust-20261101-0900.qcow2' }] },
    { name: 'cuda', current: null, builds: [] },
  ] })

  it('pins the VM to an earlier Build, applied at the next Reset', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': IMAGES, 'PUT /vms/work/image': OK({ vm: 'work', image: 'base', build: '20261020' }) })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('image-change')
    const dialog = byTestId('image-dialog')
    // Images without a Build cannot be chosen; failed Builds cannot be pinned
    expect(dialog.findAll('[data-testid="image-choice"] [role="radio"]').map(r => r.attributes('value'))).toEqual(['base', 'rust'])
    expect(dialog.findAll('[data-testid="build-choice"] [role="radio"]').map(r => r.attributes('value')))
      .toEqual(['__current__', '20261102-1200', '20261020'])
    expect(dialog.text()).toContain('Applies at the next Reset')
    expect(byTestId('image-save').attributes('disabled')).toBeDefined()     // nothing changed yet
    await dialog.find('[role="radio"][value="20261020"]').trigger('click')
    await flushPromises()
    await submit('image-dialog')
    const put = api.calls.find(c => c.method === 'PUT')!
    expect(put.path).toBe('/vms/work/image')
    expect(put.body).toEqual({ image: 'base', build: '20261020' })
  })

  it('moves to another Image at its current Build', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': IMAGES, 'PUT /vms/work/image': OK() })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false, build_pin: '20261020' }])
    await click('image-change')
    await byTestId('image-dialog').find('[role="radio"][value="rust"]').trigger('click')
    await flushPromises()
    expect(byTestId('build-choice').find('[role="radio"][value="__current__"]').attributes('aria-checked')).toBe('true')
    await submit('image-dialog')
    expect(api.calls.find(c => c.method === 'PUT')!.body).toEqual({ image: 'rust', build: null })
  })
})

describe('tool versions in the VM (#104)', () => {
  const AVAILABLE = {
    checked: '2026-11-08T04:00:00Z', package_min_age_days: 2, orca_version: null,
    tools: { 'orca': '1.4.245', 'claude-code': '2.1.7', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': null },
  }
  const IMAGES = OK({ available: AVAILABLE, images: [{ name: 'base', base: true, current: '20261102-1200', package_updates: 3, builds: [
    { build: '20261102-1200', status: 'ok', current: true, updates: ['orca', 'claude-code'],
      tools: { 'orca': '1.4.241', 'claude-code': '2.1.5', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': '44' } },
    { build: '20261020', status: 'ok', updates: ['orca', 'claude-code', 'opencode'],
      tools: { 'orca': '1.4.230', 'claude-code': '2.0.9', 'codex': null, 'opencode': '1.2.0', 'fedora': '44' } },
  ] }] })
  const rows = () => [...byTestId('overview-versions').element.querySelectorAll('[data-testid="tool-row"]')]

  it('shows the versions of the Build its System disk came from next to what is available', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': IMAGES })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    const versions = byTestId('overview-versions')
    expect(versions.text()).toContain('In this VM')
    expect(rows().map(r => r.querySelector('[data-testid="tool-installed"]')!.textContent!.trim()))
      .toEqual(['1.4.230', '2.0.9', 'unknown', '1.2.0', '44'])
    expect(rows().map(r => r.getAttribute('data-update'))).toEqual(['true', 'true', 'false', 'true', 'false'])
    expect(rows()[1]!.querySelector('[data-testid="tool-available"]')!.textContent).toContain('2.1.7')
    // package updates are counted for the Image's current Build, which this VM does not run yet
    expect(versions.find('[data-testid="tool-packages"]').exists()).toBe(false)
    expect(text('overview-versions-hint')).toBe('A new Build of Image base brings them at the VM\'s next Reset after it.')
  })

  it('counts the package updates of the current Build the VM runs', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': IMAGES })
    const disk = { since: '2026-11-02T13:00:00Z', age_days: 1, file: 'base-20261102-1200.qcow2', image: 'base', build: '20261102-1200' }
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false, reset_changes_build: false, system_disk: disk }])
    expect(byTestId('overview-versions').find('[data-testid="tool-packages"]').text()).toContain('3 updates available')
  })

  it('shows unknown when the Images cannot be read', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    expect(rows().map(r => r.querySelector('[data-testid="tool-installed"]')!.textContent!.trim())).toEqual(Array(5).fill('unknown'))
    expect(exists('overview-versions-hint')).toBe(false)
  })
})

describe('memory limit', () => {
  const SET = (applies: string | null) => OK({ vm: 'work', memory_mib: 32768, previous_mib: 26624, changed: applies !== null,
    state: applies === 'next_start' ? 'shut off' : 'running', applies, message: 'deutsch' })

  it('changes the limit of a running VM for its next Restart, which can follow right away', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'PUT /vms/work/memory': SET('next_restart'), 'POST /vms/work/restart': OK({ vm: 'work', state: 'running' }) })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('memory-change')
    expect(text('memory-current')).toContain('26624 MiB (= 26 GiB)')
    expect(text('memory-applies')).toBe('Applies at the next Restart or Reset. Until then the running VM keeps its memory.')
    expect(byTestId('memory-save').attributes('disabled')).toBeDefined()     // nothing changed yet
    await type('memory-input', '512')
    expect(byTestId('memory-dialog').text()).toContain('A whole number of MiB, at least 1024.')
    expect(byTestId('memory-save').attributes('disabled')).toBeDefined()
    await type('memory-input', '32768')
    expect(byTestId('memory-dialog').text()).toContain('= 32 GiB')
    await submit('memory-dialog')
    const put = api.calls.find(c => c.method === 'PUT')!
    expect(put.path).toBe('/vms/work/memory')
    expect(put.headers['x-agentbox']).toBe('1')
    expect(put.body).toEqual({ memory_mib: 32768 })
    expect(text('memory-saved-text')).toBe('Memory limit of work: 32 GiB. It applies at the next Restart.')
    expect(text('memory-agents')).toBe('One agent is working right now.')
    expect(api.calls.some(c => c.path === '/vms/work/restart')).toBe(false)
    await click('memory-restart')
    expect(api.calls.filter(c => c.path === '/vms/work/restart').map(c => [c.method, c.headers['x-agentbox']])).toEqual([['POST', '1']])
    expect(exists('memory-saved')).toBe(false)
  })

  it('can leave the Restart for later', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'PUT /vms/work/memory': SET('next_restart') })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('memory-change')
    await type('memory-input', '32768')
    await submit('memory-dialog')
    await click('memory-later')
    expect(exists('memory-saved')).toBe(false)
    expect(api.calls.some(c => c.path === '/vms/work/restart')).toBe(false)
  })

  it('applies to a stopped VM at its next start, without a Restart', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'PUT /vms/work/memory': SET('next_start') })
    await openApp('/vms/work/web', [{ ...WORK, state: 'stopped', reset_due: false }])
    await click('memory-change')
    expect(text('memory-applies')).toBe('Applies when the VM starts next.')
    await type('memory-input', '32768')
    await submit('memory-dialog')
    expect(exists('memory-dialog')).toBe(false)
    expect(exists('memory-saved')).toBe(false)
    expect(document.body.textContent).toContain('Memory limit of work: 32 GiB')
    expect(api.calls.some(c => c.path === '/vms/work/restart')).toBe(false)
  })

  it('is not changed while the VM is paused', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'PUT /vms/work/memory': SET('next_restart') })
    await openApp('/vms/work/web', [{ ...WORK, state: 'paused', reset_due: false }])
    await click('memory-change')
    expect(text('memory-paused')).toContain('Wake it first, then change the limit.')
    expect(exists('memory-applies')).toBe(false)
    await type('memory-input', '16384')
    expect(byTestId('memory-save').attributes('disabled')).toBeDefined()
    await submit('memory-dialog')
    expect(api.calls.some(c => c.method === 'PUT')).toBe(false)
  })

  it('shows agentbox\'s refusal translated and stays open', async () => {
    fakeApi({
      'GET /session': SIGNED_IN,
      'PUT /vms/work/memory': { status: 400, body: { error: { code: 'invalid_memory', message: 'x', field: 'memory_mib', detail: 'Speichergrenze' } } },
    })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('memory-change')
    await type('memory-input', '999999')
    await submit('memory-dialog')
    expect(text('memory-error')).toBe('The memory limit must be at least 1024 MiB and at most the Host\'s memory.')
    expect(exists('memory-dialog')).toBe(true)
  })
})

describe('Container image cleanup', () => {
  const SET = (enabled: boolean, days: number) => OK({ vm: 'work', container_image_cleanup: enabled, container_image_cleanup_days: days,
    previous: { container_image_cleanup: true, container_image_cleanup_days: 7 }, changed: true, message: 'deutsch' })
  const shown = () => text('overview-container-image-cleanup')
  const flip = async () => { await byTestId('container-image-cleanup-dialog').find('[role="switch"]').trigger('click'); await flushPromises() }
  const isOn = () => byTestId('container-image-cleanup-dialog').find('[role="switch"]').attributes('aria-checked')

  it('shows after N days or off, and on / 7 days when the VM object has no keys', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    const { es } = await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    expect(shown()).toBe('Container image cleanup: after 7 days')
    es.emit('vm', { ...WORK, reset_due: false, container_image_cleanup: true, container_image_cleanup_days: 14 })
    await flushPromises()
    expect(shown()).toBe('Container image cleanup: after 14 days')
    es.emit('vm', { ...WORK, reset_due: false, container_image_cleanup: false, container_image_cleanup_days: 14 })
    await flushPromises()
    expect(shown()).toBe('Container image cleanup: off')
  })

  it('shows the value in German', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false, container_image_cleanup: false }])
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    expect(shown()).toBe('Container-Images aufräumen: aus')
  })

  it('changes the days alone, checks them first, and sends only what changed', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'PUT /vms/work/container-image-cleanup': SET(true, 14) })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('container-image-cleanup-change')
    expect(isOn()).toBe('true')
    expect((byTestId('container-image-cleanup-days').element as HTMLInputElement).value).toBe('7')
    expect(byTestId('container-image-cleanup-save').attributes('disabled')).toBeDefined()    // nothing changed yet
    for (const bad of ['0', '366', 'x', '']) {
      await type('container-image-cleanup-days', bad)
      expect(text('container-image-cleanup-dialog')).toContain('A whole number of days from 1 to 365.')
      expect(byTestId('container-image-cleanup-save').attributes('disabled')).toBeDefined()
    }
    await type('container-image-cleanup-days', '14')
    await submit('container-image-cleanup-dialog')
    const put = api.calls.find(c => c.method === 'PUT')!
    expect(put.path).toBe('/vms/work/container-image-cleanup')
    expect(put.headers['x-agentbox']).toBe('1')
    expect(put.body).toEqual({ days: 14 })
    expect(exists('container-image-cleanup-dialog')).toBe(false)
    expect(document.body.textContent).toContain('Container image cleanup of work saved')
  })

  it('turns it off and keeps the days, which come back when it is turned on', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'PUT /vms/work/container-image-cleanup': SET(false, 14) })
    const { es } = await openApp('/vms/work/web', [{ ...WORK, reset_due: false, container_image_cleanup_days: 14 }])
    await click('container-image-cleanup-change')
    await flip()
    expect(isOn()).toBe('false')
    await submit('container-image-cleanup-dialog')
    expect(api.calls.find(c => c.method === 'PUT')!.body).toEqual({ enabled: false })
    es.emit('vm', { ...WORK, reset_due: false, container_image_cleanup: false, container_image_cleanup_days: 14 })
    await flushPromises()
    await click('container-image-cleanup-change')
    expect(isOn()).toBe('false')
    expect((byTestId('container-image-cleanup-days').element as HTMLInputElement).value).toBe('14')
  })

  it('sends both values when both changed', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'PUT /vms/work/container-image-cleanup': SET(false, 30) })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('container-image-cleanup-change')
    await flip()
    await type('container-image-cleanup-days', '30')
    await submit('container-image-cleanup-dialog')
    expect(api.calls.find(c => c.method === 'PUT')!.body).toEqual({ enabled: false, days: 30 })
  })

  it('shows the refusal by its code and stays open', async () => {
    fakeApi({
      'GET /session': SIGNED_IN,
      'PUT /vms/work/container-image-cleanup': { status: 400, body: { error: { code: 'invalid_cleanup_days', message: 'x', field: 'days', detail: 'Tage' } } },
    })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('container-image-cleanup-change')
    await type('container-image-cleanup-days', '30')
    await submit('container-image-cleanup-dialog')
    expect(text('container-image-cleanup-error')).toBe('The days for the Container image cleanup must be a whole number from 1 to 365.')
    expect(exists('container-image-cleanup-dialog')).toBe(true)
  })
})

describe('deleting a VM', () => {
  it('needs the typed name, archives the Data disk by default, and goes back to the list', async () => {
    const api = fakeApi({
      'GET /session': SIGNED_IN,
      'DELETE /vms/work': OK({ vm: 'work', data_disk: 'archived', archive: '/var/lib/agentbox/archive/work-data-x.qcow2', resumed: false }),
    })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('action-delete')
    expect(byTestId('confirm-ok', byTestId('delete-dialog').element).attributes('disabled')).toBeDefined()
    await type('confirm-typed', 'wor')
    await submit('delete-dialog')
    expect(api.calls.some(c => c.method === 'DELETE')).toBe(false)
    await type('confirm-typed', 'work')
    expect(byTestId('confirm-ok', byTestId('delete-dialog').element).attributes('disabled')).toBeUndefined()
    await submit('delete-dialog')
    const del = api.calls.find(c => c.method === 'DELETE')!
    expect(del.path).toBe('/vms/work')
    expect(del.body).toEqual({ confirm: 'work', delete_data_disk: false })
    await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/vms'))
    expect(document.body.textContent).toContain('Data disk archived on the Host: /var/lib/agentbox/archive/work-data-x.qcow2')
  })

  it('deletes the Data disk only when asked', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'DELETE /vms/work': OK({ vm: 'work', data_disk: 'deleted', archive: null }) })
    await openApp('/vms/work/web', [{ ...WORK, reset_due: false }])
    await click('action-delete')
    expect(byTestId('delete-dialog').text()).toContain('the Data disk (repositories, Orca data, agent transcripts) is archived')
    await click('delete-data-disk')
    expect(byTestId('delete-dialog').text()).toContain('deleted for good')
    await type('confirm-typed', 'work')
    await submit('delete-dialog')
    expect(api.calls.find(c => c.method === 'DELETE')!.body).toEqual({ confirm: 'work', delete_data_disk: true })
  })
})

describe('creating a VM', () => {
  const routes = (extra = {}) => ({
    'GET /session': SIGNED_IN,
    'GET /images': OK({ images: [
      { name: 'base', base: true, current: '20261102-1200', builds: [] },
      { name: 'rust', current: '20261101-0900', builds: [] },
      { name: 'cuda', current: null, builds: [] }] }),
    'GET /settings': OK({ settings: { default_image: { value: 'base' }, default_memory_mib: { value: 16384 }, default_data_disk_gib: { value: 200 } } }),
    ...extra,
  })

  it('keeps the CLI\'s name rules before anything is sent', async () => {
    const api = fakeApi(routes())
    await openApp('/vms', [WORK])
    await click('vm-create')
    const form = byTestId('create-dialog')
    expect(form.text()).toContain('Network and Orca port are assigned by the Host.')
    for (const [name, message] of [['Lab', 'Use 2 to 16 lowercase letters'], ['vms', 'This name is reserved.'],
      ['work', 'A VM of that name exists already.']] as const) {
      await type('create-name', name)
      expect(form.text()).toContain(message)
      expect(byTestId('create-submit').attributes('disabled')).toBeDefined()
    }
    await type('create-owners', 'likt0r bad/owner')
    expect(form.text()).toContain('Not a GitHub name: bad/owner')
    await type('create-name', 'lab')
    await type('create-memory', '512')
    expect(form.text()).toContain('at least 1024')
    await submit('create-dialog')
    expect(api.calls.some(c => c.method === 'POST')).toBe(false)
  })

  it('sends the VM with the Settings\' defaults filled in and opens it', async () => {
    const api = fakeApi(routes({ 'POST /vms': { status: 201, body: { vm: 'lab', network: 3, orca_port: 6803, image: 'base' } } }))
    const { es } = await openApp('/vms', [WORK])
    await click('vm-create')
    expect((byTestId('create-memory').element as HTMLInputElement).value).toBe('16384')
    expect(byTestId('create-dialog').text()).toContain('= 16 GiB')
    expect((byTestId('create-disk').element as HTMLInputElement).value).toBe('200')
    await type('create-name', 'lab')
    await type('create-owners', 'likt0r, acme')
    await submit('create-dialog')
    const post = api.calls.find(c => c.method === 'POST')!
    expect(post.path).toBe('/vms')
    expect(post.headers['x-agentbox']).toBe('1')
    expect(post.body).toEqual({ name: 'lab', image: 'base', memory_mib: 16384, data_disk_gib: 200,
      github_owners: ['likt0r', 'acme'], copy_allowlist_from: null })
    es.emit('state', { vms: [WORK, vm('lab', 'stopped', { network: 3 })], problems: [] })
    await flushPromises()
    await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/vms/lab/web'))
    expect(document.body.textContent).toContain('Network 3, Orca port 6803.')
  })

  it('shows agentbox\'s refusal translated', async () => {
    fakeApi(routes({ 'POST /vms': { status: 409, body: { error: { code: 'vm_leftovers', message: 'x', detail: 'Reste' } } } }))
    await openApp('/vms', [WORK])
    await click('vm-create')
    await type('create-name', 'lab')
    await submit('create-dialog')
    expect(text('create-error')).toBe('An earlier VM of that name left something behind.')
    expect(exists('create-dialog')).toBe(true)
  })
})
