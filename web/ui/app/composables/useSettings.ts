// The Settings form: values from GET /settings, edits as text, per-field problems (checked
// here first, then the backend's `invalid_setting` with its `field`), one all-or-nothing PATCH.
import { computed, reactive, ref } from 'vue'
import type { SettingInfo, Problem } from '~/utils/settings'

export interface Saved {
  changed: Record<string, { from: unknown, to: unknown }>
  applies_later?: Record<string, string>
}

export function useSettings() {
  const info = ref<Record<string, SettingInfo>>({})
  const draft = reactive<Record<string, string>>({})
  const errors = reactive<Record<string, Problem | { code: 'server', error: unknown }>>({})
  const loading = ref(true)
  const loadError = ref<unknown>(null)
  const saving = ref(false)
  const saveError = ref<unknown>(null)
  const saved = ref<Saved | null>(null)

  const keys = computed(() => Object.keys(info.value))
  const dirty = computed(() => keys.value.filter(k => settingChanged(k, draft[k] ?? '', info.value[k]!)))

  function adopt(settings: Record<string, SettingInfo>) {
    info.value = settings
    for (const key of Object.keys(draft)) delete draft[key]
    for (const [key, i] of Object.entries(settings)) draft[key] = String(i.value)
    for (const key of Object.keys(errors)) delete errors[key]
  }

  async function load() {
    loading.value = true
    try {
      const data = await api<{ settings: Record<string, SettingInfo> }>('/settings')
      adopt(data.settings ?? {})
      loadError.value = null
    }
    catch (e) {
      loadError.value = e
    }
    finally {
      loading.value = false
    }
  }

  function setDraft(key: string, text: string) {
    draft[key] = text
    delete errors[key]
  }

  function restoreDefault(key: string) {
    const i = info.value[key]
    if (i) setDraft(key, String(i.default))
  }

  function discard() {
    adopt(info.value)
    saveError.value = null
  }

  async function save(): Promise<boolean> {
    if (saving.value) return false
    saved.value = null
    saveError.value = null
    for (const key of Object.keys(errors)) delete errors[key]
    const body: Record<string, SettingValue> = {}
    for (const key of dirty.value) {
      const p = settingProblem(key, draft[key] ?? '')
      if (p) errors[key] = p
      else body[key] = toSend(key, draft[key] ?? '', info.value[key]!)
    }
    if (Object.keys(errors).length || !Object.keys(body).length) return false
    saving.value = true
    try {
      saved.value = await api<Saved>('/settings', { method: 'PATCH', body })
      await load()
      return true
    }
    catch (e) {
      if (e instanceof ApiError && e.field && e.field in info.value) errors[e.field] = { code: 'server', error: e }
      else saveError.value = e
      return false
    }
    finally {
      saving.value = false
    }
  }

  return { info, draft, errors, keys, dirty, loading, loadError, saving, saveError, saved, load, setDraft, restoreDefault, discard, save }
}
