// The Settings form: values from the resource `settings` (GET /settings), edits as text, per-field
// problems (checked here first, then the backend's `invalid_setting` with its `field`), one
// all-or-nothing PATCH.
import { computed, reactive, ref, watch } from 'vue'
import type { SettingInfo, Problem } from '~/utils/settings'

export interface Saved {
  changed: Record<string, { from: unknown, to: unknown }>
  applies_later?: Record<string, string>
}

export function useSettings() {
  const settings = useResource('settings')
  // the Settings the draft started from (what dirty compares against)
  const info = ref<Record<string, SettingInfo>>({})
  const draft = reactive<Record<string, string>>({})
  // a refusal of one Setting: the module's words for it (asked when shown: they follow the language)
  const errors = reactive<Record<string, Problem | { code: 'server', text: () => string }>>({})
  const loading = settings.loading
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
  /** the Settings changed elsewhere while fields were being edited: taken after save or discard */
  const changedElsewhere = ref(false)

  const keys = computed(() => Object.keys(info.value))
  const dirty = computed(() => keys.value.filter(k => settingChanged(k, draft[k] ?? '', info.value[k]!)))
  const stored = () => settings.data.value?.settings ?? {}

  function adopt(next: Record<string, SettingInfo>) {
    info.value = next
    for (const key of Object.keys(draft)) delete draft[key]
    for (const [key, i] of Object.entries(next)) draft[key] = String(i.value)
    for (const key of Object.keys(errors)) delete errors[key]
    changedElsewhere.value = false
  }

  /** The Settings as stored now, keeping what is typed: a field not edited takes the stored
   * value, an edited one keeps its text (and is no change any more once it was stored). */
  function update(next: Record<string, SettingInfo>) {
    const before = info.value
    info.value = next
    for (const [key, i] of Object.entries(next)) {
      if (!(key in draft) || !before[key] || !settingChanged(key, draft[key] ?? '', before[key]!)) draft[key] = String(i.value)
    }
    changedElsewhere.value = false
  }

  // what is stored changed (the resource read again: its Activity entry, a reconnect): it never
  // throws away what is typed, it waits until nothing is edited (a failed change counts too:
  // agentbox may have saved before it failed). While this browser saves, the save takes it at its
  // end: one that succeeded or failed with a 5xx reads what is stored then; a refused one follows
  // it as here.
  let missed = false
  function follow() {
    if (dirty.value.length) changedElsewhere.value = true
    else adopt(stored())
  }
  watch(() => settings.data.value, (data) => {
    if (!data) return
    if (saving.value) missed = true
    else if (!keys.value.length) adopt(stored())
    else follow()
  }, { immediate: true, flush: 'sync' })      // at once: a save on the way sees it as its own
  watch(() => dirty.value.length, (n) => {
    if (n === 0 && changedElsewhere.value) adopt(stored())
  })

  /** Read what is stored now, after a save: adopting it, or keeping what is typed. */
  async function readNow(keepTyped: boolean) {
    await settings.retry()
    if (settings.error.value) return
    if (keepTyped) update(stored())
    else adopt(stored())
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
    adopt(stored())
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
    missed = false
    let read = false
    try {
      const r = await patch.run(body)
      if (!r.ok) {
        const e = r.error
        if (e instanceof ApiError && e.field && e.field in info.value) errors[e.field] = { code: 'server', text: () => patch.text.value ?? '' }
        // a refusal stored nothing; a failure of agentbox may have come after it saved ("saved, but
        // not applied"): what is stored now, without throwing away what is typed
        if (e instanceof ApiError && e.status >= 500) {
          await readNow(true)
          read = true
        }
        return false
      }
      saved.value = r.data as Saved
      // the form holds a draft, so it reads at once what is stored now, and its own Activity entry
      // reads once more (an exception the owner settled on #215: waiting for the entry could leave
      // the form saving for good)
      await readNow(false)
      read = true
      return true
    }
    finally {
      reading.value = false
      if (missed && !read) follow()
      missed = false
    }
  }

  return { settings, info, draft, errors, keys, dirty, loading, saving, saveError, saved, changedElsewhere, setDraft, restoreDefault, discard, save }
}
