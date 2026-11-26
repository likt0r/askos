// Faults of the UI itself (#213): an unknown address and a fault in a view show in the page area
// with navigation and host bar still there; a fault the view can go on from and an unhandled
// rejection show a toast; a failing shell shows app/error.vue. Nothing is sent to the Host.
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import App from '~/app.vue'
import ErrorPage from '~/error.vue'
import { ApiError } from '~/utils/api'
import { faultMessage, inEventHandler } from '~/utils/faults'
import { fakeApi, FakeEventSource, SIGNED_IN, SIGNED_OUT, vm } from './fakes'
import { byTestId, click, exists, setUp, tearDown } from './vm-helpers'

let mounted: { unmount(): void } | null = null

beforeEach(async () => {
  await setUp()
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => {
  mounted?.unmount()
  mounted = null
  tearDown()
  clearError()
  useState('toasts').value = []
})

/** The app at `route`, with the stream live. Mounted with the app's own error handler
 * (plugins/faults.client.ts), as the real app is; test-utils mounts a separate Vue app. */
async function open(route: string) {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const errorHandler = useNuxtApp().vueApp.config.errorHandler
  const wrapper = await mountSuspended(App, { route, attachTo: root, global: { config: { errorHandler } } })
  mounted = wrapper
  await flushPromises()
  FakeEventSource.last.emit('hello', { stream: 'ab12cd34', resumed: false })
  FakeEventSource.last.emit('state', { vms: [vm('work')], problems: [] })
  await flushPromises()
  return wrapper
}

const STACK_FRAME = 'at renderBoom (secret-file.ts:12:3)'

function boom(message: string) {
  const error = new Error(message)
  error.stack = `Error: ${message}\n    ${STACK_FRAME}`
  return error
}

/** Test pages that fail in different places, inside the default layout like every page. */
function addFaultyRoutes() {
  const router = useRouter()
  router.addRoute({
    path: '/test-throws-in-render',
    component: defineComponent({
      render() {
        throw boom('render went wrong')
      },
    }),
  })
  // a list whose every row fails the same way
  const Row = defineComponent({
    props: { n: { type: Number, required: true } },
    render() {
      throw boom(`row ${this.n} went wrong`)
    },
  })
  router.addRoute({
    path: '/test-rows-throw',
    component: defineComponent({ render: () => h('div', [h(Row, { n: 1 }), h(Row, { n: 2 }), h(Row, { n: 3 })]) }),
  })
  router.addRoute({
    path: '/test-offline-in-setup',
    component: defineComponent({
      setup() {
        throw new ApiError(0, 'network', 'The Control Room cannot be reached')
      },
    }),
  })
  router.addRoute({
    path: '/test-throws-on-click',
    component: defineComponent({
      setup() {
        return () => h('div', { 'data-testid': 'clicky-page' }, [
          h('button', { 'data-testid': 'throw', onClick: () => { throw boom('click went wrong') } }),
          h('button', { 'data-testid': 'reject', onClick: async () => { throw boom('async click went wrong') } }),
          h('button', { 'data-testid': 'offline', onClick: async () => { throw new ApiError(0, 'network', 'The Control Room cannot be reached') } }),
        ])
      },
    }),
  })
}

/** Requests other than the reads the app makes on its own: anything that could carry a fault. */
const READS = ['GET /session', 'GET /version', 'GET /host']
function sent(api: ReturnType<typeof fakeApi>) {
  return api.calls.map(c => `${c.method} ${c.path}`).filter(c => !READS.includes(c))
}

/** The toasts shown: their titles and how long they stay (0: until dismissed). */
function toasts() {
  return useState<{ title?: string, duration?: number }[]>('toasts').value.map(t => ({ title: t.title, duration: t.duration }))
}

describe('an address no page matches', () => {
  it('shows the error page inside the layout, with the way back to the VMs', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    const wrapper = await open('/nope')
    expect(useRouter().currentRoute.value.path).toBe('/nope')
    expect(byTestId('fault').attributes('data-kind')).toBe('notFound')
    expect(byTestId('fault-title').text()).toBe('This page does not exist')
    expect(exists('fault-details')).toBe(false)
    // the shell stays
    expect(exists('nav-vms')).toBe(true)
    expect(exists('host-bar')).toBe(true)
    expect(wrapper.text()).toContain('Not found')

    await click('fault-back')
    await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/vms'))
    expect(exists('fault')).toBe(false)
    expect(sent(api)).toEqual([])
  })

  it('speaks German', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    await useNuxtApp().$i18n.setLocale('de')
    await open('/vms/work/nope/deeper')
    expect(byTestId('fault-title').text()).toBe('Diese Seite gibt es nicht')
    expect(byTestId('fault-back').text()).toBe('Zurück zu den VMs')
    expect(sent(api)).toEqual([])
  })

  it('signed out, leads to the sign-in page like any other address', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_OUT })
    mounted = await mountSuspended(App, { route: '/nope' })
    await flushPromises()
    const route = useRouter().currentRoute.value
    expect(route.path).toBe('/sign-in')
    expect(route.query.redirect).toBe('/nope')
    expect(sent(api)).toEqual([])
  })
})

describe('a fault in a view', () => {
  it('shows the error page in the page area; navigating away clears it', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await open('/test-throws-in-render')
    expect(byTestId('fault').attributes('data-kind')).toBe('view')
    expect(byTestId('fault-title').text()).toBe('Something went wrong in this view')
    expect(exists('fault-reload')).toBe(true)
    expect(exists('nav-vms')).toBe(true)
    expect(exists('host-bar')).toBe(true)

    // Details: the message, no stack trace
    expect(byTestId('fault-details').element.hasAttribute('open')).toBe(false)
    expect(byTestId('fault-message').text()).toBe('render went wrong')
    expect(document.body.textContent).not.toContain(STACK_FRAME)
    expect(document.body.textContent).not.toContain('secret-file')

    // as in a browser, the layout's route follows only pages that finished loading (test-utils
    // brings it up to date after every navigation while no page is shown)
    const route = useNuxtApp()._route as { sync?: () => void }
    const sync = route.sync
    route.sync = () => {}
    try {
      await click('nav-vms')
      await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/vms'))
      await flushPromises()
      expect(exists('fault')).toBe(false)
      expect(exists('vm-row')).toBe(true)
    }
    finally {
      route.sync = sync
    }
    expect(useError().value).toBeFalsy()          // not the full-screen error page
    expect(sent(api)).toEqual([])
  })

  it('shows the first of several faults of the page in the page area, not the full-screen page', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await open('/test-rows-throw')
    expect(byTestId('fault').attributes('data-kind')).toBe('view')
    expect(byTestId('fault-message').text()).toBe('row 1 went wrong')
    expect(useError().value).toBeFalsy()
    // each one is in the console
    const logged = vi.mocked(console.error).mock.calls.map(([e]) => (e instanceof Error ? e.message : null))
    expect(logged).toEqual(expect.arrayContaining(['row 1 went wrong', 'row 2 went wrong', 'row 3 went wrong']))
    expect(exists('nav-vms')).toBe(true)
    expect(sent(api)).toEqual([])
  })

  it('when the fault view itself fails, the full-screen page takes it', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    const i18n = useNuxtApp().$i18n
    const t = i18n.t.bind(i18n)
    vi.spyOn(i18n, 't').mockImplementation(((key: string, ...rest: unknown[]) => {
      if (key === 'fault.view.title') throw new Error('the fault view broke')
      return (t as (...a: unknown[]) => string)(key, ...rest)
    }) as typeof i18n.t)
    // it reaches the app: its handler shows app/error.vue (test-utils' mount, the root here,
    // rejects with it instead)
    const failed = await open('/test-throws-in-render').then(() => null, (e: Error) => e)
    expect(failed?.message ?? useError().value?.message).toBe('the fault view broke')
    expect(sent(api)).toEqual([])
  })

  it('a Control Room out of reach is the connection notice\'s to say', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await open('/test-offline-in-setup')
    expect(exists('fault')).toBe(false)
    expect(useError().value).toBeFalsy()
    expect(toasts()).toEqual([])
    expect(exists('nav-vms')).toBe(true)
    expect(sent(api)).toEqual([])
  })

  it('in German', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await useNuxtApp().$i18n.setLocale('de')
    await open('/test-throws-in-render')
    expect(byTestId('fault-title').text()).toBe('In dieser Ansicht ist etwas schiefgegangen')
    expect(byTestId('fault-reload').text()).toBe('Neu laden')
    expect(sent(api)).toEqual([])
  })

  it('in an event handler, keeps the view and shows a toast that stays, with Details', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await open('/test-throws-on-click')
    await click('throw')
    expect(exists('clicky-page')).toBe(true)
    expect(exists('fault')).toBe(false)
    await vi.waitFor(() => expect(exists('fault-details')).toBe(true))
    expect(toasts()).toEqual([{ title: 'Something went wrong in the Control Room UI', duration: 0 }])
    expect(byTestId('fault-message').text()).toBe('click went wrong')
    expect(document.body.textContent).not.toContain(STACK_FRAME)

    // a rejected async handler as well
    await click('reject')
    await vi.waitFor(() => expect(document.body.textContent).toContain('async click went wrong'))
    expect(exists('clicky-page')).toBe(true)
    expect(useError().value).toBeFalsy()

    // a Control Room that cannot be reached is the connection notice's to say
    await click('offline')
    expect(toasts()).toHaveLength(2)
    expect(sent(api)).toEqual([])
  })
})

describe('an unhandled promise rejection', () => {
  function reject(reason: unknown) {
    const event = new Event('unhandledrejection') as Event & { reason: unknown }
    event.reason = reason
    window.dispatchEvent(event)
  }

  it('is shown as a toast with its message', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    await open('/vms')
    reject(boom('lost promise'))
    await vi.waitFor(() => expect(exists('fault-message')).toBe(true))
    expect(byTestId('fault-message').text()).toBe('lost promise')
    expect(toasts()).toEqual([{ title: 'Something went wrong in the Control Room UI', duration: 0 }])
    expect(exists('vm-row')).toBe(true)
    expect(sent(api)).toEqual([])
  })

  it('in German', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    await useNuxtApp().$i18n.setLocale('de')
    await open('/vms')
    reject('just a string')
    await vi.waitFor(() => expect(exists('fault-message')).toBe(true))
    expect(byTestId('fault-message').text()).toBe('just a string')
    expect(toasts()).toEqual([{ title: 'In der Oberfläche des Control Room ist etwas schiefgegangen', duration: 0 }])
    expect(sent(api)).toEqual([])
  })

  it('of a request shows its code, not the Host\'s text; an ended session or a lost connection is shown elsewhere', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    await open('/vms')
    reject(new ApiError(0, 'network', 'The Control Room cannot be reached'))
    reject(new ApiError(401, 'unauthenticated', 'Sign in first'))
    await flushPromises()
    expect(toasts()).toEqual([])

    reject(new ApiError(500, 'internal', 'Text from the Host'))
    await vi.waitFor(() => expect(exists('fault-message')).toBe(true))
    expect(byTestId('fault-message').text()).toBe('ApiError 500 internal')
    expect(sent(api)).toEqual([])
  })
})

describe('outside the page area', () => {
  it('a fault in an event handler of the shell shows the toast (production names it by code)', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    await open('/vms')
    const handler = useNuxtApp().vueApp.config.errorHandler!
    handler(boom('a button of the shell'), null, 'https://vuejs.org/error-reference/#runtime-6')
    await vi.waitFor(() => expect(exists('fault-message')).toBe(true))
    expect(byTestId('fault-message').text()).toBe('a button of the shell')
    expect(useError().value).toBeFalsy()
    expect(sent(api)).toEqual([])
  })

  it('any other fault of the shell shows app/error.vue in place of the whole app', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    await open('/vms')
    const handler = useNuxtApp().vueApp.config.errorHandler!
    handler(boom('the layout broke'), null, 'https://vuejs.org/error-reference/#runtime-1')
    await flushPromises()
    expect(useError().value?.message).toBe('the layout broke')
    expect(toasts()).toEqual([])
    expect(sent(api)).toEqual([])
  })

  it('app/error.vue shows the fault itself, not its cause, and a request\'s 404 as a fault', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await open('/vms')
    const handler = useNuxtApp().vueApp.config.errorHandler!
    handler(new Error('chart failed', { cause: new TypeError('x is undefined') }), null, 'render function')
    await flushPromises()
    let page = await mountSuspended(ErrorPage, { props: { error: useError().value! } })
    expect(page.find('[data-testid="fault"]').attributes('data-kind')).toBe('shell')
    expect(page.find('[data-testid="fault-message"]').text()).toBe('chart failed')
    page.unmount()

    await clearError()
    handler(new ApiError(404, 'vm_not_found', 'Host text'), null, 'render function')
    await flushPromises()
    page = await mountSuspended(ErrorPage, { props: { error: useError().value! } })
    expect(page.find('[data-testid="fault"]').attributes('data-kind')).toBe('shell')
    expect(page.find('[data-testid="fault-message"]').text()).toBe('ApiError 404 vm_not_found')
    page.unmount()

    // an ended session or a lost connection: neither
    await clearError()
    handler(new ApiError(0, 'network', 'x'), null, 'render function')
    handler(new ApiError(401, 'unauthenticated', 'x'), null, 'render function')
    await flushPromises()
    expect(useError().value).toBeFalsy()
  })

  it('app/error.vue renders without the layout, with Reload and the message, in both languages', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    const error = createError({ message: 'the layout broke', cause: boom('the layout broke') })
    const page = await mountSuspended(ErrorPage, { props: { error } })
    expect(page.find('[data-testid="error-page"]').exists()).toBe(true)
    expect(page.find('[data-testid="nav-vms"]').exists()).toBe(false)
    expect(page.find('[data-testid="host-bar"]').exists()).toBe(false)
    expect(page.find('[data-testid="fault-title"]').text()).toBe('Something went wrong in the Control Room')
    expect(page.find('[data-testid="fault-reload"]').exists()).toBe(true)
    expect(page.find('[data-testid="fault-message"]').text()).toBe('the layout broke')
    expect(page.text()).not.toContain(STACK_FRAME)
    page.unmount()

    await useNuxtApp().$i18n.setLocale('de')
    const de = await mountSuspended(ErrorPage, { props: { error } })
    expect(de.find('[data-testid="fault-title"]').text()).toBe('Im Control Room ist etwas schiefgegangen')
    de.unmount()
    expect(sent(api)).toEqual([])
  })
})

describe('faults (units)', () => {
  it('knows a fault in an event handler by Vue\'s wording in development and its code in production', () => {
    expect(inEventHandler('native event handler')).toBe(true)
    expect(inEventHandler('component event handler')).toBe(true)
    expect(inEventHandler('https://vuejs.org/error-reference/#runtime-5')).toBe(true)
    expect(inEventHandler('https://vuejs.org/error-reference/#runtime-6')).toBe(true)
    for (const other of ['render function', 'setup function', 'watcher callback', 'mounted hook',
      'https://vuejs.org/error-reference/#runtime-0', 'https://vuejs.org/error-reference/#runtime-1',
      'https://vuejs.org/error-reference/#runtime-56', 'https://vuejs.org/error-reference/#runtime-m']) {
      expect(inEventHandler(other)).toBe(false)
    }
  })

  it('shows a fault\'s message, never its stack', () => {
    expect(faultMessage(boom('broken'))).toBe('broken')
    expect(faultMessage(new TypeError(''))).toBe('TypeError')
    expect(faultMessage('text')).toBe('text')
    expect(faultMessage({ code: 1 })).toBe('{"code":1}')
    expect(faultMessage(undefined)).toBe('undefined')
    expect(faultMessage(new ApiError(409, 'build_running', 'Host text with details'))).toBe('ApiError 409 build_running')
  })
})
