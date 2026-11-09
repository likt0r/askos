// Browser notifications: the per-browser preferences, the permission, and the listener
// that turns `notification` events into browser notifications.
import { onScopeDispose, ref } from 'vue'

const prefs = ref<Prefs>(defaultPrefs())
const support = ref<Support>('unsupported')
let loaded = false
const shown: string[] = []

function ensureLoaded() {
  if (loaded) return
  loaded = true
  prefs.value = loadPrefs()
  support.value = notificationPermission()
}

function forget(key: string) {
  const i = shown.indexOf(key)
  if (i >= 0) shown.splice(i, 1)
}

/** Preferences and permission, for the Settings page. */
export function useNotificationPrefs() {
  ensureLoaded()
  return {
    prefs,
    support,
    setKind(kind: NotificationKind, on: boolean) {
      prefs.value = { ...prefs.value, [kind]: on }
      savePrefs(prefs.value)
    },
    async enable() {
      support.value = await requestNotificationPermission()
      return support.value
    },
    refresh() {
      support.value = notificationPermission()
    },
  }
}

/** The words of a notification in the UI language. */
export function useNotificationText() {
  const { t, te } = useI18n()
  return (n: NotificationEvent): { title: string, body: string } => {
    const key = `notifications.text.${n.kind}`
    const params = {
      vm: n.vm ?? '',
      domain: String(n.domain ?? ''),
      signals: Array.isArray(n.signals) ? n.signals.join(', ') : '',
      request: String(n.request ?? ''),
      image: String(n.image ?? ''),
      build: String(n.build ?? ''),
      value: String(n.value ?? ''),
      threshold: String(n.threshold ?? ''),
      sensor: String(n.device ?? n.reading ?? ''),
    }
    return {
      title: te(`notifications.title.${n.kind}`) ? t(`notifications.title.${n.kind}`, params) : t('notifications.title.other'),
      body: te(key) ? t(key, params) : n.text,
    }
  }
}

/** Shows the browser notification for each `notification` event (once per event, whatever
 * the preferences say about its kind). Call once, from the app shell. */
export function useNotificationListener() {
  ensureLoaded()
  const live = useLive()
  const words = useNotificationText()
  const off = live.on('notification', (data: unknown) => {
    if (!isNotificationEvent(data) || !isKnownKind(data.kind) || !prefs.value[data.kind]) return
    if (notificationPermission() !== 'granted') return      // not remembered: allowing it later shows the next one
    const key = notificationKey(data)
    if (shown.includes(key)) return
    shown.push(key)
    if (shown.length > 200) shown.shift()
    const { title, body } = words(data)
    void showBrowserNotification({ title, body, tag: key, url: data.kind === 'host-reboot' || data.kind === 'temperature' ? '/system' : '/activity' })
      .then((done) => {
        if (!done) forget(key)
      })
  })
  onScopeDispose(off)
}

/** Tests only. */
export function resetNotificationsForTests() {
  loaded = false
  shown.length = 0
}
