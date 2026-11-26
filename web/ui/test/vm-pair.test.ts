// The Pair tab: Paired devices with their times, the Observer marked and never
// removable, the Pairing code only after an explicit confirmation (and hidden again),
// revoking all pairings only after typing the VM's name.
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeApi, SIGNED_IN, vm } from './fakes'
import { byTestId, click, exists, openApp, setUp, submit, tearDown, text, type } from './vm-helpers'

beforeEach(setUp)
afterEach(() => {
  vi.useRealTimers()
  tearDown()
})

const CODE = 'orca://pair?code=Zq3_secret-CODE'
const DEVICES = {
  vm: 'work',
  devices: [
    { id: 'dev-laptop', name: 'Laptop', scope: 'mobile', paired_at: '2026-11-02T08:00:00Z', last_seen_at: '2026-11-03T11:00:00Z', observer: false, removable: false },
    { id: 'dev-titan', name: 'titan', scope: 'runtime', paired_at: '2026-10-01T08:00:00Z', last_seen_at: null, observer: true, removable: false },
  ],
  observer_known: true,
  single_removal: false,
}

describe('Pair tab', () => {
  it('lists Paired devices, marks the Observer, and explains why none can be removed alone', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /vms/work/pairings': { status: 200, body: DEVICES } })
    await openApp('/vms/work/pair', [vm('work')])
    const rows = byTestId('pair-devices').findAll('[data-testid="pair-device"]')
    expect(rows.map(r => r.attributes('data-device'))).toEqual(['dev-laptop', 'dev-titan'])
    expect(rows[0]!.text()).toContain('Laptop')
    expect(rows[0]!.text()).toMatch(/paired .*2026/)
    expect(rows[0]!.text()).toMatch(/last seen .*2026/)
    expect(rows[0]!.find('[data-testid="pair-observer"]').exists()).toBe(false)
    expect(rows[1]!.find('[data-testid="pair-observer"]').text()).toBe('Observer (this Host)')
    expect(rows[1]!.text()).toContain('never seen')
    expect(exists('pair-remove')).toBe(false)
    expect(text('pair-single-note')).toContain('Orca offers no way for Agent Box to remove one Paired device')
  })

  it('offers removing a single device only where agentbox says it can, never the Observer', async () => {
    const api = fakeApi({
      'GET /session': SIGNED_IN,
      'GET /vms/work/pairings': { status: 200, body: { ...DEVICES, single_removal: true,
        devices: DEVICES.devices.map(d => ({ ...d, removable: true })) } },
      'POST /vms/work/pairings/remove': { status: 200, body: {} },
    })
    await openApp('/vms/work/pair', [vm('work')])
    expect(document.querySelectorAll('[data-testid="pair-remove"]')).toHaveLength(1)
    expect(exists('pair-single-note')).toBe(false)
    await click('pair-remove', byTestId('pair-devices').element.querySelector('[data-device="dev-laptop"]')!)
    expect(api.calls.some(c => c.method === 'POST')).toBe(false)
    await submit('remove-dialog')
    expect(api.calls.find(c => c.method === 'POST')!.body).toEqual({ device: 'dev-laptop' })
  })

  it('reveals the Pairing code only after confirming, and hides it again', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const api = fakeApi({
      'GET /session': SIGNED_IN,
      'GET /vms/work/pairings': { status: 200, body: DEVICES },
      'POST /vms/work/pairing-code': { status: 200, body: { vm: 'work', code: CODE } },
    })
    await openApp('/vms/work/pair', [vm('work')])
    expect(document.body.innerHTML).not.toContain(CODE)
    await click('pair-reveal')
    expect(document.body.textContent).toContain('Make sure nobody watches your screen')
    await click('confirm-cancel')
    expect(api.calls.some(c => c.path === '/vms/work/pairing-code')).toBe(false)

    await click('pair-reveal')
    await submit('reveal-dialog')
    const post = api.calls.find(c => c.path === '/vms/work/pairing-code')!
    expect([post.method, post.headers['x-agentbox'], post.body]).toEqual(['POST', '1', { confirm: true }])
    expect(text('pair-code')).toBe(CODE)

    await click('pair-hide')
    expect(document.body.innerHTML).not.toContain(CODE)

    await click('pair-reveal')
    await submit('reveal-dialog')
    expect(text('pair-code')).toBe(CODE)
    vi.advanceTimersByTime(120000)
    await flushPromises()
    expect(document.body.innerHTML).not.toContain(CODE)
  })

  it('forgets the code when leaving the tab', async () => {
    fakeApi({
      'GET /session': SIGNED_IN,
      'GET /vms/work/pairings': { status: 200, body: DEVICES },
      'POST /vms/work/pairing-code': { status: 200, body: { vm: 'work', code: CODE } },
      'GET /vms/work/logs?boot=current&lines=200': { status: 200, body: { vm: 'work', entries: [], cursor: null, truncated: false } },
    })
    await openApp('/vms/work/pair', [vm('work')])
    await click('pair-reveal')
    await submit('reveal-dialog')
    expect(text('pair-code')).toBe(CODE)
    await useRouter().push('/vms/work/log')
    await flushPromises()
    await useRouter().push('/vms/work/pair')
    await flushPromises()
    expect(exists('pair-view')).toBe(true)
    expect(document.body.innerHTML).not.toContain(CODE)
  })

  it('says why a code is not there yet', async () => {
    fakeApi({
      'GET /session': SIGNED_IN,
      'GET /vms/work/pairings': { status: 200, body: DEVICES },
      'POST /vms/work/pairing-code': { status: 503, body: { error: { code: 'pairing_code_unavailable', message: 'x' } } },
    })
    await openApp('/vms/work/pair', [vm('work')])
    await click('pair-reveal')
    await submit('reveal-dialog')
    expect(byTestId('confirm-error').find('[data-testid="failure-text"] p').text()).toBe('Orca has no Pairing code yet. Try again shortly.')
    expect(exists('pair-code')).toBe(false)
  })

  it('revokes all pairings only after the VM\'s name is typed', async () => {
    let revoked = false
    const api = fakeApi({
      'GET /session': SIGNED_IN,
      'GET /vms/work/pairings': () => ({ status: 200, body: revoked ? { ...DEVICES, devices: [DEVICES.devices[1]] } : DEVICES }),
      'POST /vms/work/pairings/revoke': () => {
        revoked = true
        return { status: 200, body: { vm: 'work', observer_repaired: true, message: 'x' } }
      },
    })
    const { es } = await openApp('/vms/work/pair', [vm('work')])
    await click('pair-revoke')
    await submit('revoke-dialog')
    expect(api.calls.some(c => c.path.endsWith('/revoke'))).toBe(false)
    await type('confirm-typed', 'work')
    await submit('revoke-dialog')
    const post = api.calls.find(c => c.path.endsWith('/revoke'))!
    expect(post.body).toEqual({ confirm: 'work' })
    await flushPromises()
    expect(document.body.textContent).toContain('The Host is the Observer again.')
    // the list is read again through the change's own Activity entry, not by the view
    const reads = () => api.calls.filter(c => c.method === 'GET' && c.path === '/vms/work/pairings').length
    expect(reads()).toBe(1)
    expect(byTestId('pair-devices').findAll('[data-testid="pair-device"]')).toHaveLength(2)
    es.emit('activity', { time: 't', vm: 'work', actor: 'owner', kind: 'action', action: 'revoke-pairings', code: 'revoke-pairings', outcome: 'ok', description: '' })
    await vi.waitFor(() => expect(byTestId('pair-devices').findAll('[data-testid="pair-device"]').map(r => r.attributes('data-device'))).toEqual(['dev-titan']))
  })

  it('shows when the VM cannot be asked', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /vms/work/pairings': { status: 409, body: { error: { code: 'vm_not_running', message: 'x' } } } })
    await openApp('/vms/work/pair', [vm('work', 'stopped')])
    expect(text('pair-error')).toBe('The VM is not running.')
  })
})

describe('Pair tab follows the Activity', () => {
  const entry = (action: string, vmName = 'work') => ({ time: '2026-11-03T12:00:00Z', vm: vmName, actor: 'root', kind: 'pairing', action, code: action, outcome: 'ok', description: '' })
  const wait = (check: () => void) => vi.waitFor(check, { timeout: 3000, interval: 20 })
  const quiet = () => new Promise(r => setTimeout(r, 500))

  async function open() {
    const api = fakeApi({
      'GET /session': SIGNED_IN,
      'GET /vms/work/pairings': { status: 200, body: DEVICES },
      'POST /vms/work/pairing-code': { status: 200, body: { code: CODE } },
    })
    const opened = await openApp('/vms/work/pair', [vm('work')])
    return { ...opened, loads: () => api.calls.filter(c => c.path === '/vms/work/pairings').length }
  }

  it.each(['observe', 'unpair', 'revoke-pairings'])('%s of this VM loads the devices again', async (code) => {
    const { es, loads } = await open()
    expect(loads()).toBe(1)
    es.emit('activity', entry(code))
    await wait(() => expect(loads()).toBe(2))
  })

  it('ignores other codes and another VM\'s entry', async () => {
    const { es, loads } = await open()
    es.emit('activity', entry('start'))
    es.emit('activity', entry('observe', 'priv'))
    await quiet()
    expect(loads()).toBe(1)
  })

  it('loads after a reconnect without resume, not after one that resumed, and keeps the revealed code', async () => {
    const { es, loads } = await open()
    await click('pair-reveal')
    await submit('reveal-dialog')
    expect(text('pair-code')).toBe(CODE)
    es.emit('hello', { stream: 'x', resumed: true })
    await quiet()
    expect(loads()).toBe(1)
    es.emit('hello', { stream: 'y', resumed: false })
    await wait(() => expect(loads()).toBe(2))
    await flushPromises()
    expect(text('pair-code')).toBe(CODE)
  })
})
