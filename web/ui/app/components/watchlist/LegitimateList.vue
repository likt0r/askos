<script setup lang="ts">
// The Legitimate domains: the Host's (add, change the note, remove after asking) and the
// built-in ones (read-only). A domain that would lift strong Signals can only be added on
// the Host with --force (ADR 0009); the refusal shows the command.
import type { LegitimateDomain } from '~/utils/access'

const props = defineProps<{ domains: LegitimateDomain[], loaded: boolean, error?: unknown }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()
const when = useWhen()

const filter = ref('')
const showAll = ref(false)
// the Host's first, then the built-in ones
const rows = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const all = [...props.domains].sort((a, b) => Number(a.builtin) - Number(b.builtin) || a.domain.localeCompare(b.domain))
  return q ? all.filter(d => d.domain.includes(q) || d.note.toLowerCase().includes(q)) : all
})
const visible = computed(() => (showAll.value || filter.value ? rows.value : rows.value.slice(0, 12)))

// adding
const domainInput = ref('')
const note = ref('')
const adding = ref(false)
const addError = ref<unknown>(null)
const refused = ref<string | null>(null)
const domain = computed(() => normalizeDomain(domainInput.value))
const canAdd = computed(() => looksLikeDomain(domain.value) && note.value.length <= 200)

async function add() {
  if (!canAdd.value) return
  adding.value = true
  addError.value = null
  refused.value = null
  const d = domain.value
  try {
    const answer = await api<{ added?: number, builtin?: boolean, message?: string }>('/watchlist/legitimate', {
      method: 'POST',
      body: { domain: d, ...(note.value.trim() ? { note: note.value.trim() } : {}) },
    })
    const fresh = !answer?.builtin && !props.domains.some(x => x.domain === d)
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t(fresh ? 'watchlist.legitimate.added' : 'watchlist.legitimate.already', { domain: d }) })
    domainInput.value = ''
    note.value = ''
    emit('changed')
  }
  catch (e) {
    if (e instanceof ApiError && e.code === 'force_required') refused.value = d
    addError.value = e
  }
  finally {
    adding.value = false
  }
}

// note and removal
type Dialog = 'note' | 'remove'
const dialog = ref<Dialog | null>(null)
const target = ref<LegitimateDomain | null>(null)
const busy = ref(false)
const error = ref<unknown>(null)
const noteText = ref('')
const dialogOpen = (d: Dialog) => computed({
  get: () => dialog.value === d,
  set: (v: boolean) => { if (!v && !busy.value) dialog.value = null },
})
const noteOpen = dialogOpen('note')
const removeOpen = dialogOpen('remove')

function openDialog(d: Dialog, e: LegitimateDomain) {
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

const path = (d: string) => `/watchlist/legitimate/${encodeURIComponent(d)}`

function saveNote() {
  const e = target.value!
  void run(() => api(`${path(e.domain)}/note`, { method: 'PUT', body: { note: noteText.value.trim() } }),
    t('watchlist.legitimate.noteSaved', { domain: e.domain }))
}

function remove() {
  const e = target.value!
  void run(() => api(path(e.domain), { method: 'DELETE' }), t('watchlist.legitimate.removed', { domain: e.domain }))
}
</script>

<template>
  <section class="space-y-3" data-testid="legitimate">
    <div class="flex flex-wrap items-center gap-2">
      <h3 class="font-medium text-highlighted">
        {{ t('watchlist.legitimate.title') }}
      </h3>
      <span v-if="props.loaded" class="text-sm text-muted">{{ t('watchlist.legitimate.count', props.domains.length) }}</span>
      <span class="flex-1" />
      <UInput v-model="filter" size="sm" icon="i-lucide-search" :placeholder="t('watchlist.legitimate.filter')" :aria-label="t('watchlist.legitimate.filter')" class="w-full sm:w-56" />
    </div>
    <p class="text-sm text-muted">
      {{ t('watchlist.legitimate.intro') }}
    </p>

    <form class="grid gap-2 rounded-lg border border-default p-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto]" novalidate data-testid="legitimate-add" @submit.prevent="add">
      <UInput v-model="domainInput" placeholder="registry.example" autocomplete="off" spellcheck="false" class="font-mono" :aria-label="t('watchlist.legitimate.domain')" data-testid="legitimate-add-domain" />
      <UInput v-model="note" :placeholder="`${t('watchlist.legitimate.note')} (${t('watchlist.legitimate.optional')})`" :aria-label="t('watchlist.legitimate.note')" data-testid="legitimate-add-note" />
      <UButton type="submit" icon="i-lucide-plus" :label="t('watchlist.legitimate.add')" :loading="adding" :disabled="!canAdd" data-testid="legitimate-add-submit" />
    </form>
    <p v-if="note.length > 200" class="text-sm text-error">
      {{ t('watchlist.legitimate.tooLong') }}
    </p>
    <UAlert v-if="addError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(addError)" role="alert" data-testid="legitimate-add-error">
      <template v-if="refused" #description>
        {{ t('watchlist.legitimate.onlyCli') }}
        <code class="mt-1 block break-all font-mono text-xs">sudo agentbox legitimate-add {{ refused }} --force</code>
      </template>
    </UAlert>
    <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(props.error)" />

    <ul v-if="visible.length" class="divide-y divide-default rounded-lg border border-default bg-default" data-testid="legitimate-list">
      <li v-for="d in visible" :key="d.domain" class="flex flex-col gap-1 px-4 py-2 sm:flex-row sm:items-start sm:gap-3" data-testid="legitimate-row" :data-domain="d.domain" :data-builtin="d.builtin">
        <span class="break-all font-mono text-sm text-highlighted sm:w-56 sm:shrink-0">{{ d.domain }}</span>
        <div class="min-w-0 flex-1 space-y-0.5 text-xs">
          <UTooltip v-if="d.builtin" :text="t('watchlist.legitimate.builtinHint')">
            <UBadge color="neutral" variant="outline" size="sm" :label="t('watchlist.legitimate.builtin')" />
          </UTooltip>
          <p v-if="d.note" class="text-sm text-default" data-testid="legitimate-note">
            {{ d.note }}
          </p>
          <p v-if="!d.builtin && d.added_by && d.added_by !== 'migration' && d.added" class="text-dimmed">
            {{ t('watchlist.legitimate.addedBy', { who: d.added_by, when: when.at(d.added) }) }}
          </p>
        </div>
        <span v-if="!d.builtin" class="flex shrink-0 gap-1">
          <UTooltip :text="t('watchlist.legitimate.editNote')">
            <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-pencil" :aria-label="t('watchlist.legitimate.editNote')" data-testid="legitimate-note-edit" @click="openDialog('note', d)" />
          </UTooltip>
          <UButton
            color="error"
            variant="ghost"
            size="sm"
            icon="i-lucide-trash-2"
            :aria-label="t('watchlist.legitimate.removeOne', { domain: d.domain })"
            data-testid="legitimate-remove"
            @click="openDialog('remove', d)"
          />
        </span>
      </li>
    </ul>
    <p v-else-if="props.loaded" class="text-sm text-muted">
      {{ t('watchlist.legitimate.none') }}
    </p>
    <UButton
      v-if="!filter && rows.length > 12"
      color="neutral"
      variant="link"
      size="sm"
      :label="showAll ? t('watchlist.legitimate.fewer') : t('watchlist.legitimate.all', rows.length)"
      @click="showAll = !showAll"
    />

    <UModal v-model:open="noteOpen" :title="t('watchlist.legitimate.noteTitle', { domain: target?.domain })">
      <template #body>
        <form class="space-y-3" @submit.prevent="saveNote">
          <UInput v-model="noteText" class="w-full" autofocus data-testid="legitimate-note-input" />
          <p v-if="noteText.length > 200" class="text-sm text-error">
            {{ t('watchlist.legitimate.tooLong') }}
          </p>
          <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
        </form>
      </template>
      <template #footer>
        <div class="flex w-full justify-end gap-2">
          <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="busy" @click="noteOpen = false" />
          <UButton icon="i-lucide-save" :label="t('access.common.save')" :loading="busy" :disabled="noteText.length > 200" data-testid="legitimate-note-save" @click="saveNote" />
        </div>
      </template>
    </UModal>

    <AccessConfirmDialog
      v-model:open="removeOpen"
      :title="t('watchlist.legitimate.removeTitle', { domain: target?.domain })"
      :description="t('watchlist.legitimate.removeText')"
      :confirm-label="t('watchlist.legitimate.remove')"
      :busy="busy"
      :error="error ? errorText(error) : null"
      @confirm="remove"
    />
  </section>
</template>
