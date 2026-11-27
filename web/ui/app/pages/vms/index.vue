<script setup lang="ts">
definePageMeta({ titleKey: 'nav.vms' })

const { t } = useI18n()
const { vms, problems, loaded } = useVms()
const filter = ref('')
const createOpen = ref(false)

const shown = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return vms.value
  return vms.value.filter(vm => vm.name.includes(q) || (vm.image ?? '').toLowerCase().includes(q) || vm.state.includes(q))
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-3">
      <UInput
        v-model="filter"
        icon="i-lucide-search"
        :placeholder="t('vms.filter')"
        :aria-label="t('vms.filter')"
        class="w-full sm:w-64"
        data-testid="vm-filter"
      />
      <span v-if="loaded" class="text-sm text-muted" data-testid="vm-count">
        {{ t('vms.count', vms.length) }}
      </span>
      <span class="flex-1" />
      <UButton icon="i-lucide-plus" :label="t('vm.create.open')" data-testid="vm-create" @click="createOpen = true" />
    </div>
    <VmCreateDialog v-model:open="createOpen" />

    <p v-if="!loaded" class="text-sm text-muted" data-testid="vms-loading">
      {{ t('vms.loading') }}
    </p>
    <p v-else-if="!vms.length" class="text-sm text-muted" data-testid="vms-empty">
      {{ t('vm.create.empty') }}
    </p>
    <p v-else-if="!shown.length" class="text-sm text-muted">
      {{ t('vms.noMatch', { filter }) }}
    </p>
    <VmList v-else :vms="shown" />

    <UAlert
      v-if="problems.length"
      color="warning"
      variant="subtle"
      icon="i-lucide-triangle-alert"
      :title="t('vms.problems')"
      data-testid="vm-problems"
    >
      <template #description>
        <ul class="list-disc ps-4">
          <li v-for="p in problems" :key="p.vm">
            <span class="font-medium">{{ p.vm }}</span>: {{ p.problem }}
          </li>
        </ul>
      </template>
    </UAlert>
  </div>
</template>
