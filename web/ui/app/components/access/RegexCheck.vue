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
const busy = ref(false)
const error = ref<unknown>(null)
const tested = ref('')

const normalizedHost = computed(() => normalizeDomain(host.value))
const hostProblem = computed(() => host.value.trim() && !looksLikeDomain(normalizedHost.value) ? t('regex.check.hostInvalid') : null)
const canCheck = computed(() => looksLikePattern(props.pattern.trim()) && !hostProblem.value && !busy.value)
const match = computed(() => (checked.value && tested.value ? checked.value.matches[tested.value] : undefined))
const preview = computed(() => checked.value?.preview ?? null)

// which request counts: each change of pattern, scope or list and each new check makes the
// answers to earlier requests stale, and a stale answer is dropped, result or error alike
let current = 0

watch(() => [props.pattern, props.scope, props.list], () => {
  current++
  checked.value = null
  error.value = null
  busy.value = false
})

async function check() {
  if (!canCheck.value) return
  const request = ++current
  busy.value = true
  error.value = null
  const h = host.value.trim() ? normalizedHost.value : ''
  try {
    const result = await api<RegexCheck>(regexCheckPath(props.list, props.pattern.trim(), h, props.list === 'allowlist' ? props.scope : undefined))
    if (request !== current) return
    tested.value = h
    checked.value = result
  }
  catch (e) {
    if (request !== current) return
    checked.value = null
    error.value = e
  }
  finally {
    if (request === current) busy.value = false
  }
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
    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" data-testid="regex-error" />
    <div v-else-if="checked" class="space-y-2 text-sm" data-testid="regex-result">
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
