<script setup lang="ts">
// Harmless marks: destinations the owner judged false positives. They raise no more Alerts
// (except for Signals that do not depend on the name); blocking stays as it is. A mark is a
// domain (with its subdomains) or a pattern (a Regex entry, ADR 0013), marked and shown verbatim.
import type { HarmlessMark, RegexCheck } from '~/utils/access'

const props = defineProps<{ marks: HarmlessMark[], loaded: boolean, error?: unknown }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const kind = ref<'domain' | 'regex'>('domain')
const input = ref('')
const patternInput = ref('')
const checked = ref<RegexCheck | null>(null)
const busy = ref(false)
const error = ref<unknown>(null)
const removing = ref<HarmlessMark | null>(null)
const removeOpen = computed({
  get: () => removing.value !== null,
  set: (v: boolean) => { if (!v && !busy.value) removing.value = null },
})

const domain = computed(() => normalizeDomain(input.value))
const pattern = computed(() => patternInput.value.trim())
const valid = computed(() => (kind.value === 'regex' ? looksLikePattern(pattern.value) : looksLikeDomain(domain.value)))
const path = (m: HarmlessMark) => entryPath('/watchlist/harmless', m)
const key = (m: HarmlessMark) => (m.regex != null ? `regex:${m.regex}` : m.domain ?? '')
const removingValue = computed(() => (removing.value ? entryValue(removing.value) : ''))

async function add() {
  if (!valid.value) return
  busy.value = true
  error.value = null
  const mark: HarmlessMark = kind.value === 'regex' ? { domain: null, regex: pattern.value } : { domain: domain.value, regex: null }
  try {
    await api(path(mark), { method: 'PUT' })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('watchlist.harmless.marked', { domain: entryValue(mark) }) })
    input.value = ''
    patternInput.value = ''
    emit('changed')
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}

async function remove() {
  const m = removing.value
  if (!m) return
  busy.value = true
  error.value = null
  try {
    await api(path(m), { method: 'DELETE' })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('watchlist.harmless.unmarked', { domain: entryValue(m) }) })
    busy.value = false
    removing.value = null
    emit('changed')
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="space-y-3 rounded-lg border border-default p-4" data-testid="harmless">
    <div>
      <h3 class="font-medium text-highlighted">
        {{ t('watchlist.harmless.title') }}
      </h3>
      <p class="text-sm text-muted">
        {{ t('watchlist.harmless.intro') }}
      </p>
    </div>
    <form class="flex flex-wrap gap-2" novalidate @submit.prevent="add">
      <AccessEntryKind v-model="kind" size="xs" class="w-full" />
      <UInput
        v-if="kind === 'domain'"
        v-model="input"
        placeholder="false-positive.example"
        autocomplete="off"
        spellcheck="false"
        class="min-w-0 flex-1 font-mono"
        :aria-label="t('watchlist.harmless.domain')"
        data-testid="harmless-input"
      />
      <UInput
        v-else
        v-model="patternInput"
        placeholder="^ci[0-9]+\.builds\.example$"
        autocomplete="off"
        spellcheck="false"
        class="min-w-0 flex-1 font-mono"
        :aria-label="t('regex.pattern')"
        data-testid="harmless-regex"
      />
      <UButton type="submit" icon="i-lucide-bell-off" :label="t('watchlist.harmless.mark')" :loading="busy && !removing" :disabled="!valid" data-testid="harmless-add" />
      <AccessRegexCheck v-if="kind === 'regex'" v-model:checked="checked" list="harmless" :pattern="patternInput" class="w-full" />
    </form>
    <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(props.error)" />
    <UAlert v-if="error && !removing" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" data-testid="harmless-error" />
    <p v-if="props.loaded && !props.marks.length" class="text-sm text-muted" data-testid="harmless-empty">
      {{ t('watchlist.harmless.empty') }}
    </p>
    <ul v-else-if="props.marks.length" class="divide-y divide-default rounded-md border border-default">
      <li v-for="m in props.marks" :key="key(m)" class="flex items-center gap-2 px-3 py-1.5" data-testid="harmless-row" :data-domain="m.domain ?? undefined" :data-regex="m.regex ?? undefined">
        <UTooltip v-if="m.regex != null" :text="t('regex.badgeHint')">
          <UBadge color="warning" variant="outline" size="sm" icon="i-lucide-regex" :label="t('regex.badge')" data-testid="entry-regex" />
        </UTooltip>
        <span class="min-w-0 flex-1 break-all font-mono text-sm">{{ entryValue(m) }}</span>
        <UButton
          color="error"
          variant="ghost"
          size="sm"
          icon="i-lucide-trash-2"
          :aria-label="t('watchlist.harmless.unmarkOne', { domain: entryValue(m) })"
          data-testid="harmless-remove"
          @click="removing = m"
        />
      </li>
    </ul>

    <AccessConfirmDialog
      v-model:open="removeOpen"
      :title="t('watchlist.harmless.unmarkTitle', { domain: removingValue })"
      :description="t('watchlist.harmless.unmarkText')"
      :confirm-label="t('watchlist.harmless.unmark')"
      :busy="busy"
      :error="error && removing ? errorText(error) : null"
      @confirm="remove"
    />
  </section>
</template>
