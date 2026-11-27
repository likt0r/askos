<script setup lang="ts">
// A VM's Web tab: Open web, what it was refused in the last 24 hours (allow or forbid from
// there), the Allowlist entries that apply to it, and its Forbids.
import type { AllowlistEntry, ForbidEntry } from '~/utils/access'

const { t } = useI18n()
const route = useRoute()
const live = useLive()

const name = computed(() => String(route.params.vm))
const vm = computed(() => live.state.vms.find(v => v.name === name.value))
// an allow for the VM lifts its Forbid of the domain, a forbid takes the place of its entry: both
// lists change together (ADR 0024; the resources module's rows)
const entriesList = useResource('vmAllowlist', { params: () => ({ vm: name.value }) })
const forbidsList = useResource('forbids', { params: () => ({ vm: name.value }) })
const entries = computed<AllowlistEntry[]>(() => (Array.isArray(entriesList.data.value) ? entriesList.data.value : []))
const forbids = computed<ForbidEntry[]>(() => (Array.isArray(forbidsList.data.value) ? forbidsList.data.value : []))
const entriesLoaded = computed(() => entriesList.data.value !== undefined)
const forbidsLoaded = computed(() => forbidsList.data.value !== undefined)
</script>

<template>
  <div v-if="vm" class="space-y-6" data-testid="web-tab">
    <AllowlistOpenWeb :vm="vm" />

    <AllowlistBlockedList :vm="vm.name" :entries="entries" :forbids="forbids" />

    <section class="space-y-2" data-testid="vm-entries">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="font-medium text-highlighted">
          {{ t('webTab.entries.title') }}
        </h3>
        <span class="flex-1" />
        <UButton color="neutral" variant="link" size="sm" trailing-icon="i-lucide-arrow-right" :to="`/allowlist?vm=${vm.name}`" :label="t('webTab.entries.all')" />
      </div>
      <ReadFailure :resource="entriesList" data-testid="vm-entries-error" />
      <p v-if="entriesLoaded && !entries.length" class="text-sm text-muted">
        {{ t('webTab.entries.empty') }}
      </p>
      <AllowlistEntryList v-else-if="entries.length" :entries="entries" />
    </section>

    <section class="space-y-2" data-testid="vm-forbids">
      <h3 class="font-medium text-highlighted">
        {{ t('webTab.forbids.title') }}
      </h3>
      <p class="text-sm text-muted">
        {{ t('webTab.forbids.intro') }}
      </p>
      <ReadFailure :resource="forbidsList" data-testid="vm-forbids-error" />
      <p v-if="forbidsLoaded && !forbids.length" class="text-sm text-muted" data-testid="forbids-empty">
        {{ t('webTab.forbids.empty') }}
      </p>
      <AllowlistForbidList v-else-if="forbids.length" :entries="forbids" />
    </section>
  </div>
</template>
