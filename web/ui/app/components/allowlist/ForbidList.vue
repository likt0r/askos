<script setup lang="ts">
// A VM's Forbids, one row each: domain, remaining time, connections refused in 24 h, note, who
// added it. Actions as for an Allowlist entry: extend, make permanent, change the note, remove
// (asks first; the destination is then held and notified again).
import type { ForbidEntry } from '~/utils/access'

const props = defineProps<{ entries: ForbidEntry[] }>()
const emit = defineEmits<{ changed: [] }>()

const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()
const when = useWhen()
const now = useNow(15000)

type Dialog = 'extend' | 'note' | 'remove' | 'permanent'
const dialog = ref<Dialog | null>(null)
const target = ref<ForbidEntry | null>(null)
const busy = ref(false)
const error = ref<unknown>(null)
const noteText = ref('')

const dialogOpen = (d: Dialog) => computed({
  get: () => dialog.value === d,
  set: (v: boolean) => { if (!v && !busy.value) dialog.value = null },
})
const extendOpen = dialogOpen('extend')
const noteOpen = dialogOpen('note')
const removeOpen = dialogOpen('remove')
const permanentOpen = dialogOpen('permanent')

function left(e: ForbidEntry): { text: string, color: 'neutral' | 'warning' | 'error' | 'success' } {
  const s = entrySecondsLeft(e, now.value)
  if (s === null) return { text: t('allowlist.entry.permanent'), color: 'neutral' }
  if (s === 0 || e.expired) return { text: t('allowlist.entry.expired'), color: 'error' }
  return { text: t('allowlist.entry.left', { span: when.span(s) }), color: s < 900 ? 'warning' : 'success' }
}

function refused(e: ForbidEntry) {
  return e.refused_24h === null ? t('forbid.entry.refusedUnknown') : t('forbid.entry.refused', e.refused_24h)
}

function openDialog(d: Dialog, e: ForbidEntry) {
  target.value = e
  error.value = null
  noteText.value = e.note
  dialog.value = d
}

async function run(request: () => Promise<unknown>, done: string) {
  busy.value = true
  error.value = null
  try {
    await request()
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: done })
    busy.value = false
    dialog.value = null
    emit('changed')
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}

function extend(duration: string) {
  const e = target.value!
  void run(() => api(forbidPath(e, 'extend'), { method: 'POST', body: { duration } }),
    t('forbid.entry.extended', { domain: e.domain, duration: t(`allowlist.durations.${duration}`) }))
}
function makePermanent() {
  const e = target.value!
  void run(() => api(forbidPath(e, 'make-permanent'), { method: 'POST' }), t('forbid.entry.madePermanent', { domain: e.domain }))
}
function saveNote() {
  const e = target.value!
  void run(() => api(forbidPath(e, 'note'), { method: 'PUT', body: { note: noteText.value.trim() } }),
    t('allowlist.entry.noteSaved', { domain: e.domain }))
}
function remove() {
  const e = target.value!
  void run(() => api(forbidPath(e), { method: 'DELETE' }), t('forbid.entry.removed', { domain: e.domain }))
}

const extendDurations = EXTEND_DURATIONS
const targetDomain = computed(() => target.value?.domain ?? '')
const targetVm = computed(() => target.value ? t('allowlist.scope.vm', { vm: target.value.scope }) : '')
</script>

<template>
  <ul class="divide-y divide-default rounded-lg border border-default bg-default" data-testid="forbid-entries">
    <li
      v-for="e in props.entries"
      :key="`${e.scope}/${e.domain}`"
      class="flex flex-col gap-1.5 px-4 py-3 sm:flex-row sm:items-start sm:gap-4"
      data-testid="forbid-entry"
      :data-domain="e.domain"
    >
      <div class="min-w-0 flex-1 space-y-1">
        <div class="flex flex-wrap items-center gap-2">
          <span class="break-all font-mono text-sm font-medium text-highlighted">{{ e.domain }}</span>
          <UBadge :color="left(e).color" variant="soft" size="sm" :label="left(e).text" data-testid="forbid-left" />
        </div>
        <p v-if="e.note" class="text-sm text-default" data-testid="forbid-entry-note">
          {{ e.note }}
        </p>
        <p class="text-xs text-muted">
          <span data-testid="forbid-refused">{{ refused(e) }}</span>
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
            <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-clock-plus" :aria-label="t('allowlist.entry.extend')" data-testid="forbid-extend" @click="openDialog('extend', e)" />
          </UTooltip>
          <UTooltip :text="t('allowlist.entry.makePermanent')">
            <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-infinity" :aria-label="t('allowlist.entry.makePermanent')" data-testid="forbid-permanent" @click="openDialog('permanent', e)" />
          </UTooltip>
        </template>
        <UTooltip :text="t('allowlist.entry.editNote')">
          <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-pencil" :aria-label="t('allowlist.entry.editNote')" data-testid="forbid-note-edit" @click="openDialog('note', e)" />
        </UTooltip>
        <UTooltip :text="t('forbid.entry.remove')">
          <UButton color="error" variant="ghost" size="sm" icon="i-lucide-trash-2" :aria-label="t('forbid.entry.remove')" data-testid="forbid-remove" @click="openDialog('remove', e)" />
        </UTooltip>
      </div>
    </li>
  </ul>

  <UModal v-model:open="extendOpen" :title="t('allowlist.entry.extendTitle', { domain: targetDomain })" :description="targetVm">
    <template #body>
      <div class="space-y-3">
        <p class="text-sm text-muted">
          {{ t('forbid.entry.extendText') }}
        </p>
        <div class="flex flex-wrap gap-2" data-testid="forbid-extend-choices">
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
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
      </div>
    </template>
  </UModal>

  <AccessConfirmDialog
    v-model:open="permanentOpen"
    :title="t('allowlist.entry.permanentTitle', { domain: targetDomain })"
    :description="t('forbid.entry.permanentText', { vm: target?.scope ?? '' })"
    :confirm-label="t('allowlist.entry.makePermanent')"
    color="primary"
    :busy="busy"
    :error="error ? errorText(error) : null"
    @confirm="makePermanent"
  />

  <UModal v-model:open="noteOpen" :title="t('allowlist.entry.noteTitle', { domain: targetDomain })" :description="targetVm">
    <template #body>
      <form class="space-y-3" @submit.prevent="saveNote">
        <UInput v-model="noteText" :placeholder="t('forbid.add.notePlaceholder')" class="w-full" autofocus data-testid="forbid-note-input" />
        <p v-if="noteText.length > 200" class="text-sm text-error">
          {{ t('allowlist.add.noteTooLong') }}
        </p>
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
      </form>
    </template>
    <template #footer>
      <div class="flex w-full justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="busy" @click="noteOpen = false" />
        <UButton icon="i-lucide-save" :label="t('access.common.save')" :loading="busy" :disabled="noteText.length > 200" data-testid="forbid-note-save" @click="saveNote" />
      </div>
    </template>
  </UModal>

  <AccessConfirmDialog
    v-model:open="removeOpen"
    :title="t('forbid.entry.removeTitle', { domain: targetDomain })"
    :description="t('forbid.entry.removeText', { vm: target?.scope ?? '' })"
    :confirm-label="t('forbid.entry.remove')"
    :busy="busy"
    :error="error ? errorText(error) : null"
    @confirm="remove"
  />
</template>
