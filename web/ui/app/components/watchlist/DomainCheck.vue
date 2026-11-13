<script setup lang="ts">
// Checks a domain against the Watchlist, the Feeds and the patterns: its Signals and why,
// what that means (Alert, blocked also on the Allowlist, only lifting the Signal lets it through), and a Harmless
// mark for a false positive.
import type { DomainCheck, HarmlessMark } from '~/utils/access'

const props = defineProps<{ harmless: HarmlessMark[] }>()
const emit = defineEmits<{ 'harmless-changed': [] }>()
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const input = ref('')
const result = ref<DomainCheck | null>(null)
const busy = ref(false)
const marking = ref(false)
const error = ref<unknown>(null)
const confirmUnmark = ref(false)

const domain = computed(() => normalizeDomain(input.value))
const valid = computed(() => looksLikeDomain(domain.value))
/** the domain itself carries a mark (a parent's mark is removed on the parent, a pattern in the list) */
const ownMark = computed(() => !!result.value && props.harmless.some(m => m.domain === result.value!.domain))

const verdict = computed(() => {
  const r = result.value
  if (!r) return null
  if (r.harmless) return { color: 'info' as const, icon: 'i-lucide-shield-check', title: t(r.harmless_regex ? 'watchlist.check.verdict.harmlessRegex' : 'watchlist.check.verdict.harmless', { mark: r.harmless }) }
  if (r.strong) return { color: 'error' as const, icon: 'i-lucide-shield-alert', title: t('watchlist.check.verdict.strong') }
  if (r.alert) return { color: 'warning' as const, icon: 'i-lucide-triangle-alert', title: t('watchlist.check.verdict.alert') }
  if (r.legitimate) return { color: 'success' as const, icon: 'i-lucide-shield-check', title: t(r.legitimate_regex ? 'watchlist.check.verdict.legitimateRegex' : 'watchlist.check.verdict.legitimate', { domain: r.legitimate }) }
  return { color: 'success' as const, icon: 'i-lucide-shield-check', title: t('watchlist.check.verdict.clean') }
})
const consequences = computed(() => {
  const r = result.value
  if (!r) return []
  const out: string[] = []
  if (r.alert) out.push(t('watchlist.check.raisesAlerts'))
  else if (r.signals.length && r.harmless) out.push(t('watchlist.check.noAlerts'))
  if (r.strong) out.push(t('watchlist.check.blocked'), t('watchlist.check.onlyLift'))
  return out
})

async function check(d?: string) {
  if (d !== undefined) input.value = d
  if (!valid.value) return
  busy.value = true
  error.value = null
  try {
    result.value = await api<DomainCheck>(`/watchlist/check?domain=${encodeURIComponent(domain.value)}`)
  }
  catch (e) {
    result.value = null
    error.value = e
  }
  finally {
    busy.value = false
  }
}

async function setMark(on: boolean) {
  const d = result.value?.domain
  if (!d) return
  marking.value = true
  error.value = null
  try {
    await api(`/watchlist/harmless/${encodeURIComponent(d)}`, { method: on ? 'PUT' : 'DELETE' })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t(on ? 'watchlist.harmless.marked' : 'watchlist.harmless.unmarked', { domain: d }) })
    confirmUnmark.value = false
    emit('harmless-changed')
    await check(d)
  }
  catch (e) {
    error.value = e
  }
  finally {
    marking.value = false
  }
}

defineExpose({ check })
</script>

<template>
  <section class="space-y-3 rounded-lg border border-default p-4" data-testid="domain-check">
    <div>
      <h3 class="font-medium text-highlighted">
        {{ t('watchlist.check.title') }}
      </h3>
      <p class="text-sm text-muted">
        {{ t('watchlist.check.intro') }}
      </p>
    </div>
    <form class="flex flex-wrap gap-2" novalidate @submit.prevent="check()">
      <UInput
        v-model="input"
        icon="i-lucide-search"
        placeholder="example.com"
        autocomplete="off"
        spellcheck="false"
        class="min-w-0 flex-1 font-mono"
        :aria-label="t('watchlist.check.domain')"
        data-testid="check-input"
      />
      <UButton type="submit" :label="t('watchlist.check.submit')" :loading="busy" :disabled="!valid" data-testid="check-submit" />
    </form>

    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />

    <div v-if="result && verdict" class="space-y-3" data-testid="check-result" :data-strong="result.strong" :data-alert="result.alert">
      <UAlert :color="verdict.color" variant="subtle" :icon="verdict.icon" :title="`${result.domain}: ${verdict.title}`">
        <template v-if="consequences.length" #description>
          <ul class="list-disc ps-4">
            <li v-for="c in consequences" :key="c">
              {{ c }}
            </li>
          </ul>
        </template>
      </UAlert>
      <WatchlistSignalList v-if="result.signals.length" :signals="result.signals" />
      <p v-else class="text-sm text-muted">
        {{ t('watchlist.check.noSignals') }}
      </p>
      <div v-if="result.signals.length" class="flex flex-wrap items-center gap-2">
        <UButton
          v-if="!result.harmless"
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-bell-off"
          :label="t('watchlist.harmless.mark')"
          :loading="marking"
          data-testid="check-mark-harmless"
          @click="setMark(true)"
        />
        <UButton
          v-else-if="ownMark"
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-bell"
          :label="t('watchlist.harmless.unmark')"
          data-testid="check-unmark-harmless"
          @click="confirmUnmark = true"
        />
        <span class="text-xs text-muted">{{ t('watchlist.harmless.explain') }}</span>
      </div>
    </div>

    <AccessConfirmDialog
      v-model:open="confirmUnmark"
      :title="t('watchlist.harmless.unmarkTitle', { domain: result?.domain })"
      :description="t('watchlist.harmless.unmarkText')"
      :confirm-label="t('watchlist.harmless.unmark')"
      :busy="marking"
      @confirm="setMark(false)"
    />
  </section>
</template>
