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
  // a save runs until the read after it is done
  const reading = ref(false)
  const saving = computed(() => patch.busy.value || reading.value)
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

  /** The Settings as stored now, keeping what is typed: a field not edited takes the stored
   * value, an edited one keeps its text (and is no change any more once it was stored). */
  function update(settings: Record<string, SettingInfo>) {
    const before = info.value
    info.value = settings
    for (const [key, i] of Object.entries(settings)) {
      if (!(key in draft) || !before[key] || !settingChanged(key, draft[key] ?? '', before[key]!)) draft[key] = String(i.value)
    }
  }

  async function load(keepTyped = false) {
    loading.value = true
    try {
      const data = await api<{ settings: Record<string, SettingInfo> }>('/settings')
      if (keepTyped) update(data.settings ?? {})
      else adopt(data.settings ?? {})
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
  // the read after its save brings what changed. The first entry after its own save is taken for
  // that save's: it is never said to come from elsewhere (it is still read when nothing is typed).
  let ownEntry = false
  useReloadOnActivity(() => {
    if (saving.value) return
    const own = ownEntry
    ownEntry = false
    if (!dirty.value.length) void load()
    else if (!own) changedElsewhere.value = true
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
    reading.value = true
    ownEntry = true
    try {
      const r = await patch.run(body)
      if (!r.ok) {
        const e = r.error
        if (e instanceof ApiError && e.field && e.field in info.value) errors[e.field] = { code: 'server', text: () => patch.text.value ?? '' }
        // a refusal stored nothing; a failure of agentbox may have come after it saved ("saved, but
        // not applied"): what is stored now, without throwing away what is typed
        if (e instanceof ApiError && e.status >= 500) await load(true)
        return false
      }
      saved.value = r.data as Saved
      // the form holds a draft, so it reads at once what is stored now, and its own Activity entry
      // reads once more (an exception the owner settled on #215: waiting for the entry could leave
      // the form saving for good)
      await load()
      return true
    }
    finally {
      reading.value = false
    }
  }

  return { info, draft, errors, keys, dirty, loading, loadError, saving, saveError, saved, changedElsewhere, load, setDraft, restoreDefault, discard, save }
}
