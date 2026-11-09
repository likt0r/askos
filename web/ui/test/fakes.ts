// Stand-ins for the browser's EventSource and fetch, so tests drive the UI like the
// backend would (docs/control-room-api.md).
import { vi } from 'vitest'

export class FakeEventSource {
  static instances: FakeEventSource[] = []
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2

  readonly url: string
  readyState = FakeEventSource.CONNECTING
  closed = false
  private listeners = new Map<string, ((event: MessageEvent) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  static get last(): FakeEventSource {
    const es = FakeEventSource.instances.at(-1)
    if (!es) throw new Error('no EventSource was opened')
    return es
  }

  static reset() {
    FakeEventSource.instances = []
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener])
  }

  removeEventListener() {}

  close() {
    this.closed = true
    this.readyState = FakeEventSource.CLOSED
  }

  /** The backend sends an event. */
  emit(type: string, data: unknown, id?: string) {
    this.readyState = FakeEventSource.OPEN
    const event = new MessageEvent(type, { data: JSON.stringify(data), lastEventId: id ?? '' })
    for (const l of this.listeners.get(type) ?? []) l(event)
  }

  /** The connection dropped; the browser will retry by itself (readyState CONNECTING). */
  drop() {
    this.readyState = FakeEventSource.CONNECTING
    for (const l of this.listeners.get('error') ?? []) l(new MessageEvent('error'))
  }

  /** An HTTP error (401, 503): EventSource gives up (readyState CLOSED). */
  fail() {
    this.readyState = FakeEventSource.CLOSED
    for (const l of this.listeners.get('error') ?? []) l(new MessageEvent('error'))
  }
}

export function vm(name: string, state = 'running', fields: Record<string, unknown> = {}) {
  return {
    name, state, libvirt_state: 'running', network: 1, orca_port: 6801, image: 'base', build_pin: null,
    agents: {}, devices: [], system_disk: { since: null, age_days: 2, file: null, image: 'base', build: null },
    ...fields,
  }
}

export interface Call { method: string, path: string, headers: Record<string, string>, body: unknown }

type Answer = { status: number, body?: unknown, headers?: Record<string, string> }

/** A fake fetch for /api/v1: answers by "METHOD /path" (a function may decide per call, and
 * answer later with a promise: a request still on the way). */
export function fakeApi(routes: Record<string, Answer | ((call: Call) => Answer | Promise<Answer>)>) {
  const calls: Call[] = []
  const fetch = vi.fn(async (input: string, init: RequestInit = {}) => {
    const method = (init.method ?? 'GET').toUpperCase()
    const path = String(input).replace(/^\/api\/v1/, '')
    const headers = Object.fromEntries(Object.entries(init.headers ?? {}).map(([k, v]) => [k.toLowerCase(), String(v)]))
    const call = { method, path, headers, body: init.body ? JSON.parse(String(init.body)) : undefined }
    calls.push(call)
    const route = routes[`${method} ${path}`]
    const answer: Answer = (typeof route === 'function' ? await route(call) : route)
      ?? { status: 404, body: { error: { code: 'not_found', message: 'Not found' } } }
    return new Response(answer.body === undefined ? null : JSON.stringify(answer.body), {
      status: answer.status,
      headers: { 'Content-Type': 'application/json', ...answer.headers },
    })
  })
  vi.stubGlobal('fetch', fetch)
  return { calls, fetch }
}

export const SIGNED_IN = { status: 200, body: { authenticated: true, expires: '2026-11-04T00:00:00Z' } }
export const SIGNED_OUT = { status: 401, body: { error: { code: 'unauthenticated', message: 'Sign in first' } } }
