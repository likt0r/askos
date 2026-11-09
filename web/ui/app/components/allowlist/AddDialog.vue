<script setup lang="ts">
// Adds an Allowlist entry: scope (all VMs or one), domain or pattern, duration, note. Also used
// from a VM's blocked list with the domain and scope given. Destinations with strong Signals are
// refused by the backend (only the CLI can force them): shown with their Signals. A pattern (a
// Regex entry, ADR 0013) is added only after its preview, the hosts of the proxy log it would
// allow, and an explicit confirmation; it never allows a destination with strong Signals.
import type { AllowlistEntry, EntryDuration, RegexCheck } from '~/utils/access'

const props = withDefaults(defineProps<{
  /** a fixed domain (allowing a blocked destination); else typed in */
  domain?: string
  /** the scope chosen at first: `all` or a VM */
  scope?: string
  /** VM names to choose from (the scope is fixed when empty) */
  vms?: string[]
}>(), { domain: undefined, scope: 'all', vms: () => [] })
const emit = defineEmits<{ added: [entry: AllowlistEntry] }>()
const open = defineModel<boolean>('open', { default: false })

const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const kind = ref<'domain' | 'regex'>('domain')
const domain = ref('')
const pattern = ref('')
const scope = ref('all')
const duration = ref<EntryDuration>('1h')
const note = ref('')
const busy = ref(false)
const error = ref<unknown>(null)
const refused = ref<{ domain: string, scope: string } | null>(null)
const checked = ref<RegexCheck | null>(null)
const confirmed = ref(false)

const fixedDomain = computed(() => props.domain !== undefined)
const isRegex = computed(() => kind.value === 'regex')
const scopes = computed(() => [
  { value: 'all', label: t('allowlist.scope.all') },
  ...props.vms.map(vm => ({ value: vm, label: t('allowlist.scope.vm', { vm }) })),
])
const durations = computed(() => ENTRY_DURATIONS.map(d => ({ value: d, label: t(`allowlist.durations.${d}`) })))
const normalized = computed(() => normalizeDomain(domain.value))
const domainProblem = computed(() => domain.value.trim() && !looksLikeDomain(normalized.value) ? t('allowlist.add.domainInvalid') : null)
const patternProblem = computed(() => pattern.value.trim() && !looksLikePattern(pattern.value.trim()) ? t('regex.patternInvalid') : null)
// a pattern only once its preview was shown and confirmed
const ready = computed(() => isRegex.value
  ? looksLikePattern(pattern.value.trim()) && !!checked.value && confirmed.value
  : looksLikeDomain(normalized.value))
const canSubmit = computed(() => ready.value && !busy.value && note.value.length <= 200)

watch(open, (isOpen) => {
  if (!isOpen) return
  kind.value = 'domain'
  domain.value = props.domain ?? ''
  pattern.value = ''
  scope.value = props.scope
  duration.value = '1h'
  note.value = ''
  error.value = null
  refused.value = null
  checked.value = null
  confirmed.value = false
}, { immediate: true })
// a new pattern or scope needs its own preview and confirmation
watch([pattern, scope, kind], () => {
  confirmed.value = false
  error.value = null
})

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = null
  refused.value = null
  const what = isRegex.value ? pattern.value.trim() : normalized.value
  const body: Record<string, string | boolean> = isRegex.value
    ? { scope: scope.value, regex: what, duration: duration.value, confirm: true }
    : { scope: scope.value, domain: what, duration: duration.value }
  if (note.value.trim()) body.note = note.value.trim()
  try {
    const entry = await api<AllowlistEntry>('/allowlist', { method: 'POST', body })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t(isRegex.value ? 'allowlist.add.doneRegex' : 'allowlist.add.done', { domain: what }) })
    emit('added', entry)
    open.value = false
  }
  catch (e) {
    if (e instanceof ApiError && e.code === 'domain_suspicious' && !isRegex.value) refused.value = { domain: what, scope: scope.value }
    else error.value = e
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <UModal v-model:open="open" :title="t('allowlist.add.title')" :description="isRegex ? t('allowlist.add.descriptionRegex') : t('allowlist.add.description')">
    <template #body>
      <form class="space-y-4" novalidate data-testid="allowlist-add" @submit.prevent="submit">
        <AccessEntryKind v-if="!fixedDomain" v-model="kind" />
        <UFormField v-if="!isRegex" :label="t('allowlist.add.domain')" name="domain" :error="domainProblem ?? undefined" :help="fixedDomain ? undefined : t('allowlist.add.domainHelp')">
          <UInput
            v-model="domain"
            :disabled="fixedDomain"
            placeholder="docs.rs"
            autocomplete="off"
            spellcheck="false"
            class="w-full font-mono"
            data-testid="add-domain"
          />
        </UFormField>
        <UFormField v-else :label="t('regex.pattern')" name="regex" :error="patternProblem ?? undefined" :help="t('allowlist.add.regexHelp')">
          <UInput
            v-model="pattern"
            placeholder="^mirror[0-9]+\.vendor\.example$"
            autocomplete="off"
            spellcheck="false"
            class="w-full font-mono"
            data-testid="add-regex"
          />
        </UFormField>
        <UFormField v-if="props.vms.length" :label="t('allowlist.add.scope')" name="scope">
          <USelect v-model="scope" :items="scopes" class="w-full" data-testid="add-scope" />
        </UFormField>
        <p v-else class="text-sm text-muted">
          {{ props.scope === 'all' ? t('allowlist.scope.all') : t('allowlist.scope.vm', { vm: props.scope }) }}
        </p>
        <template v-if="isRegex">
          <AccessRegexCheck v-model:checked="checked" list="allowlist" :pattern="pattern" :scope="scope" />
          <UCheckbox
            v-model="confirmed"
            :disabled="!checked"
            :label="t('allowlist.add.confirmRegex')"
            :description="t('allowlist.add.confirmRegexHelp')"
            data-testid="add-confirm"
          />
        </template>
        <UFormField :label="t('allowlist.add.duration')" name="duration">
          <div class="flex flex-wrap gap-2" role="radiogroup" :aria-label="t('allowlist.add.duration')" data-testid="add-duration">
            <UButton
              v-for="d in durations"
              :key="d.value"
              role="radio"
              :aria-checked="duration === d.value"
              :color="duration === d.value ? 'primary' : 'neutral'"
              :variant="duration === d.value ? 'solid' : 'outline'"
              size="sm"
              :label="d.label"
              :data-duration="d.value"
              @click="duration = d.value"
            />
          </div>
        </UFormField>
        <UFormField :label="t('allowlist.add.note')" name="note" :hint="t('allowlist.add.optional')" :error="note.length > 200 ? t('allowlist.add.noteTooLong') : undefined">
          <UInput v-model="note" :placeholder="t('allowlist.add.notePlaceholder')" class="w-full" data-testid="add-note" />
        </UFormField>

        <AllowlistRefusal v-if="refused" :domain="refused.domain" :scope="refused.scope" />
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" data-testid="add-error" />
        <button type="submit" class="hidden" />
      </form>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" @click="open = false" />
        <UButton icon="i-lucide-check" :label="t('allowlist.add.submit')" :loading="busy" :disabled="!canSubmit" data-testid="add-submit" @click="submit" />
      </div>
    </template>
  </UModal>
</template>
