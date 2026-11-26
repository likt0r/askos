<script setup lang="ts">
// Adds an Allowlist entry: scope (all VMs or one), domain or pattern, duration, note. Also used
// from a VM's blocked list with the domain and scope given. Destinations with strong Signals are
// refused by the backend (no entry lets them through, ADR 0021): shown with their Signals and how
// to lift them. A pattern (a Regex entry, ADR 0013) is added only after its preview, the hosts of
// the proxy log it would allow, and an explicit confirmation; it never allows a destination with
// strong Signals either. Where a VM's more specific Forbid still wins over the new entry (ADR 0024),
// the dialog stays open and names them.
import type { AllowlistEntry, EntryDuration, RegexCheck } from '~/utils/access'

const props = withDefaults(defineProps<{
  /** a fixed domain (allowing a blocked destination); else typed in */
  domain?: string
  /** the scope chosen at first: `all` or a VM */
  scope?: string
  /** VM names to choose from (the scope is fixed when empty) */
  vms?: string[]
}>(), { domain: undefined, scope: 'all', vms: () => [] })
const open = defineModel<boolean>('open', { default: false })

const { t } = useI18n()
const toast = useToast()

const kind = ref<'domain' | 'regex'>('domain')
const domain = ref('')
const pattern = ref('')
const scope = ref('all')
const duration = ref<EntryDuration>('1h')
const note = ref('')
// a domain with strong Signals is refused: the dialog shows them and how to lift them
const add = useChange('allow', { form: true, own: ['domain_suspicious'] })
const busy = add.busy
const refused = ref<string | null>(null)
const checked = ref<RegexCheck | null>(null)
const confirmed = ref(false)
const stillForbidden = ref<{ vm: string, domain: string }[]>([])

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
// the preview shown belongs to the pattern and scope as they are now (the check names what it
// was made for), whatever happened in between
const previewed = computed(() => !!checked.value && checked.value.regex === pattern.value.trim()
  && checked.value.scope === scope.value)
// a pattern only once its preview was shown and confirmed
const ready = computed(() => isRegex.value
  ? looksLikePattern(pattern.value.trim()) && previewed.value && confirmed.value
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
  add.reset()
  refused.value = null
  checked.value = null
  confirmed.value = false
  stillForbidden.value = []
}, { immediate: true })
// a new pattern or scope needs its own preview and confirmation; so does coming back from Domain
// (the check is not there to drop its preview while the form shows a domain)
watch([pattern, scope, kind], () => {
  checked.value = null
  confirmed.value = false
  add.reset()
})

async function submit() {
  if (!canSubmit.value) return
  refused.value = null
  const what = isRegex.value ? pattern.value.trim() : normalized.value
  const r = await add.run({ scope: scope.value, ...(isRegex.value ? { regex: what } : { domain: what }), duration: duration.value, note: note.value.trim() })
  if (!r.ok) {
    // a pattern is never refused for it (no single destination to check)
    if (r.error instanceof ApiError && r.error.code === 'domain_suspicious') refused.value = what
    return
  }
  const entry = r.data as AllowlistEntry | null
  toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t(isRegex.value ? 'allowlist.add.doneRegex' : 'allowlist.add.done', { domain: what }) })
  const winning = Array.isArray(entry?.forbidden_for) ? entry.forbidden_for : []
  if (winning.length) stillForbidden.value = winning
  else open.value = false
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
            :disabled="!previewed"
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

        <AllowlistRefusal v-if="refused" :domain="refused" />
        <UAlert
          v-if="stillForbidden.length"
          color="warning"
          variant="subtle"
          icon="i-lucide-ban"
          :title="t('allowlist.add.stillForbidden', { forbids: stillForbidden.map(f => `${f.vm} (${f.domain})`).join(', ') })"
          data-testid="add-still-forbidden"
        />
        <ChangeFailure :error="add.shown.value" data-testid="add-error" />
        <button type="submit" class="hidden" />
      </form>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t(stillForbidden.length ? 'access.common.close' : 'access.common.cancel')" @click="open = false" />
        <UButton v-if="!stillForbidden.length" icon="i-lucide-check" :label="t('allowlist.add.submit')" :loading="busy" :disabled="!canSubmit" data-testid="add-submit" @click="submit" />
      </div>
    </template>
  </UModal>
</template>
