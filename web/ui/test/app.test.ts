// The app shell against a fake backend: pages need a session, the VM list follows the
// stream, the connection state is visible, drops are retried, sign-out.
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '~/app.vue'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { fakeApi, FakeEventSource, SIGNED_IN, SIGNED_OUT, vm } from './fakes'

beforeEach(async () => {
  resetSessionForTests()
  resetLiveForTests()
  FakeEventSource.reset()
  vi.stubGlobal('EventSource', FakeEventSource)
  await useNuxtApp().$i18n.setLocale('en')
})
afterEach(() => {
  resetLiveForTests()
  resetSessionForTests()
})

function rows(wrapper: Awaited<ReturnType<typeof mountSuspended>>) {
  return wrapper.findAll('[data-testid="vm-row"]').map(r => [r.attributes('data-vm'), r.find('[data-testid="vm-state"]').attributes('data-state')])
}

function connection(wrapper: Awaited<ReturnType<typeof mountSuspended>>) {
  return wrapper.find('[data-testid="connection-status"]').attributes('data-connection')
}

describe('app shell', () => {
  it('sends a visitor without a session to the sign-in page, keeping the deep link', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT })
    await mountSuspended(App, { route: '/vms/work/log' })
    await flushPromises()
    const route = useRouter().currentRoute.value
    expect(route.path).toBe('/sign-in')
    expect(route.query.redirect).toBe('/vms/work/log')
    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('shows the navigation and the live VM list', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    const wrapper = await mountSuspended(App, { route: '/vms' })
    await flushPromises()
    for (const [id, label] of [['vms', 'VMs'], ['allowlist', 'Allowlist'], ['watchlist', 'Watchlist'], ['activity', 'Activity'], ['system', 'System'], ['settings', 'Settings']]) {
      expect(wrapper.find(`[data-testid="nav-${id}"]`).text()).toContain(label)
    }
    expect(wrapper.find('[data-testid="host-bar"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="vms-loading"]').exists()).toBe(true)
    expect(connection(wrapper)).toBe('connecting')

    const es = FakeEventSource.last
    expect(es.url).toBe('/api/v1/events')
    es.emit('hello', { stream: 'ab12cd34', resumed: false })
    es.emit('watch', { running: true, since: 't', restarts: 0, last_exit: null })
    es.emit('state', { vms: [vm('work'), vm('priv', 'paused', { network: 2 })], problems: [] })
    await flushPromises()
    expect(connection(wrapper)).toBe('live')
    expect(rows(wrapper)).toEqual([['work', 'running'], ['priv', 'paused']])
    expect(wrapper.find('[data-testid="vm-count"]').text()).toBe('2 VMs')

    // one VM changes: its row updates without reloading
    es.emit('vm', vm('priv', 'running', { network: 2, agents: { working: 2, waiting: 1 } }))
    await flushPromises()
    expect(rows(wrapper)).toEqual([['work', 'running'], ['priv', 'running']])
    expect(wrapper.find('[data-vm="priv"]').text()).toContain('2 working · 1 waiting')

    // filter
    await wrapper.find('[data-testid="vm-filter"] input, input[data-testid="vm-filter"]').setValue('pri')
    expect(rows(wrapper)).toEqual([['priv', 'running']])
  })

  it('shows a dropped connection, reconnects, and says when agentbox watch is not running', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    const wrapper = await mountSuspended(App, { route: '/vms' })
    await flushPromises()
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })
    FakeEventSource.last.emit('state', { vms: [vm('work')], problems: [] })
    await flushPromises()
    expect(wrapper.find('[data-testid="connection-notice"]').exists()).toBe(false)

    FakeEventSource.last.fail()        // e.g. 503 while the backend restarts
    await flushPromises()
    expect(connection(wrapper)).toBe('reconnecting')
    const notice = wrapper.find('[data-testid="connection-notice"]')
    expect(notice.text()).toContain('The live connection dropped')
    expect(rows(wrapper)).toEqual([['work', 'running']])     // still shown, marked as possibly stale

    // "Retry now"
    await notice.find('button').trigger('click')
    expect(FakeEventSource.instances).toHaveLength(2)
    FakeEventSource.last.emit('hello', { stream: 'y', resumed: false })
    FakeEventSource.last.emit('watch', { running: false, since: null, restarts: 3, last_exit: 1 })
    await flushPromises()
    expect(connection(wrapper)).toBe('live')
    expect(wrapper.find('[data-testid="connection-notice"]').text()).toContain('agentbox watch')
  })

  it('goes to the sign-in page when the stream ends with the session', async () => {
    let signedIn = true
    fakeApi({ 'GET /session': () => (signedIn ? SIGNED_IN : SIGNED_OUT) })
    await mountSuspended(App, { route: '/vms/work' })
    await flushPromises()
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })
    signedIn = false
    FakeEventSource.last.fail()        // 401
    await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/sign-in'))
    const route = useRouter().currentRoute.value
    expect(route.query).toMatchObject({ redirect: '/vms/work/web', expired: '1' })
    expect(FakeEventSource.last.closed).toBe(true)
  })

  it('shows a VM with its tabs, and an unknown one as missing', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    const wrapper = await mountSuspended(App, { route: '/vms/work' })
    await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/vms/work/web'))
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })
    FakeEventSource.last.emit('state', { vms: [vm('work')], problems: [] })
    await flushPromises()
    expect(wrapper.find('[data-testid="vm-detail"]').text()).toContain('work')
    const tabs = wrapper.find('[data-testid="vm-tabs"]').text()
    for (const tab of ['Web', 'Access', 'Log', 'Pair']) expect(tabs).toContain(tab)
    expect(wrapper.find('[data-testid="web-tab"]').exists()).toBe(true)

    await useRouter().push('/vms/nope/log')
    await vi.waitFor(() => expect(wrapper.find('[data-testid="vm-not-found"]').exists()).toBe(true))
    expect(wrapper.find('[data-testid="vm-not-found"]').text()).toContain('There is no VM named “nope”.')
  })

  it('signs out with the CSRF header and forgets the live state', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'DELETE /session': { status: 200, body: { authenticated: false } } })
    const wrapper = await mountSuspended(App, { route: '/vms' })
    await flushPromises()
    FakeEventSource.last.emit('hello', { stream: 'x', resumed: false })
    await wrapper.find('[data-testid="sign-out"]').trigger('click')
    await flushPromises()
    const del = api.calls.find(c => c.method === 'DELETE')!
    expect(del.path).toBe('/session')
    expect(del.headers['x-agentbox']).toBe('1')
    await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/sign-in'))
    expect(FakeEventSource.last.closed).toBe(true)
    expect(useLive().state.vms).toEqual([])
  })
})
