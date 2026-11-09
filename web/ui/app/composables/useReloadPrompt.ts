// A new Control Room build was installed: the `version` event carries a version that differs
// from the one this page loaded with (GET /version at start). Open pages offer to reload.
import { computed, ref } from 'vue'

const own = ref<string | null>(null)
const release = ref<string | null>(null)
const dismissed = ref<string | null>(null)
let asked = false

/** The version of this page, asked once after it loaded. */
export async function loadOwnVersion() {
  if (asked) return
  asked = true
  try {
    const data = await api<{ version?: string, ui?: string, release?: string | null }>('/version')
    if (typeof data?.version === 'string') own.value = data.version
    if (typeof data?.release === 'string') release.value = data.release
  }
  catch {
    asked = false      // the next page load asks again; without a version nothing is offered
  }
}

/** The Release of Agent Box the Host runs (GET /version), once known. */
export function useRelease() {
  return release
}

export function useReloadPrompt() {
  const live = useLive()
  const available = computed(() => {
    const v = live.state.version
    return !!v && v.ui === 'nuxt' && !!own.value && v.version !== own.value
  })
  const visible = computed(() => available.value && dismissed.value !== live.state.version?.version)
  return {
    visible,
    reload: () => window.location.reload(),
    dismiss() {
      dismissed.value = live.state.version?.version ?? null
    },
  }
}

/** Tests only. */
export function resetReloadPromptForTests() {
  own.value = null
  release.value = null
  dismissed.value = null
  asked = false
}
