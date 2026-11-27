<script setup lang="ts">
// The Allowlist: every entry (or those that apply to one VM, or those for all VMs), with
// scope, remaining time, uses in the last 24 hours, note and who added it; add, extend,
// make permanent, change the note, remove. The resource `allowlist`: changes from elsewhere (CLI,
// the expiry timer) arrive as `activity` entries of kind `allowlist`, and deleting a VM drops its
// entries (the resources module's rows).
import type { AllowlistEntry } from '~/utils/access'

definePageMeta({ titleKey: 'nav.allowlist' })

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const live = useLive()

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

const list = useResource('allowlist', { params: () => ({ filter: filter.value }), keepPrevious: true })
const entries = computed<AllowlistEntry[]>(() => (Array.isArray(list.data.value) ? list.data.value : []))
const loaded = computed(() => list.data.value !== undefined)
const adding = ref(false)

const counts = computed(() => ({
  temporary: entries.value.filter(e => e.expires !== null).length,
  total: entries.value.length,
}))

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
      <UButton color="neutral" variant="ghost" icon="i-lucide-refresh-cw" :loading="list.fetching.value" :aria-label="t('access.common.refresh')" @click="list.retry()" />
      <UButton icon="i-lucide-plus" :label="t('allowlist.add.open')" data-testid="allowlist-add-open" @click="adding = true" />
    </div>

    <p class="text-sm text-muted">
      {{ t('allowlist.intro') }}
    </p>

    <ReadFailure :resource="list" data-testid="allowlist-error" />
    <p v-if="!loaded && !list.error.value" class="text-sm text-muted">
      {{ t('access.common.loading') }}
    </p>
    <p v-else-if="loaded && !entries.length" class="rounded-lg border border-dashed border-default p-6 text-center text-sm text-muted" data-testid="allowlist-empty">
      {{ filter === '*' ? t('allowlist.empty') : t('allowlist.emptyFiltered') }}
    </p>
    <AllowlistEntryList v-else-if="entries.length" :entries="entries" />

    <AllowlistAddDialog
      v-model:open="adding"
      :vms="vmNames"
      :scope="filter === '*' ? 'all' : filter"
    />
  </div>
</template>
