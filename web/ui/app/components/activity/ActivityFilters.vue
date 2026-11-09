<script setup lang="ts">
// Filter of the Activity timeline: kinds (toggle chips), VM, text search.
import type { ActivityFilter } from '~/utils/activity'

const filter = defineModel<ActivityFilter>({ required: true })
defineProps<{ vms: string[] }>()
const { t } = useI18n()

function toggle(kind: string) {
  filter.value.kinds = filter.value.kinds.includes(kind)
    ? filter.value.kinds.filter(k => k !== kind)
    : [...filter.value.kinds, kind]
}

const active = computed(() => filter.value.kinds.length > 0 || !!filter.value.vm || !!filter.value.search)

function clear() {
  filter.value.kinds = []
  filter.value.vm = ''
  filter.value.search = ''
}
</script>

<template>
  <div class="flex flex-col gap-3" data-testid="activity-filters">
    <div class="flex flex-wrap items-center gap-2">
      <UInput
        v-model="filter.search"
        type="search"
        icon="i-lucide-search"
        :placeholder="t('activity.search')"
        :aria-label="t('activity.search')"
        class="min-w-0 flex-1 basis-48"
        data-testid="activity-search"
      />
      <label class="flex items-center gap-2 text-sm text-muted">
        <span class="sr-only sm:not-sr-only">{{ t('activity.vm') }}</span>
        <select
          v-model="filter.vm"
          class="rounded-md border border-default bg-default px-2.5 py-1.5 text-sm text-highlighted focus-visible:outline-2 focus-visible:outline-primary"
          data-testid="activity-vm"
          :aria-label="t('activity.vm')"
        >
          <option value="">
            {{ t('activity.allVms') }}
          </option>
          <option v-for="name in vms" :key="name" :value="name">
            {{ name }}
          </option>
        </select>
      </label>
      <UButton
        v-if="active"
        color="neutral"
        variant="ghost"
        size="sm"
        icon="i-lucide-x"
        :label="t('activity.clear')"
        data-testid="activity-clear"
        @click="clear"
      />
    </div>
    <ul class="flex flex-wrap gap-1.5" :aria-label="t('activity.kinds')">
      <li v-for="kind in ACTIVITY_KINDS" :key="kind">
        <button
          type="button"
          class="rounded-full border px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-primary"
          :class="filter.kinds.includes(kind) ? 'border-primary bg-primary/10 text-primary' : 'border-default text-muted hover:bg-elevated'"
          :aria-pressed="filter.kinds.includes(kind)"
          data-testid="activity-kind"
          :data-kind="kind"
          @click="toggle(kind)"
        >
          {{ t(`activity.kind.${kind}`) }}
        </button>
      </li>
    </ul>
  </div>
</template>
