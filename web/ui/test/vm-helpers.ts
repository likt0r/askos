// Helpers for the VM detail tests: the app at a route with a live stream, and the
// dialogs, which Nuxt UI renders into document.body (outside the wrapper).
import { DOMWrapper, flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { vi } from 'vitest'
import App from '~/app.vue'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { FakeEventSource } from './fakes'

export async function setUp() {
  resetSessionForTests()
  resetLiveForTests()
  FakeEventSource.reset()
  vi.stubGlobal('EventSource', FakeEventSource)
  await useNuxtApp().$i18n.setLocale('en')
}

let mounted: { unmount(): void } | null = null

export function tearDown() {
  mounted?.unmount()
  mounted = null
  resetLiveForTests()
  resetSessionForTests()
}

/** The app at `route`, after the stream sent hello and these VMs (and host readings). */
export async function openApp(route: string, vms: unknown[], host?: unknown) {
  const root = document.createElement('div')
  document.body.appendChild(root)
  // attached, so the page and the dialogs (rendered into document.body) are one document
  const wrapper = await mountSuspended(App, { route, attachTo: root })
  mounted = wrapper
  await flushPromises()
  const es = FakeEventSource.last
  es.emit('hello', { stream: 'ab12cd34', resumed: false })
  if (host) es.emit('host', host)
  es.emit('state', { vms, problems: [] })
  await flushPromises()
  return { wrapper, es }
}

/** An element anywhere on the page (dialogs included) by data-testid. */
export function byTestId(id: string, within: ParentNode = document.body): DOMWrapper<HTMLElement> {
  const el = within.querySelector<HTMLElement>(`[data-testid="${id}"]`)
  if (!el) throw new Error(`no element [data-testid="${id}"]`)
  return new DOMWrapper(el)
}

export function exists(id: string) {
  return document.body.querySelector(`[data-testid="${id}"]`) !== null
}

export async function click(id: string, within?: ParentNode) {
  await byTestId(id, within).trigger('click')
  await flushPromises()
}

export async function submit(id: string) {
  await byTestId(id).trigger('submit')
  await flushPromises()
}

export async function type(id: string, value: string) {
  await byTestId(id).setValue(value)
  await flushPromises()
}

export function text(id: string) {
  return byTestId(id).text()
}
