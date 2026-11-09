// Shared by the tests of Activity, Host bar, System, Settings and notifications: the app on a
// page, against a fake backend and a fake event stream.
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { beforeEach, afterEach, vi } from 'vitest'
import App from '~/app.vue'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { resetHostForTests } from '~/composables/useHost'
import { resetNotificationsForTests } from '~/composables/useNotifications'
import { resetReloadPromptForTests } from '~/composables/useReloadPrompt'
import { fakeApi, FakeEventSource, SIGNED_IN } from './fakes'

type Routes = Parameters<typeof fakeApi>[0]

export function useFreshApp() {
  beforeEach(async () => {
    resetSessionForTests()
    resetLiveForTests()
    resetHostForTests()
    resetNotificationsForTests()
    resetReloadPromptForTests()
    FakeEventSource.reset()
    localStorage.clear()
    vi.stubGlobal('EventSource', FakeEventSource)
    await useNuxtApp().$i18n.setLocale('en')
  })
  afterEach(() => {
    resetLiveForTests()
    resetSessionForTests()
  })
}

export async function openPage(route: string, routes: Routes = {}) {
  const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /version': { status: 200, body: { api: 1, version: 'aaaa', ui: 'nuxt', installed: null } }, ...routes })
  const wrapper = await mountSuspended(App, { route })
  await flushPromises()
  const es = FakeEventSource.last
  es.emit('hello', { stream: 'abcd1234', resumed: false })
  await flushPromises()
  return { wrapper, api, es }
}

export const ok = (body: unknown) => ({ status: 200, body })

/** A host sample as the backend sends it. */
export function reading(id: string, kind: string, value: number | null, extra: Record<string, unknown> = {}) {
  const limits: Record<string, number | null> = { cpu_load: 90, cpu_temp: 85, memory: 90, gpu_temp: 87, nvme_temp: 70, swap: 50, swap_activity: 10 }
  const units: Record<string, string> = { cpu_load: '%', cpu_temp: '°C', memory: '%', gpu_temp: '°C', nvme_temp: '°C', swap: '%', swap_activity: 'MiB/s' }
  const threshold = limits[kind] ?? null
  return {
    id, kind, device: null, source: 'test', unit: units[kind] ?? '%', value, threshold,
    over: value !== null && threshold !== null && value >= threshold, ...extra,
  }
}

export function hostSample(readings: ReturnType<typeof reading>[], vms: Record<string, number> = {}, time = 1000) {
  return {
    time,
    readings,
    vms: Object.fromEntries(Object.entries(vms).map(([k, v]) => [k, { memory_bytes: v }])),
  }
}
