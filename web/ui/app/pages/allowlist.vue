<script setup lang="ts">
// The Allowlist: every entry (or those that apply to one VM, or those for all VMs), with
// scope, remaining time, uses in the last 24 hours, note and who added it; add, extend,
// make permanent, change the note, remove. Changes from elsewhere (CLI, the expiry timer)
// arrive as `activity` events of kind `allowlist`.
import type { AllowlistEntry } from '~/utils/access'

definePageMeta({ titleKey: 'nav.allowlist' })

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const live = useLive()
const errorText = useErrorText()

const EVERY = '*'
const filter = computed<string>({
  get: () => (typeof route.query.vm === 'string' && route.query.vm ? route.query.vm : EVERY),
  set: (v) => { void router.replace({ query: v === EVERY ? {} : { vm: v } }) },
})
const filters = computed(() => [
  { value: EVERY, label: t('allowlist.filter.every') },
  { value: 'all', label: t('allowlist.filter.allVms') },
  ...live.state.vms.map(vm => ({ value: vm.name, label: t('allowlist.filter.vm', { vm: vm.name }) })),
])
const vmNames = computed(() => live.state.vms.map(vm => vm.name))

const entries = ref<AllowlistEntry[]>([])
const loaded = ref(false)
const loading = ref(false)
const error = ref<unknown>(null)
const adding = ref(false)
let request = 0

async function load() {
  const mine = ++request
  loading.value = true
  try {
    const query = filter.value === EVERY ? '' : `?vm=${encodeURIComponent(filter.value)}`
    const list = await api<AllowlistEntry[]>(`/allowlist${query}`)
    if (mine !== request) return
    entries.value = Array.isArray(list) ? list : []
    error.value = null
    loaded.value = true
  }
  catch (e) {
    if (mine === request) error.value = e
  }
  finally {
    if (mine === request) loading.value = false
  }
}

const counts = computed(() => ({
  temporary: entries.value.filter(e => e.expires !== null).length,
  total: entries.value.length,
}))

watch(filter, load)
onMounted(load)
useLiveEvent('activity', (entry) => {
  if (entry?.kind === 'allowlist') void load()
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-3">
      <USelect
        v-model="filter"
        :items="filters"
        icon="i-lucide-filter"
        class="w-full sm:w-64"
        :aria-label="t('allowlist.filter.label')"
        data-testid="allowlist-filter"
      />
      <span v-if="loaded" class="text-sm text-muted" data-testid="allowlist-count">
        {{ t('allowlist.count', counts.total) }}<template v-if="counts.temporary"> · {{ t('allowlist.temporaryCount', counts.temporary) }}</template>
      </span>
      <span class="flex-1" />
      <UButton color="neutral" variant="ghost" icon="i-lucide-refresh-cw" :loading="loading" :aria-label="t('access.common.refresh')" @click="load" />
      <UButton icon="i-lucide-plus" :label="t('allowlist.add.open')" data-testid="allowlist-add-open" @click="adding = true" />
    </div>

    <p class="text-sm text-muted">
      {{ t('allowlist.intro') }}
    </p>

    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" data-testid="allowlist-error" />
    <p v-if="!loaded && !error" class="text-sm text-muted">
      {{ t('access.common.loading') }}
    </p>
    <p v-else-if="loaded && !entries.length" class="rounded-lg border border-dashed border-default p-6 text-center text-sm text-muted" data-testid="allowlist-empty">
      {{ filter === '*' ? t('allowlist.empty') : t('allowlist.emptyFiltered') }}
    </p>
    <AllowlistEntryList v-else-if="entries.length" :entries="entries" @changed="load" />

    <AllowlistAddDialog
      v-model:open="adding"
      :vms="vmNames"
      :scope="filter === '*' ? 'all' : filter"
      @added="load"
    />
  </div>
</template>
