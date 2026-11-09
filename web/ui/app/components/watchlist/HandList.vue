<script setup lang="ts">
// The hand-maintained Watchlist on the Host: add an entry (domain or pattern, category, source,
// note), change its note, remove it (asks first). A pattern (a Regex entry, ADR 0013) is marked
// and shown verbatim, and can be tested against a host first; a broad one is fine here. Entries
// of a strong category can only be removed or weakened on the Host with --force (ADR 0009);
// their row shows the command instead.
// While the list cannot be read (`unreadable`, the reason from the Host) the proxy keeps Open
// web closed: the alert says so. The Feeds still show (Feeds.vue).
import type { RegexCheck, Target, Watchlist, WatchlistEntry } from '~/utils/access'
import { WATCHLIST_CATEGORIES } from '~/utils/access'

const props = defineProps<{ watchlist: Watchlist | null, error?: unknown }>()
const emit = defineEmits<{ changed: [] }>()
const { t, te } = useI18n()
const errorText = useErrorText()
const toast = useToast()
const when = useWhen()

const filter = ref('')
const showAll = ref(false)
const entries = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const all = props.watchlist?.entries ?? []
  return q
    ? all.filter(e => entryValue(e).toLowerCase().includes(q) || e.category.includes(q) || e.source.toLowerCase().includes(q) || e.note.toLowerCase().includes(q))
    : all
})
const visible = computed(() => (showAll.value || filter.value ? entries.value : entries.value.slice(0, 12)))

function category(c: string) {
  return te(`signals.names.${c}`) ? t(`signals.names.${c}`) : c
}
const categories = computed(() => WATCHLIST_CATEGORIES.map(c => ({ label: category(c), value: c })))

// adding
const kind = ref<'domain' | 'regex'>('domain')
const domainInput = ref('')
const patternInput = ref('')
const checked = ref<RegexCheck | null>(null)
const newCategory = ref<string>('exfil')
const source = ref('')
const note = ref('')
const adding = ref(false)
const addError = ref<unknown>(null)
const domain = computed(() => normalizeDomain(domainInput.value))
const pattern = computed(() => patternInput.value.trim())
const canAdd = computed(() => (kind.value === 'regex' ? looksLikePattern(pattern.value) : looksLikeDomain(domain.value))
  && source.value.length <= 200 && note.value.length <= 200)
// an existing strong entry lowered to payload, or a missing Watchlist created again: only the CLI may do that
const lowering = ref<Target | null>(null)
// which of the two: the Watchlist would be created again (its own code, never the wording)
const recreating = ref(false)

async function add() {
  if (!canAdd.value) return
  adding.value = true
  addError.value = null
  lowering.value = null
  recreating.value = false
  const entry: Target = kind.value === 'regex' ? { domain: null, regex: pattern.value } : { domain: domain.value, regex: null }
  try {
    await api('/watchlist/entries', {
      method: 'POST',
      body: { ...(entry.regex != null ? { regex: entry.regex } : { domain: entry.domain }), category: newCategory.value, ...(source.value.trim() ? { source: source.value.trim() } : {}), ...(note.value.trim() ? { note: note.value.trim() } : {}) },
    })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('watchlist.hand.added', { domain: entryValue(entry), category: category(newCategory.value) }) })
    domainInput.value = ''
    patternInput.value = ''
    source.value = ''
    note.value = ''
    emit('changed')
  }
  catch (e) {
    if (e instanceof ApiError && (e.code === 'force_required' || e.code === 'watchlist_recreate_force_required')) {
      lowering.value = entry
      recreating.value = e.code === 'watchlist_recreate_force_required'
    }
    addError.value = e
  }
  finally {
    adding.value = false
  }
}

// note and removal
type Dialog = 'note' | 'remove'
const dialog = ref<Dialog | null>(null)
const target = ref<WatchlistEntry | null>(null)
const busy = ref(false)
const error = ref<unknown>(null)
const noteText = ref('')
const dialogOpen = (d: Dialog) => computed({
  get: () => dialog.value === d,
  set: (v: boolean) => { if (!v && !busy.value) dialog.value = null },
})
const noteOpen = dialogOpen('note')
const removeOpen = dialogOpen('remove')

function openDialog(d: Dialog, e: WatchlistEntry) {
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

const path = (e: Target, action = '') => entryPath('/watchlist/entries', e, action)
/** what the commands shown for the Host take for an entry */
const cli = (e: Target | null) => (e ? cliTarget(e) : '')
const targetValue = computed(() => (target.value ? entryValue(target.value) : ''))

function saveNote() {
  const e = target.value!
  void run(() => api(path(e, 'note'), { method: 'PUT', body: { note: noteText.value.trim() } }),
    t('watchlist.hand.noteSaved', { domain: entryValue(e) }))
}

function remove() {
  const e = target.value!
  void run(() => api(path(e), { method: 'DELETE' }), t('watchlist.hand.removed', { domain: entryValue(e) }))
}
</script>

<template>
  <section class="space-y-3" data-testid="hand-list">
    <div class="flex flex-wrap items-center gap-2">
      <h3 class="font-medium text-highlighted">
        {{ t('watchlist.hand.title') }}
      </h3>
      <span v-if="props.watchlist && !props.watchlist.unreadable" class="text-sm text-muted">{{ t('watchlist.hand.count', props.watchlist.entries.length) }}</span>
      <span class="flex-1" />
      <UInput v-model="filter" size="sm" icon="i-lucide-search" :placeholder="t('watchlist.hand.filter')" :aria-label="t('watchlist.hand.filter')" class="w-full sm:w-56" />
    </div>
    <p class="text-sm text-muted">
      {{ t('watchlist.hand.intro') }}
    </p>
    <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(props.error)" role="alert" data-testid="hand-list-error" />
    <UAlert v-if="props.watchlist?.unreadable" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="t('errors.watchlist_unreadable')" :description="props.watchlist.unreadable" role="alert" data-testid="hand-list-unreadable" />

    <form class="grid gap-2 rounded-lg border border-default p-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1.2fr)_minmax(0,2fr)_minmax(0,2fr)_auto]" novalidate data-testid="watch-add" @submit.prevent="add">
      <AccessEntryKind v-model="kind" size="xs" class="sm:col-span-5" />
      <UInput v-if="kind === 'domain'" v-model="domainInput" placeholder="exfil.example" autocomplete="off" spellcheck="false" class="font-mono" :aria-label="t('watchlist.hand.domain')" data-testid="watch-add-domain" />
      <UInput v-else v-model="patternInput" placeholder="^npm-[a-z]+\.(com|net)$" autocomplete="off" spellcheck="false" class="font-mono" :aria-label="t('regex.pattern')" data-testid="watch-add-regex" />
      <USelect v-model="newCategory" :items="categories" :aria-label="t('watchlist.hand.category')" data-testid="watch-add-category" />
      <UInput v-model="source" :placeholder="t('watchlist.hand.sourcePlaceholder')" :aria-label="t('watchlist.hand.source')" data-testid="watch-add-source" />
      <UInput v-model="note" :placeholder="`${t('watchlist.hand.note')} (${t('watchlist.hand.optional')})`" :aria-label="t('watchlist.hand.note')" data-testid="watch-add-note" />
      <UButton type="submit" icon="i-lucide-plus" :label="t('watchlist.hand.add')" :loading="adding" :disabled="!canAdd" data-testid="watch-add-submit" />
      <AccessRegexCheck v-if="kind === 'regex'" v-model:checked="checked" list="watchlist" :pattern="patternInput" class="sm:col-span-5" />
    </form>
    <p v-if="source.length > 200 || note.length > 200" class="text-sm text-error">
      {{ t('watchlist.hand.tooLong') }}
    </p>
    <UAlert v-if="addError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="recreating ? t('watchlist.hand.recreateTitle') : errorText(addError)" role="alert" data-testid="watch-add-error">
      <template v-if="lowering" #description>
        {{ recreating ? t('watchlist.hand.onlyCliRecreate') : t('watchlist.hand.onlyCli') }}
        <code class="mt-1 block break-all font-mono text-xs">sudo agentbox watch-add {{ cli(lowering) }} --category {{ newCategory }} --force</code>
      </template>
    </UAlert>

    <ul v-if="visible.length" class="divide-y divide-default rounded-lg border border-default bg-default" data-testid="hand-watchlist">
      <li v-for="e in visible" :key="e.regex != null ? `regex:${e.regex}` : e.domain!" class="flex flex-col gap-1 px-4 py-2 sm:flex-row sm:items-start sm:gap-3" data-testid="watchlist-entry" :data-domain="e.domain ?? undefined" :data-regex="e.regex ?? undefined">
        <span class="flex flex-wrap items-center gap-1 sm:w-56 sm:shrink-0">
          <UTooltip v-if="e.regex != null" :text="t('regex.badgeHint')">
            <UBadge color="warning" variant="outline" size="sm" icon="i-lucide-regex" :label="t('regex.badge')" data-testid="entry-regex" />
          </UTooltip>
          <UTooltip v-if="e.refused" :text="e.refused">
            <UBadge color="error" variant="subtle" size="sm" icon="i-lucide-ban" :label="t('regex.refused')" data-testid="entry-refused" />
          </UTooltip>
          <span class="break-all font-mono text-sm text-highlighted">{{ entryValue(e) }}</span>
        </span>
        <span class="flex flex-wrap gap-1 sm:w-48 sm:shrink-0">
          <UBadge :color="e.strong ? 'error' : 'warning'" variant="subtle" size="sm" :label="category(e.category)" />
          <UBadge v-if="!e.strong" color="neutral" variant="outline" size="sm" :label="t('watchlist.hand.weak')" />
        </span>
        <div class="min-w-0 flex-1 space-y-0.5 text-xs">
          <p class="truncate text-muted" :title="e.source">
            {{ e.source || '—' }}
          </p>
          <p v-if="e.note" class="text-sm text-default" data-testid="watch-note">
            {{ e.note }}
          </p>
          <p v-if="e.added_by !== 'migration'" class="text-dimmed">
            {{ t('watchlist.hand.addedBy', { who: e.added_by, when: when.at(e.added) }) }}
          </p>
        </div>
        <span class="flex shrink-0 gap-1">
          <UTooltip :text="t('watchlist.hand.editNote')">
            <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-pencil" :aria-label="t('watchlist.hand.editNote')" data-testid="watch-note-edit" @click="openDialog('note', e)" />
          </UTooltip>
          <UTooltip v-if="e.strong" :text="t('watchlist.hand.lockedOne')">
            <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-lock" :aria-label="t('watchlist.hand.lockedOne')" data-testid="watch-remove-locked" @click="openDialog('remove', e)" />
          </UTooltip>
          <UButton
            v-else
            color="error"
            variant="ghost"
            size="sm"
            icon="i-lucide-trash-2"
            :aria-label="t('watchlist.hand.removeOne', { domain: entryValue(e) })"
            data-testid="watch-remove"
            @click="openDialog('remove', e)"
          />
        </span>
      </li>
    </ul>
    <p v-else-if="props.watchlist" class="text-sm text-muted">
      {{ t('watchlist.hand.none') }}
    </p>
    <UButton
      v-if="!filter && entries.length > 12"
      color="neutral"
      variant="link"
      size="sm"
      :label="showAll ? t('watchlist.hand.fewer') : t('watchlist.hand.all', entries.length)"
      @click="showAll = !showAll"
    />

    <UModal v-model:open="noteOpen" :title="t('watchlist.hand.noteTitle', { domain: targetValue })">
      <template #body>
        <form class="space-y-3" @submit.prevent="saveNote">
          <UInput v-model="noteText" class="w-full" autofocus data-testid="watch-note-input" />
          <p v-if="noteText.length > 200" class="text-sm text-error">
            {{ t('watchlist.hand.tooLong') }}
          </p>
          <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
        </form>
      </template>
      <template #footer>
        <div class="flex w-full justify-end gap-2">
          <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="busy" @click="noteOpen = false" />
          <UButton icon="i-lucide-save" :label="t('access.common.save')" :loading="busy" :disabled="noteText.length > 200" data-testid="watch-note-save" @click="saveNote" />
        </div>
      </template>
    </UModal>

    <!-- a strong entry: only the command for the Host, nothing to confirm -->
    <UModal v-if="target?.strong" v-model:open="removeOpen" :title="t('watchlist.hand.removeTitle', { domain: targetValue })">
      <template #body>
        <div class="space-y-2 text-sm" data-testid="watch-remove-cli">
          <p>{{ t('watchlist.hand.onlyCli') }}</p>
          <code class="block break-all rounded bg-elevated p-2 font-mono text-xs">sudo agentbox watch-remove {{ cli(target) }} --force</code>
        </div>
      </template>
    </UModal>
    <AccessConfirmDialog
      v-else
      v-model:open="removeOpen"
      :title="t('watchlist.hand.removeTitle', { domain: targetValue })"
      :description="t('watchlist.hand.removeText')"
      :confirm-label="t('watchlist.hand.remove')"
      :busy="busy"
      :error="error ? errorText(error) : null"
      @confirm="remove"
    />
  </section>
</template>
