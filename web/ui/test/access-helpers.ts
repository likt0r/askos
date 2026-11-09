// Helpers for the tests of the Allowlist, Watchlist and credential views: the whole app on a
// route against a fake backend, and the dialogs (teleported to <body>).
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { vi } from 'vitest'
import App from '~/app.vue'
import { fakeApi, FakeEventSource, SIGNED_IN, vm } from './fakes'
import type { Call } from './fakes'

type Answer = { status: number, body?: unknown, headers?: Record<string, string> }
type Routes = Record<string, Answer | ((call: Call) => Answer | Promise<Answer>)>

const mounted: { unmount: () => void }[] = []

export function ok(body: unknown = {}): Answer {
  return { status: 200, body }
}

export function fail(status: number, code: string, message = code, detail?: string): Answer {
  return { status, body: { error: { code, message, ...(detail ? { detail } : {}) } } }
}

/** The app on `route`, signed in, with the given VMs from the live stream. */
export async function openApp(route: string, routes: Routes, vms = [vm('work'), vm('priv', 'running', { network: 2 })]) {
  const api = fakeApi({ 'GET /session': SIGNED_IN, ...routes })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const wrapper = await mountSuspended(App, { route, attachTo: host })
  mounted.push(wrapper)
  await flushPromises()
  const es = FakeEventSource.last
  es.emit('hello', { stream: 'x', resumed: false })
  es.emit('state', { vms, problems: [] })
  await flushPromises()
  await settle()
  return { wrapper, api, es }
}

export function unmountAll() {
  while (mounted.length) mounted.pop()!.unmount()
}

/** Lets requests, renders and dialog transitions finish. */
export async function settle() {
  for (let i = 0; i < 3; i++) {
    await flushPromises()
    await new Promise(r => setTimeout(r, 0))
  }
}

export function $(selector: string): HTMLElement | null {
  return document.body.querySelector<HTMLElement>(selector)
}

export function $$(selector: string): HTMLElement[] {
  return [...document.body.querySelectorAll<HTMLElement>(selector)]
}

export function must(selector: string): HTMLElement {
  const el = $(selector)
  if (!el) throw new Error(`not found: ${selector}`)
  return el
}

/** The element of `selector` whose data attribute `key` is `value` (a pattern, which a CSS
 * attribute selector would read with its backslashes as escapes). */
export function withData(selector: string, key: string, value: string): HTMLElement {
  const el = $$(selector).find(e => e.dataset[key] === value)
  if (!el) throw new Error(`not found: ${selector} with ${key}=${value}`)
  return el
}

export async function click(target: string | Element) {
  const el = typeof target === 'string' ? must(target) : target
  ;(el as HTMLElement).click()
  await settle()
}

export async function type(target: string, value: string) {
  const el = must(target)
  const input = (el.matches('input, textarea') ? el : el.querySelector('input, textarea')) as HTMLInputElement
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await settle()
}

export function changes(calls: Call[]) {
  return calls.filter(c => c.method !== 'GET').map(c => ({ method: c.method, path: c.path, body: c.body, csrf: c.headers['x-agentbox'] }))
}

export async function waitFor(check: () => void) {
  await vi.waitFor(check, { timeout: 3000, interval: 20 })
}

export { vm }
