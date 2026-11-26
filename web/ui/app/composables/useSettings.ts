// The Settings form: values from GET /settings, edits as text, per-field problems (checked
// here first, then the backend's `invalid_setting` with its `field`), one all-or-nothing PATCH.
import { computed, reactive, ref, watch } from 'vue'
import type { SettingInfo, Problem } from '~/utils/settings'

export interface Saved {
  changed: Record<string, { from: unknown, to: unknown }>
  applies_later?: Record<string, string>
}

export function useSettings() {
  const info = ref<Record<string, SettingInfo>>({})
  const draft = reactive<Record<string, string>>({})
  // a refusal of one Setting: the module's words for it (asked when shown: they follow the language)
  const errors = reactive<Record<string, Problem | { code: 'server', text: () => string }>>({})
  const loading = ref(true)
  const loadError = ref<unknown>(null)
  const patch = useChange('setSettings', { form: true })
  const saving = patch.busy
  // a refusal of one Setting shows at its field (errors); any other failure below the form
  const saveError = computed(() => {
    const e = patch.shown.value
    return e instanceof ApiError && e.field && e.field in info.value ? null : e
  })
  const saved = ref<Saved | null>(null)
  /** the Settings changed elsewhere while fields were being edited: loaded after save or discard */
  const changedElsewhere = ref(false)

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
      changedElsewhere.value = false
      loadError.value = null
    }
    catch (e) {
      loadError.value = e
    }
    finally {
      loading.value = false
    }
  }

  // a change from elsewhere never throws away what is typed: it waits until nothing is edited (a
  // failed change counts too: agentbox may have saved before it failed). While this browser saves,
  // its own read after the save brings what changed.
  useReloadOnActivity(() => {
    if (saving.value) return
    if (dirty.value.length) changedElsewhere.value = true
    else void load()
  }, { kinds: ['settings'] })
  watch(() => dirty.value.length, (n) => {
    if (n === 0 && changedElsewhere.value) void load()
  })

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
    patch.reset()
  }

  async function save(): Promise<boolean> {
    if (saving.value) return false
    saved.value = null
    patch.reset()
    for (const key of Object.keys(errors)) delete errors[key]
    const body: Record<string, SettingValue> = {}
    for (const key of dirty.value) {
      const p = settingProblem(key, draft[key] ?? '')
      if (p) errors[key] = p
      else body[key] = toSend(key, draft[key] ?? '', info.value[key]!)
    }
    if (Object.keys(errors).length || !Object.keys(body).length) return false
    const r = await patch.run(body)
    if (!r.ok) {
      const e = r.error
      if (e instanceof ApiError && e.field && e.field in info.value) errors[e.field] = { code: 'server', text: () => patch.text.value ?? '' }
      return false
    }
    saved.value = r.data as Saved
    // the form holds a draft, so it reads at once what is stored now, and its own Activity entry
    // reads once more (an exception the owner settled on #215: waiting for the entry could leave
    // the form saving for good)
    await load()
    return true
  }

  return { info, draft, errors, keys, dirty, loading, loadError, saving, saveError, saved, changedElsewhere, load, setDraft, restoreDefault, discard, save }
}
