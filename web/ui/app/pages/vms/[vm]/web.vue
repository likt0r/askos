<script setup lang="ts">
// A VM's Web tab: Open web, what it was refused in the last 24 hours (allow from there),
// and the Allowlist entries that apply to it.
import type { AllowlistEntry } from '~/utils/access'

const { t } = useI18n()
const route = useRoute()
const live = useLive()
const errorText = useErrorText()

const name = computed(() => String(route.params.vm))
const vm = computed(() => live.state.vms.find(v => v.name === name.value))
const entries = ref<AllowlistEntry[]>([])
const entriesError = ref<unknown>(null)
const entriesLoaded = ref(false)

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

watch(name, loadEntries)
onMounted(loadEntries)
useLiveEvent('activity', (entry) => {
  if (entry?.kind === 'allowlist') void loadEntries()
})
</script>

<template>
  <div v-if="vm" class="space-y-6" data-testid="web-tab">
    <AllowlistOpenWeb :vm="vm" />

    <AllowlistBlockedList :vm="vm.name" :entries="entries" @allowed="loadEntries" />

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
      <AllowlistEntryList v-else-if="entries.length" :entries="entries" @changed="loadEntries" />
    </section>
  </div>
</template>
