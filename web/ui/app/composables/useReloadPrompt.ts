// A new Control Room build was installed: the resource `version` (GET /version, kept current by
// the `version` event) names a build that differs from the one this page loaded with. Open pages
// offer to reload. The page's own version is the UI build of the first copy of `version` (read when
// the app shell opens); later ones, after an event, a reconnect or a sign-in, may name a newer
// build, not this page's.
import { computed, ref, watch } from 'vue'

const own = ref<string | null>(null)
const dismissed = ref<string | null>(null)

/** GET /version; its failure shows nothing (without a version nothing is offered). */
function useVersion() {
  return useResource('version', { extra: true }).data
}

/** The Release of Agent Box the Host runs (GET /version), once known. */
export function useRelease() {
  const version = useVersion()
  return computed(() => (typeof version.value?.release === 'string' ? version.value.release : null))
}

export function useReloadPrompt() {
  const version = useVersion()
  watch(() => version.value?.version, (v) => {
    if (own.value === null && typeof v === 'string') own.value = v
  }, { immediate: true })
  // the UI build installed now: the resource, which the `version` event keeps current
  const installed = computed(() => version.value ?? null)
  const available = computed(() => {
    const v = installed.value
    return !!v && v.ui === 'nuxt' && !!own.value && typeof v.version === 'string' && v.version !== own.value
  })
  const visible = computed(() => available.value && dismissed.value !== installed.value?.version)
  return {
    visible,
    reload: () => window.location.reload(),
    dismiss() {
      dismissed.value = installed.value?.version ?? null
    },
  }
}

/** Tests only. */
export function resetReloadPromptForTests() {
  own.value = null
  dismissed.value = null
}
