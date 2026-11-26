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
import { fakeApi, SIGNED_IN, SIGNED_OUT, vm } from './fakes'
import { byTestId, click, exists, openApp, setUp, tearDown } from './vm-helpers'

beforeEach(async () => {
  await setUp()
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => {
  tearDown()
  clearError()
  useState('toasts').value = []
})

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
  router.addRoute({
    path: '/test-throws-on-click',
    component: defineComponent({
      setup() {
        return () => h('div', { 'data-testid': 'clicky-page' }, [
          h('button', { 'data-testid': 'throw', onClick: () => { throw boom('click went wrong') } }),
          h('button', { 'data-testid': 'reject', onClick: async () => { throw boom('async click went wrong') } }),
        ])
      },
    }),
  })
}

/** Requests that could carry something to the Host: everything but reads. */
function sent(api: ReturnType<typeof fakeApi>) {
  return api.calls.filter(c => c.method !== 'GET').map(c => `${c.method} ${c.path}`)
}

/** The toasts shown: their titles and how long they stay (0: until dismissed). */
function toasts() {
  return useState<{ title?: string, duration?: number }[]>('toasts').value.map(t => ({ title: t.title, duration: t.duration }))
}

describe('an address no page matches', () => {
  it('shows the error page inside the layout, with the way back to the VMs', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    const { wrapper } = await openApp('/nope', [vm('work')])
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
    fakeApi({ 'GET /session': SIGNED_IN })
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/vms/work/nope/deeper', [vm('work')])
    expect(byTestId('fault-title').text()).toBe('Diese Seite gibt es nicht')
    expect(byTestId('fault-back').text()).toBe('Zurück zu den VMs')
  })

  it('signed out, leads to the sign-in page like any other address', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT })
    await mountSuspended(App, { route: '/nope' })
    await flushPromises()
    const route = useRouter().currentRoute.value
    expect(route.path).toBe('/sign-in')
    expect(route.query.redirect).toBe('/nope')
  })
})

describe('a fault in a view', () => {
  it('shows the error page in the page area; navigating away clears it', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await openApp('/test-throws-in-render', [vm('work')])
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

    await click('nav-vms')
    await vi.waitFor(() => expect(useRouter().currentRoute.value.path).toBe('/vms'))
    await flushPromises()
    expect(exists('fault')).toBe(false)
    expect(exists('vm-row')).toBe(true)
    expect(useError().value).toBeFalsy()          // not the full-screen error page
    expect(sent(api)).toEqual([])
  })

  it('in German', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/test-throws-in-render', [vm('work')])
    expect(byTestId('fault-title').text()).toBe('In dieser Ansicht ist etwas schiefgegangen')
    expect(byTestId('fault-reload').text()).toBe('Neu laden')
  })

  it('in an event handler, keeps the view and shows a toast that stays, with Details', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    addFaultyRoutes()
    await openApp('/test-throws-on-click', [vm('work')])
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
    await openApp('/vms', [vm('work')])
    reject(boom('lost promise'))
    await vi.waitFor(() => expect(exists('fault-message')).toBe(true))
    expect(byTestId('fault-message').text()).toBe('lost promise')
    expect(toasts()).toEqual([{ title: 'Something went wrong in the Control Room UI', duration: 0 }])
    expect(exists('vm-row')).toBe(true)
    expect(sent(api)).toEqual([])
  })

  it('in German', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/vms', [vm('work')])
    reject('just a string')
    await vi.waitFor(() => expect(exists('fault-message')).toBe(true))
    expect(byTestId('fault-message').text()).toBe('just a string')
    expect(toasts()).toEqual([{ title: 'In der Oberfläche des Control Room ist etwas schiefgegangen', duration: 0 }])
  })

  it('of a request shows its code, not the Host\'s text; an ended session or a lost connection is shown elsewhere', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
    await openApp('/vms', [vm('work')])
    reject(new ApiError(0, 'network', 'The Control Room cannot be reached'))
    reject(new ApiError(401, 'unauthenticated', 'Sign in first'))
    await flushPromises()
    expect(exists('fault-message')).toBe(false)

    reject(new ApiError(500, 'internal', 'Text from the Host'))
    await vi.waitFor(() => expect(exists('fault-message')).toBe(true))
    expect(byTestId('fault-message').text()).toBe('ApiError 500 internal')
  })
})

describe('a failing shell', () => {
  it('shows app/error.vue in place of the whole app', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN })
    await openApp('/vms', [vm('work')])
    const handler = useNuxtApp().vueApp.config.errorHandler!
    handler(boom('the layout broke'), null, 'render function')
    await flushPromises()
    expect(useError().value?.message).toBe('the layout broke')
    expect(sent(api)).toEqual([])
  })

  it('app/error.vue renders without the layout, with Reload and the message, in both languages', async () => {
    fakeApi({ 'GET /session': SIGNED_IN })
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
  })
})
