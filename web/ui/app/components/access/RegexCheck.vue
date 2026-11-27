<script setup lang="ts">
// Checks a pattern for a Regex entry (ADR 0013) the way adding it would: whether it keeps the
// rules (else why), whether a host typed in matches it, and for the Allowlist which hosts of
// the scope's proxy log of the last 7 days it would allow (`GET /regex/check`). The result
// belongs to the pattern, scope and list it was made for; it is dropped when one changes, also
// when its request is still on the way then (a confirmation must never follow a preview of
// something else).
import type { RegexCheck, RegexList } from '~/utils/access'

const props = withDefaults(defineProps<{ list: RegexList, pattern: string, scope?: string }>(), { scope: undefined })
/** the check of the current pattern and scope, or null */
const checked = defineModel<RegexCheck | null>('checked', { default: null })
const { t } = useI18n()
const errorText = useErrorText()
const when = useWhen()

const host = ref('')
const tested = ref('')
// the check asked for (the resource `regexCheck`, read anew for each "Test"); none while the
// pattern, scope or list changed since
let asks = 0
const asked = ref<{ list: RegexList, pattern: string, host: string, scope?: string, asked: number } | null>(null)
const checking = useResource('regexCheck', {
  params: () => asked.value ?? { list: props.list, pattern: '', host: '', asked: 0 },
  enabled: () => !!asked.value,
})
const busy = computed(() => !!asked.value && checking.fetching.value)

const normalizedHost = computed(() => normalizeDomain(host.value))
const hostProblem = computed(() => host.value.trim() && !looksLikeDomain(normalizedHost.value) ? t('regex.check.hostInvalid') : null)
const canCheck = computed(() => looksLikePattern(props.pattern.trim()) && !hostProblem.value && !busy.value)
const match = computed(() => (checked.value && tested.value ? checked.value.matches[tested.value] : undefined))
const preview = computed(() => checked.value?.preview ?? null)

// a check belongs to the pattern, scope and list it was made for: one of them changing drops it,
// also when its answer is still on the way (a confirmation must never follow a preview of
// something else)
watch(() => [props.pattern, props.scope, props.list], () => {
  asked.value = null
  checked.value = null
})
// the answer of the check asked for, the copy of its own parameters, taken once: a preview the
// owner may have confirmed does not change under the confirmation (a later read of the copy)
let taken = 0
watch(() => [checking.data.value, checking.error.value, asked.value] as const, ([data, error, ask]) => {
  if (!ask || ask.asked === taken) return
  if (error) {
    checked.value = null
    return
  }
  if (!data) return
  taken = ask.asked
  checked.value = data
  tested.value = ask.host
})
// the rules refuse the pattern: the check's answer (as adding it would answer), not a failed read
const REFUSED = ['invalid_pattern', 'pattern_too_broad']
const refusal = computed(() => {
  const e = asked.value ? checking.error.value : null
  return e instanceof ApiError && REFUSED.includes(e.code) ? e : null
})

function check() {
  if (!canCheck.value) return
  checked.value = null
  asks += 1
  const h = host.value.trim() ? normalizedHost.value : ''
  asked.value = { list: props.list, pattern: props.pattern.trim(), host: h, ...(props.list === 'allowlist' ? { scope: props.scope } : {}), asked: asks }
}

function seen(h: { allowed: number, blocked: number }) {
  return t('regex.preview.seen', { allowed: h.allowed, blocked: h.blocked })
}
</script>

<template>
  <div class="space-y-2" data-testid="regex-check">
    <div class="flex flex-wrap gap-2">
      <UInput
        v-model="host"
        :placeholder="t('regex.check.hostPlaceholder')"
        autocomplete="off"
        spellcheck="false"
        size="sm"
        class="min-w-0 flex-1 font-mono"
        :aria-label="t('regex.check.host')"
        data-testid="regex-host"
        @keydown.enter.prevent="check"
      />
      <UButton
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-flask-conical"
        :label="props.list === 'allowlist' ? t('regex.check.preview') : t('regex.check.test')"
        :loading="busy"
        :disabled="!canCheck"
        data-testid="regex-test"
        @click="check"
      />
    </div>
    <p v-if="hostProblem" class="text-sm text-error">
      {{ hostProblem }}
    </p>
    <UAlert v-if="refusal" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(refusal)" role="alert" data-testid="regex-error" />
    <ReadFailure v-else-if="asked" :resource="checking" data-testid="regex-error" />
    <div v-if="checked" class="space-y-2 text-sm" data-testid="regex-result">
      <p class="text-muted">
        {{ t('regex.check.valid') }}
      </p>
      <p v-if="match !== undefined" :class="match ? 'text-success' : 'text-muted'" data-testid="regex-match" :data-match="match">
        {{ match ? t('regex.check.matches', { host: tested }) : t('regex.check.noMatch', { host: tested }) }}
      </p>
      <template v-if="preview">
        <UAlert v-if="preview.log_unreadable" color="warning" variant="subtle" icon="i-lucide-triangle-alert" :title="t('regex.preview.logUnreadable')" :description="preview.log_unreadable" data-testid="regex-preview-log" />
        <p v-else-if="!preview.hosts.length" class="text-muted" data-testid="regex-preview-none">
          {{ t('regex.preview.none') }}
        </p>
        <template v-else>
          <p class="font-medium text-highlighted">
            {{ t('regex.preview.title', preview.hosts.length + preview.more) }}
          </p>
          <ul class="max-h-48 divide-y divide-default overflow-y-auto rounded-md border border-default" data-testid="regex-preview">
            <li v-for="h in preview.hosts" :key="h.host" class="flex flex-wrap items-center gap-2 px-3 py-1.5" data-testid="regex-preview-host" :data-host="h.host" :data-strong="h.strong">
              <span class="min-w-0 flex-1 break-all font-mono text-xs text-highlighted">{{ h.host }}</span>
              <span class="text-xs text-muted">{{ seen(h) }}</span>
              <UBadge v-if="h.strong" color="error" variant="subtle" size="sm" :label="t('regex.preview.staysBlocked')" />
            </li>
          </ul>
          <p v-if="preview.more" class="text-xs text-muted">
            {{ t('regex.preview.more', preview.more) }}
          </p>
          <UAlert
            v-if="preview.truncated"
            color="warning"
            variant="subtle"
            icon="i-lucide-triangle-alert"
            :title="preview.reaches_back ? t('regex.preview.truncatedSince', { when: when.at(preview.reaches_back) }) : t('regex.preview.truncated')"
            data-testid="regex-preview-truncated"
          />
        </template>
        <UAlert v-if="preview.watchlist_unreadable" color="warning" variant="subtle" icon="i-lucide-triangle-alert" :title="t('regex.preview.watchlistUnreadable')" :description="preview.watchlist_unreadable" data-testid="regex-preview-watchlist" />
      </template>
    </div>
  </div>
</template>
