// Browser notifications and the reload prompt (#21): the `notification` event becomes a browser
// notification in the UI language, per-kind preferences, no double showing, the service worker
// fallback for Android's Chrome, and "a new version is installed".
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  defaultPrefs, isNotificationEvent, loadPrefs, notificationKey, PREFS_KEY, savePrefs,
} from '~/utils/notifications'
import { ok, openPage, useFreshApp } from './system-helpers'

interface Shown { title: string, options: Record<string, unknown> }

/** A stand-in for the browser's Notification class that records what was shown. */
function stubNotification(permission: string = 'granted', fail = false) {
  const shown: Shown[] = []
  const instances: { onclick: (() => void) | null, close: () => void }[] = []
  class FakeNotification {
    static permission = permission
    static requestPermission = vi.fn(async () => permission)
    onclick: (() => void) | null = null
    close = vi.fn()
    constructor(title: string, options: Record<string, unknown> = {}) {
      if (fail) throw new TypeError('Failed to construct \'Notification\': Illegal constructor. Use ServiceWorkerRegistration.showNotification() instead.')
      shown.push({ title, options })
      instances.push(this)
    }
  }
  vi.stubGlobal('Notification', FakeNotification)
  return Object.assign(shown, { instances })
}

const T = '2026-11-03T12:00:00Z'
const events: Record<string, Record<string, unknown>> = {
  alert: { time: T, kind: 'alert', severity: 'critical', vm: 'work', text: 'work tried to reach paste.example:443 (paste, young)', domain: 'paste.example:443', signals: ['paste', 'young'] },
  'github-refused': { time: T, kind: 'github-refused', severity: 'warning', vm: 'work', text: 'GitHub token refused', request: 'likt0r/askos', detail: 'Repository nicht erlaubt' },
  'build-failed': { time: T, kind: 'build-failed', severity: 'warning', vm: null, text: 'Build failed', image: 'base', build: '7', detail: 'dnf failed' },
  'build-rejected': { time: T, kind: 'build-rejected', severity: 'critical', vm: null, text: 'Build rejected', image: 'base', build: '8', detail: 'safeguard missing' },
  'host-reboot': { time: T, kind: 'host-reboot', severity: 'warning', vm: null, text: 'Host reboot needed', packages: ['kernel'] },
  temperature: { time: T, kind: 'temperature', severity: 'warning', vm: null, text: 'cpu.temp 91', reading: 'cpu.temp', device: null, value: 91, threshold: 85 },
}

const EN: Record<string, [string, string]> = {
  alert: ['Alert: work', 'work tried to reach paste.example:443 (paste, young)'],
  'github-refused': ['GitHub token refused: work', 'work: the GitHub token request for likt0r/askos was refused'],
  'build-failed': ['Build failed: base', 'Build base-7 failed. The previous Build stays in use.'],
  'build-rejected': ['Build rejected: base', 'Build base-8 was rejected: a safeguard is missing or weakened. The previous Build stays in use.'],
  'host-reboot': ['Host reboot needed', 'Updates need a Host reboot. The disk passphrase must be entered at boot.'],
  temperature: ['Host temperature', 'cpu.temp is at 91 °C (threshold 85 °C)'],
}
const DE: Record<string, [string, string]> = {
  alert: ['Alert: work', 'work wollte paste.example:443 erreichen (paste, young)'],
  'github-refused': ['GitHub-Token abgelehnt: work', 'work: Die Anfrage nach einem GitHub-Token für likt0r/askos wurde abgelehnt'],
  'build-failed': ['Build gescheitert: base', 'Build base-7 ist gescheitert. Der vorherige Build bleibt in Gebrauch.'],
  'build-rejected': ['Build verworfen: base', 'Build base-8 wurde verworfen: eine Schutzmaßnahme fehlt oder ist abgeschwächt. Der vorherige Build bleibt in Gebrauch.'],
  'host-reboot': ['Host-Neustart nötig', 'Updates brauchen einen Neustart des Hosts. Die Passphrase der Festplatte muss beim Start eingegeben werden.'],
  temperature: ['Temperatur des Hosts', 'cpu.temp liegt bei 91 °C (Schwelle 85 °C)'],
}

const settle = async () => { await flushPromises(); await flushPromises() }

describe('browser notifications', () => {
  useFreshApp()
  afterEach(() => {
    delete (document as unknown as Record<string, unknown>).visibilityState
    delete (document as unknown as Record<string, unknown>).hidden
  })

  it('shows every kind with a translated title and body, and a tag of its own', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    for (const e of Object.values(events)) es.emit('notification', e)
    await settle()
    expect(shown.map(s => [s.title, s.options.body])).toEqual(Object.keys(events).map(k => EN[k]))
    const tags = shown.map(s => s.options.tag)
    expect(new Set(tags).size).toBe(Object.keys(events).length)
    expect(tags.every(t => typeof t === 'string' && t.length > 0)).toBe(true)
  })

  it('words them in German when the UI is German', async () => {
    const shown = stubNotification()
    await useNuxtApp().$i18n.setLocale('de')
    const { es } = await openPage('/vms')
    for (const e of Object.values(events)) es.emit('notification', e)
    await settle()
    expect(shown.map(s => [s.title, s.options.body])).toEqual(Object.keys(events).map(k => DE[k]))
  })

  it('gives two events of one kind different tags, and leads to the right page', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    es.emit('notification', events.alert)
    es.emit('notification', { ...events.alert, domain: 'other.example:443', text: 'work tried to reach other.example:443', time: '2026-11-03T12:00:01Z' })
    es.emit('notification', events.temperature)
    await settle()
    expect(shown).toHaveLength(3)
    expect(new Set(shown.map(s => s.options.tag)).size).toBe(3)
    expect(shown[0]!.options.data).toEqual({ url: '/activity' })
    expect(shown[2]!.options.data).toEqual({ url: '/system' })
  })

  it('tells two events apart that differ in a field other than the text', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    es.emit('notification', { ...events.temperature, device: 'nvme0n1' })
    es.emit('notification', { ...events.temperature, device: 'nvme1n1' })
    await settle()
    expect(shown).toHaveLength(2)
    expect(new Set(shown.map(s => s.options.tag)).size).toBe(2)
  })

  it('shows nothing for a kind that is switched off, the others still', async () => {
    localStorage.setItem('agentbox-notifications', JSON.stringify({ alert: false }))
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    es.emit('notification', events.alert)
    es.emit('notification', events['build-failed'])
    await settle()
    expect(shown.map(s => s.title)).toEqual(['Build failed: base'])
  })

  it('follows a preference changed while the page is open', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    useNotificationPrefs().setKind('alert', false)
    es.emit('notification', events.alert)
    await settle()
    expect(shown).toHaveLength(0)
    useNotificationPrefs().setKind('alert', true)
    es.emit('notification', { ...events.alert, time: '2026-11-03T12:05:00Z' })
    await settle()
    expect(shown).toHaveLength(1)
  })

  it('ignores unknown kinds and malformed events', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    es.emit('notification', { time: T, kind: 'coffee', severity: 'warning', vm: null, text: 'Coffee is ready' })
    es.emit('notification', { time: T, kind: 'alert' })
    es.emit('notification', { time: T, text: 'no kind' })
    es.emit('notification', 'a string')
    es.emit('notification', null)
    es.emit('notification', 42)
    es.emit('notification', [])
    es.emit('notification', { kind: 'toString', text: 'prototype' })
    es.emit('notification', { kind: '__proto__', text: 'prototype' })
    await settle()
    expect(shown).toHaveLength(0)
    es.emit('notification', events.alert)
    await settle()
    expect(shown).toHaveLength(1)
  })

  it('copes with events that lack the optional fields', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    es.emit('notification', { time: T, kind: 'alert', severity: 'critical', vm: null, text: 'Alert' })
    es.emit('notification', { time: T, kind: 'temperature', severity: 'warning', vm: null, text: 'Hot' })
    await settle()
    expect(shown).toHaveLength(2)
    for (const s of shown) expect(`${s.title} ${s.options.body}`).not.toMatch(/undefined|null|NaN|\{/)
  })

  for (const permission of ['default', 'denied']) {
    it(`shows nothing and throws nothing when the permission is ${permission}`, async () => {
      const shown = stubNotification(permission)
      const { es } = await openPage('/vms')
      expect(() => es.emit('notification', events.alert)).not.toThrow()
      await settle()
      expect(shown).toHaveLength(0)
    })
  }

  it('shows nothing and throws nothing when the browser has no Notification', async () => {
    vi.stubGlobal('Notification', undefined)
    const { es } = await openPage('/vms')
    expect(() => es.emit('notification', events.alert)).not.toThrow()
    await settle()
    expect(useNotificationPrefs().support.value).toBe('unsupported')
  })

  it('shows an event once when a resumed stream replays it', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    es.emit('notification', events.alert, 'abcd1234-5')
    es.emit('notification', events.alert, 'abcd1234-5')
    es.drop()
    es.emit('hello', { stream: 'abcd1234', resumed: true })
    es.emit('notification', events.alert, 'abcd1234-5')
    await settle()
    expect(shown).toHaveLength(1)
  })

  it('does not remember an event it could not show: allowing notifications later shows it', async () => {
    const shown = stubNotification('default')
    const { es } = await openPage('/vms')
    es.emit('notification', events.alert)
    await settle()
    expect(shown).toHaveLength(0)
    ;(Notification as unknown as { permission: string }).permission = 'granted'
    es.emit('notification', events.alert)
    await settle()
    expect(shown).toHaveLength(1)
  })

  it('notifies while the page is in the background', async () => {
    const shown = stubNotification()
    const { es } = await openPage('/vms')
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    es.emit('notification', events.alert)
    es.emit('notification', events['host-reboot'])
    await settle()
    expect(shown.map(s => s.title)).toEqual(['Alert: work', 'Host reboot needed'])
  })

  it('does not stack listeners when the app shell mounts again', async () => {
    const shown = stubNotification()
    const first = await openPage('/vms')
    first.wrapper.unmount()
    const { es } = await openPage('/vms')
    es.emit('notification', events.alert)
    await settle()
    expect(shown).toHaveLength(1)
  })

  it('closes the notification and focuses the window on a click', async () => {
    const shown = stubNotification()
    const focus = vi.spyOn(window, 'focus').mockImplementation(() => {})
    const { es } = await openPage('/vms')
    es.emit('notification', events.alert)
    await settle()
    expect(shown.instances).toHaveLength(1)
    shown.instances[0]!.onclick!()
    expect(focus).toHaveBeenCalled()
    expect(shown.instances[0]!.close).toHaveBeenCalled()
  })
})

describe('service worker fallback', () => {
  useFreshApp()

  function stubServiceWorker(register: () => Promise<unknown>, ready: Promise<unknown>) {
    const showNotification = vi.fn(async (..._args: unknown[]) => {})
    const registration = { showNotification }
    const sw = {
      register: vi.fn(async (..._args: unknown[]) => { await register(); return registration }),
      ready: ready.then(() => registration),
    }
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { serviceWorker: sw }))
    return { sw, showNotification, registration }
  }

  it('uses the service worker when `new Notification` throws (Android Chrome)', async () => {
    const shown = stubNotification('granted', true)
    const { sw, showNotification } = stubServiceWorker(async () => undefined, Promise.resolve())
    const { es } = await openPage('/vms')
    es.emit('notification', events.alert)
    await settle()
    expect(shown).toHaveLength(0)
    expect(sw.register).toHaveBeenCalledWith('/notification-sw.js')
    expect(showNotification).toHaveBeenCalledTimes(1)
    const [title, options] = showNotification.mock.calls[0] as unknown as [string, Record<string, unknown>]
    expect(title).toBe('Alert: work')
    expect(options).toMatchObject({ body: EN.alert![1], data: { url: '/activity' } })
    expect(typeof options.tag).toBe('string')
  })

  it('shows through the registration when `ready` does not come', async () => {
    vi.useFakeTimers()
    try {
      stubNotification('granted', true)
      const { showNotification } = stubServiceWorker(async () => undefined, new Promise(() => {}))
      const { es } = await openPage('/vms')
      es.emit('notification', events.alert)
      await vi.advanceTimersByTimeAsync(6000)
      expect(showNotification).toHaveBeenCalledTimes(1)
    }
    finally {
      vi.useRealTimers()
    }
  })

  it('throws nothing when there is no service worker, or it cannot be registered', async () => {
    stubNotification('granted', true)
    const { es } = await openPage('/vms')
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { serviceWorker: undefined }))
    expect(() => es.emit('notification', events.alert)).not.toThrow()
    await settle()
    stubServiceWorker(async () => { throw new Error('insecure context') }, Promise.resolve())
    expect(() => es.emit('notification', events['build-failed'])).not.toThrow()
    await settle()
  })

  it('has a worker script that is valid and only shows notifications', async () => {
    const source = readFileSync(resolve(process.cwd(), 'public/notification-sw.js'), 'utf8')
    expect(() => new Function(source)).not.toThrow()
    expect(source.replace(/^\s*\/\/.*$/gm, '')).not.toMatch(/\bfetch\b|importScripts|caches\.|eval\(|https?:\/\//)
    const listeners: Record<string, (event: unknown) => void> = {}
    const opened: string[] = []
    const focused: string[] = []
    const scope = {
      windows: [] as unknown[],
      skipWaiting: vi.fn(),
      clients: {
        claim: vi.fn(async () => {}),
        matchAll: vi.fn(async () => scope.windows),
        openWindow: vi.fn(async (url: string) => { opened.push(url) }),
      },
      addEventListener: (name: string, fn: (event: unknown) => void) => { listeners[name] = fn },
    }
    new Function('self', source)(scope)
    expect(Object.keys(listeners).sort()).toEqual(['activate', 'install', 'notificationclick'])
    const click = async (data: unknown) => {
      let pending: Promise<unknown> = Promise.resolve()
      const close = vi.fn()
      listeners.notificationclick!({ notification: { close, data }, waitUntil: (p: Promise<unknown>) => { pending = p } })
      await pending
      return close
    }
    expect(await click({ url: '/system' })).toHaveBeenCalled()
    expect(opened).toEqual(['/system'])
    scope.windows = [{ focus: async () => { focused.push('window') } }]
    await click(undefined)
    expect(focused).toEqual(['window'])
    expect(opened).toEqual(['/system'])
  })
})

describe('reload prompt', () => {
  useFreshApp()

  type Wrapper = Awaited<ReturnType<typeof openPage>>['wrapper']
  const version = (v: string, ui = 'nuxt') => ({ version: v, ui, installed: null })
  const prompt = (w: Wrapper) => w.find('[data-testid="reload-prompt"]')
  const button = (w: Wrapper, id: 'reload-now' | 'reload-later') => w.find(`[data-testid="${id}"]`)

  it('asks GET /version once at start and shows no prompt for the same version', async () => {
    const { wrapper, es, api } = await openPage('/vms')
    es.emit('version', version('aaaa'))
    await settle()
    expect(api.calls.filter(c => c.path === '/version')).toHaveLength(1)
    expect(prompt(wrapper).exists()).toBe(false)
  })

  it('offers a reload when another version is installed, and reloads on a click', async () => {
    const reload = vi.fn()
    const { wrapper, es } = await openPage('/vms')
    vi.spyOn(window.location, 'reload').mockImplementation(reload)
    es.emit('version', version('bbbb'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(true)
    expect(prompt(wrapper).text()).toContain('A new version of the Control Room is installed')
    await button(wrapper, 'reload-now').trigger('click')
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('shows the prompt in German', async () => {
    await useNuxtApp().$i18n.setLocale('de')
    const { wrapper, es } = await openPage('/vms')
    es.emit('version', version('bbbb'))
    await settle()
    expect(prompt(wrapper).text()).toContain('Eine neue Version des Control Room ist installiert')
    expect(button(wrapper, 'reload-now').text()).toBe('Neu laden')
    expect(button(wrapper, 'reload-later').text()).toBe('Jetzt nicht')
  })

  it('"Not now" hides it until yet another version arrives', async () => {
    const { wrapper, es } = await openPage('/vms')
    es.emit('version', version('bbbb'))
    await settle()
    await button(wrapper, 'reload-later').trigger('click')
    await settle()
    expect(prompt(wrapper).exists()).toBe(false)
    es.emit('version', version('bbbb'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(false)
    es.emit('version', version('cccc'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(true)
  })

  it('shows no prompt for the legacy UI', async () => {
    const { wrapper, es } = await openPage('/vms')
    es.emit('version', version('bbbb', 'legacy'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(false)
  })

  it('goes away when the page\'s own version is installed again', async () => {
    const { wrapper, es } = await openPage('/vms')
    es.emit('version', version('bbbb'))
    await settle()
    es.emit('version', version('aaaa'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(false)
  })

  it('shows no prompt when GET /version fails', async () => {
    const { wrapper, es } = await openPage('/vms', { 'GET /version': { status: 500, body: { error: { code: 'internal', message: 'x' } } } })
    es.emit('version', version('bbbb'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(false)
  })

  it('shows no prompt when GET /version fails with a network error', async () => {
    const { wrapper, es } = await openPage('/vms', { 'GET /version': () => { throw new TypeError('network') } })
    es.emit('version', version('bbbb'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(false)
  })

  it('shows no prompt for a garbage version answer', async () => {
    const { wrapper, es } = await openPage('/vms', { 'GET /version': ok({ version: 7 }) })
    es.emit('version', version('bbbb'))
    await settle()
    expect(prompt(wrapper).exists()).toBe(false)
  })
})

describe('preferences (units)', () => {
  const memory = (initial?: string) => {
    const data = new Map<string, string>(initial === undefined ? [] : [[PREFS_KEY, initial]])
    return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => { data.set(k, v) }, data }
  }

  it('is all on by default', () => {
    expect(loadPrefs(memory())).toEqual(defaultPrefs())
    expect(Object.values(defaultPrefs()).every(v => v === true)).toBe(true)
  })

  it('reads what was saved and keeps the rest on', () => {
    expect(loadPrefs(memory('{"alert":false,"temperature":false}'))).toEqual({ ...defaultPrefs(), alert: false, temperature: false })
  })

  it('ignores values that are not booleans, and unknown keys', () => {
    expect(loadPrefs(memory('{"alert":"no","build-failed":0,"coffee":false,"host-reboot":false}'))).toEqual({ ...defaultPrefs(), 'host-reboot': false })
  })

  for (const broken of ['{not json', '', 'null', '5', '"text"', '[]', '[false]', 'true']) {
    it(`falls back to the defaults for ${JSON.stringify(broken)}`, () => {
      expect(loadPrefs(memory(broken))).toEqual(defaultPrefs())
    })
  }

  it('falls back to the defaults when storage throws or is missing', () => {
    const throwing = { getItem: () => { throw new DOMException('denied', 'SecurityError') }, setItem: () => { throw new DOMException('full', 'QuotaExceededError') } }
    expect(loadPrefs(throwing)).toEqual(defaultPrefs())
    expect(() => savePrefs({ ...defaultPrefs(), alert: false }, throwing)).not.toThrow()
    expect(loadPrefs(null)).toEqual(defaultPrefs())
    expect(() => savePrefs(defaultPrefs(), null)).not.toThrow()
  })

  it('round-trips through storage', () => {
    const storage = memory()
    savePrefs({ ...defaultPrefs(), 'build-failed': false }, storage)
    expect(JSON.parse(storage.data.get(PREFS_KEY)!)).toMatchObject({ 'build-failed': false, alert: true })
    expect(loadPrefs(storage)).toEqual({ ...defaultPrefs(), 'build-failed': false })
  })

  it('uses localStorage, and the defaults when it throws (private mode)', () => {
    localStorage.clear()
    savePrefs({ ...defaultPrefs(), alert: false })
    expect(localStorage.getItem(PREFS_KEY)).toContain('"alert":false')
    expect(loadPrefs().alert).toBe(false)
    const denied = () => { throw new DOMException('denied', 'SecurityError') }
    vi.stubGlobal('localStorage', { getItem: denied, setItem: denied })
    expect(loadPrefs()).toEqual(defaultPrefs())
    expect(() => savePrefs(defaultPrefs())).not.toThrow()
  })

  it('copes with a browser that throws on touching localStorage at all', () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')!
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, get: () => { throw new DOMException('denied', 'SecurityError') } })
    try {
      expect(loadPrefs()).toEqual(defaultPrefs())
      expect(() => savePrefs(defaultPrefs())).not.toThrow()
    }
    finally {
      Object.defineProperty(globalThis, 'localStorage', original)
    }
  })

  it('recognises notification events and keys a repeat alike', () => {
    expect(isNotificationEvent(events.alert)).toBe(true)
    expect(isNotificationEvent({ kind: 'alert' })).toBe(false)
    expect(isNotificationEvent(null)).toBe(false)
    expect(notificationKey(events.alert as never)).toBe(notificationKey({ ...events.alert } as never))
  })
})
