// Browser notifications (#21): which event kinds a browser shows is a per-browser
// preference (localStorage); the browser's permission is asked once, by a click.

/** `kind` of the `notification` event (docs/control-room-api.md "Notifications"). */
export const NOTIFICATION_KINDS = [
  'alert', 'github-refused', 'build-failed', 'build-rejected', 'host-reboot', 'temperature',
] as const
export type NotificationKind = (typeof NOTIFICATION_KINDS)[number]

export interface NotificationEvent {
  time: string
  kind: string
  severity: 'warning' | 'critical' | string
  vm: string | null
  text: string
  [key: string]: unknown
}

export type Prefs = Record<NotificationKind, boolean>

export const PREFS_KEY = 'agentbox-notifications'

export function defaultPrefs(): Prefs {
  return Object.fromEntries(NOTIFICATION_KINDS.map(k => [k, true])) as Prefs
}

export function loadPrefs(storage: Pick<Storage, 'getItem'> | null = safeStorage()): Prefs {
  const prefs = defaultPrefs()
  try {
    const raw = storage?.getItem(PREFS_KEY)
    const stored = raw ? JSON.parse(raw) as Record<string, unknown> : {}
    for (const kind of NOTIFICATION_KINDS) {
      if (typeof stored[kind] === 'boolean') prefs[kind] = stored[kind] as boolean
    }
  }
  catch {
    // unreadable: the defaults
  }
  return prefs
}

export function savePrefs(prefs: Prefs, storage: Pick<Storage, 'setItem'> | null = safeStorage()) {
  try {
    storage?.setItem(PREFS_KEY, JSON.stringify(prefs))
  }
  catch {
    // private mode: the preference lasts until the page closes
  }
}

function safeStorage(): Storage | null {
  try {
    return localStorage
  }
  catch {
    return null
  }
}

export function isKnownKind(kind: unknown): kind is NotificationKind {
  return typeof kind === 'string' && (NOTIFICATION_KINDS as readonly string[]).includes(kind)
}

export function isNotificationEvent(data: unknown): data is NotificationEvent {
  const n = data as NotificationEvent | null
  return !!n && typeof n.kind === 'string' && typeof n.text === 'string'
}

/** The same notification twice (a resumed stream replays, two tabs) is shown once. The key is
 * the whole event, so two that differ only in an extra field (two sensors, two Builds) are
 * two notifications; it also serves as the notification's `tag`. */
export function notificationKey(n: NotificationEvent): string {
  return JSON.stringify(Object.keys(n).sort().map(k => [k, n[k]]))
}

export type Support = 'unsupported' | NotificationPermission

export function notificationPermission(): Support {
  return typeof Notification === 'undefined' ? 'unsupported' : Notification.permission
}

/** Asks the browser for permission; call it from a click. */
export async function requestNotificationPermission(): Promise<Support> {
  if (typeof Notification === 'undefined') return 'unsupported'
  try {
    return await Notification.requestPermission()
  }
  catch {
    return Notification.permission
  }
}

export interface Shown { title: string, body: string, tag: string, url?: string }

/** Shows a notification. Desktop browsers take `new Notification`, which works with the tab
 * in the background; Android's Chrome refuses it and needs a service worker for it. */
export async function showBrowserNotification(n: Shown): Promise<boolean> {
  if (notificationPermission() !== 'granted') return false
  const options = { body: n.body, tag: n.tag, data: { url: n.url ?? '/activity' } }
  try {
    const notification = new Notification(n.title, options)
    notification.onclick = () => {
      try {
        window.focus()
      }
      catch {
        // not allowed
      }
      notification.close()
    }
    return true
  }
  catch {
    // fall through to the service worker
  }
  try {
    if (!('serviceWorker' in navigator)) return false
    const registration = await navigator.serviceWorker.register('/notification-sw.js')
    let timer: ReturnType<typeof setTimeout> | undefined
    const ready = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), 5000) }),
    ]).finally(() => clearTimeout(timer))
    await (ready ?? registration).showNotification(n.title, options)
    return true
  }
  catch {
    return false
  }
}
