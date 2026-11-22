<script setup lang="ts">
// A VM's Web tab: Open web, what it was refused in the last 24 hours (allow or forbid from
// there), the Allowlist entries that apply to it, and its Forbids.
import type { AllowlistEntry, ForbidEntry } from '~/utils/access'

const { t } = useI18n()
const route = useRoute()
const live = useLive()
const errorText = useErrorText()

const name = computed(() => String(route.params.vm))
const vm = computed(() => live.state.vms.find(v => v.name === name.value))
const entries = ref<AllowlistEntry[]>([])
const entriesError = ref<unknown>(null)
const entriesLoaded = ref(false)
const forbids = ref<ForbidEntry[]>([])
const forbidsError = ref<unknown>(null)
const forbidsLoaded = ref(false)

async function loadEntries() {
  try {
    const list = await api<AllowlistEntry[]>(`/vms/${name.value}/allowlist`)
    entries.value = Array.isArray(list) ? list : []
    entriesError.value = null
    entriesLoaded.value = true
  }
  catch (e) {
    entriesError.value = e
  }
}

async function loadForbids() {
  try {
    const list = await api<ForbidEntry[]>(`/vms/${name.value}/forbids`)
    forbids.value = Array.isArray(list) ? list : []
    forbidsError.value = null
    forbidsLoaded.value = true
  }
  catch (e) {
    forbidsError.value = e
  }
}

// an allow for the VM lifts its Forbid of the domain, a forbid takes the place of its entry: both lists
// change together (ADR 0024)
function loadBoth() {
  void loadEntries()
  void loadForbids()
}

watch(name, loadBoth)
onMounted(loadBoth)
useLiveEvent('activity', (entry) => {
  if (entry?.kind === 'allowlist') loadBoth()
})
</script>

<template>
  <div v-if="vm" class="space-y-6" data-testid="web-tab">
    <AllowlistOpenWeb :vm="vm" />

    <AllowlistBlockedList :vm="vm.name" :entries="entries" :forbids="forbids" @allowed="loadBoth" @forbidden="loadBoth" />

    <section class="space-y-2" data-testid="vm-entries">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="font-medium text-highlighted">
          {{ t('webTab.entries.title') }}
        </h3>
        <span class="flex-1" />
        <UButton color="neutral" variant="link" size="sm" trailing-icon="i-lucide-arrow-right" :to="`/allowlist?vm=${vm.name}`" :label="t('webTab.entries.all')" />
      </div>
      <UAlert v-if="entriesError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(entriesError)" />
      <p v-else-if="entriesLoaded && !entries.length" class="text-sm text-muted">
        {{ t('webTab.entries.empty') }}
      </p>
      <AllowlistEntryList v-else-if="entries.length" :entries="entries" @changed="loadBoth" />
    </section>

    <section class="space-y-2" data-testid="vm-forbids">
      <h3 class="font-medium text-highlighted">
        {{ t('webTab.forbids.title') }}
      </h3>
      <p class="text-sm text-muted">
        {{ t('webTab.forbids.intro') }}
      </p>
      <UAlert v-if="forbidsError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(forbidsError)" />
      <p v-else-if="forbidsLoaded && !forbids.length" class="text-sm text-muted" data-testid="forbids-empty">
        {{ t('webTab.forbids.empty') }}
      </p>
      <AllowlistForbidList v-else-if="forbids.length" :entries="forbids" @changed="loadBoth" />
    </section>
  </div>
</template>
