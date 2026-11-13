<script setup lang="ts">
// Allowlist entries, one row each: domain or pattern (a Regex entry, marked as such and shown
// verbatim), scope, remaining time, uses in 24 h, note, who added it. Actions: extend, make
// permanent, change the note, remove (asks first).
import type { AllowlistEntry } from '~/utils/access'

const props = withDefaults(defineProps<{ entries: AllowlistEntry[], showScope?: boolean }>(), { showScope: true })
const emit = defineEmits<{ changed: [] }>()

const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()
const when = useWhen()
const now = useNow(15000)

type Dialog = 'extend' | 'note' | 'remove' | 'permanent'
const dialog = ref<Dialog | null>(null)
const target = ref<AllowlistEntry | null>(null)
const busy = ref(false)
const error = ref<unknown>(null)
const refused = ref<'extend' | 'make-permanent' | null>(null)
const noteText = ref('')

const dialogOpen = (d: Dialog) => computed({
  get: () => dialog.value === d,
  set: (v: boolean) => { if (!v && !busy.value) dialog.value = null },
})
const extendOpen = dialogOpen('extend')
const noteOpen = dialogOpen('note')
const removeOpen = dialogOpen('remove')
const permanentOpen = dialogOpen('permanent')

function key(e: AllowlistEntry) {
  return `${e.scope}/${e.regex != null ? `regex:${e.regex}` : e.domain}`
}

function left(e: AllowlistEntry): { text: string, color: 'neutral' | 'warning' | 'error' | 'success' } {
  const s = entrySecondsLeft(e, now.value)
  if (s === null) return { text: t('allowlist.entry.permanent'), color: 'neutral' }
  if (s === 0 || e.expired) return { text: t('allowlist.entry.expired'), color: 'error' }
  return { text: t('allowlist.entry.left', { span: when.span(s) }), color: s < 900 ? 'warning' : 'success' }
}

function uses(e: AllowlistEntry) {
  return e.uses_24h === null ? t('allowlist.entry.usesUnknown') : t('allowlist.entry.uses', e.uses_24h)
}

function openDialog(d: Dialog, e: AllowlistEntry) {
  target.value = e
  error.value = null
  refused.value = null
  noteText.value = e.note
  dialog.value = d
}

async function run(what: 'extend' | 'make-permanent' | 'note' | 'remove', request: () => Promise<unknown>, done: string) {
  busy.value = true
  error.value = null
  refused.value = null
  try {
    await request()
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: done })
    busy.value = false
    dialog.value = null
    emit('changed')
  }
  catch (e) {
    if (e instanceof ApiError && e.code === 'domain_suspicious' && (what === 'extend' || what === 'make-permanent')) refused.value = what
    else error.value = e
  }
  finally {
    busy.value = false
  }
}

function extend(duration: string) {
  const e = target.value!
  void run('extend', () => api(allowlistEntryPath(e, 'extend'), { method: 'POST', body: { duration } }),
    t('allowlist.entry.extended', { domain: entryValue(e), duration: t(`allowlist.durations.${duration}`) }))
}
function makePermanent() {
  const e = target.value!
  void run('make-permanent', () => api(allowlistEntryPath(e, 'make-permanent'), { method: 'POST' }),
    t('allowlist.entry.madePermanent', { domain: entryValue(e) }))
}
function saveNote() {
  const e = target.value!
  void run('note', () => api(allowlistEntryPath(e, 'note'), { method: 'PUT', body: { note: noteText.value.trim() } }),
    t('allowlist.entry.noteSaved', { domain: entryValue(e) }))
}
function remove() {
  const e = target.value!
  void run('remove', () => api(allowlistEntryPath(e), { method: 'DELETE' }), t('allowlist.entry.removed', { domain: entryValue(e) }))
}

const extendDurations = EXTEND_DURATIONS
const scopeLabel = (s: string) => s === 'all' ? t('allowlist.scope.all') : t('allowlist.scope.vm', { vm: s })
const targetScope = computed(() => target.value ? scopeLabel(target.value.scope) : '')
const targetValue = computed(() => (target.value ? entryValue(target.value) : ''))
</script>

<template>
  <ul class="divide-y divide-default rounded-lg border border-default bg-default" data-testid="allowlist-entries">
    <li
      v-for="e in props.entries"
      :key="key(e)"
      class="flex flex-col gap-1.5 px-4 py-3 sm:flex-row sm:items-start sm:gap-4"
      data-testid="allowlist-entry"
      :data-domain="e.domain ?? undefined"
      :data-regex="e.regex ?? undefined"
      :data-scope="e.scope"
    >
      <div class="min-w-0 flex-1 space-y-1">
        <div class="flex flex-wrap items-center gap-2">
          <UTooltip v-if="e.regex != null" :text="t('regex.badgeHint')">
            <UBadge color="warning" variant="outline" size="sm" icon="i-lucide-regex" :label="t('regex.badge')" data-testid="entry-regex" />
          </UTooltip>
          <UTooltip v-if="e.refused" :text="e.refused">
            <UBadge color="error" variant="subtle" size="sm" icon="i-lucide-ban" :label="t('regex.refused')" data-testid="entry-refused" />
          </UTooltip>
          <span class="break-all font-mono text-sm font-medium text-highlighted">{{ entryValue(e) }}</span>
          <UBadge v-if="props.showScope" :color="e.scope === 'all' ? 'info' : 'neutral'" variant="subtle" size="sm" :label="scopeLabel(e.scope)" data-testid="entry-scope" />
          <UBadge :color="left(e).color" variant="soft" size="sm" :label="left(e).text" data-testid="entry-left" />
        </div>
        <p v-if="e.note" class="text-sm text-default" data-testid="entry-note">
          {{ e.note }}
        </p>
        <p class="text-xs text-muted">
          <span data-testid="entry-uses">{{ uses(e) }}</span>
          <span aria-hidden="true"> · </span>
          <span>{{ t('allowlist.entry.added', { who: e.added_by || '?', when: when.at(e.added) }) }}</span>
          <template v-if="e.expires">
            <span aria-hidden="true"> · </span>
            <span>{{ t('allowlist.entry.until', { when: when.at(e.expires) }) }}</span>
          </template>
        </p>
      </div>
      <div class="flex shrink-0 flex-wrap gap-1 max-sm:-ms-2">
        <template v-if="e.expires !== null">
          <UTooltip :text="t('allowlist.entry.extend')">
            <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-clock-plus" :aria-label="t('allowlist.entry.extend')" data-testid="entry-extend" @click="openDialog('extend', e)" />
          </UTooltip>
          <UTooltip :text="t('allowlist.entry.makePermanent')">
            <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-infinity" :aria-label="t('allowlist.entry.makePermanent')" data-testid="entry-permanent" @click="openDialog('permanent', e)" />
          </UTooltip>
        </template>
        <UTooltip :text="t('allowlist.entry.editNote')">
          <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-pencil" :aria-label="t('allowlist.entry.editNote')" data-testid="entry-note-edit" @click="openDialog('note', e)" />
        </UTooltip>
        <UTooltip :text="t('allowlist.entry.remove')">
          <UButton color="error" variant="ghost" size="sm" icon="i-lucide-trash-2" :aria-label="t('allowlist.entry.remove')" data-testid="entry-remove" @click="openDialog('remove', e)" />
        </UTooltip>
      </div>
    </li>
  </ul>

  <UModal v-model:open="extendOpen" :title="t('allowlist.entry.extendTitle', { domain: targetValue })" :description="targetScope">
    <template #body>
      <div class="space-y-3">
        <p class="text-sm text-muted">
          {{ t('allowlist.entry.extendText') }}
        </p>
        <div class="flex flex-wrap gap-2" data-testid="extend-choices">
          <UButton
            v-for="d in extendDurations"
            :key="d"
            color="neutral"
            variant="outline"
            :label="t('allowlist.entry.extendBy', { duration: t(`allowlist.durations.${d}`) })"
            :disabled="busy"
            :data-duration="d"
            @click="extend(d)"
          />
        </div>
        <AllowlistRefusal v-if="refused && target?.domain" :domain="target.domain" />
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
      </div>
    </template>
  </UModal>

  <AccessConfirmDialog
    v-model:open="permanentOpen"
    :title="t('allowlist.entry.permanentTitle', { domain: targetValue })"
    :description="t('allowlist.entry.permanentText', { scope: targetScope })"
    :confirm-label="t('allowlist.entry.makePermanent')"
    color="primary"
    :busy="busy"
    :error="error ? errorText(error) : null"
    @confirm="makePermanent"
  >
    <AllowlistRefusal v-if="refused && target?.domain" :domain="target.domain" />
  </AccessConfirmDialog>

  <UModal v-model:open="noteOpen" :title="t('allowlist.entry.noteTitle', { domain: targetValue })" :description="targetScope">
    <template #body>
      <form class="space-y-3" @submit.prevent="saveNote">
        <UInput v-model="noteText" :placeholder="t('allowlist.add.notePlaceholder')" class="w-full" autofocus data-testid="note-input" />
        <p v-if="noteText.length > 200" class="text-sm text-error">
          {{ t('allowlist.add.noteTooLong') }}
        </p>
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
      </form>
    </template>
    <template #footer>
      <div class="flex w-full justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="busy" @click="noteOpen = false" />
        <UButton icon="i-lucide-save" :label="t('access.common.save')" :loading="busy" :disabled="noteText.length > 200" data-testid="note-save" @click="saveNote" />
      </div>
    </template>
  </UModal>

  <AccessConfirmDialog
    v-model:open="removeOpen"
    :title="t('allowlist.entry.removeTitle', { domain: targetValue })"
    :description="t('allowlist.entry.removeText', { scope: targetScope })"
    :confirm-label="t('allowlist.entry.remove')"
    :busy="busy"
    :error="error ? errorText(error) : null"
    @confirm="remove"
  />
</template>
