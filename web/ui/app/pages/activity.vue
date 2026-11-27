<script setup lang="ts">
// Activity (#15): one timeline of everything that happened, live.
definePageMeta({ titleKey: 'nav.activity' })
const { t } = useI18n()
const live = useLive()
const { filter, timeline, entries, truncated, retentionDays, loading, showMore, lines } = useActivity()
const MAX_LINES = ACTIVITY_MAX_LINES

const vmNames = computed(() => {
  const names = new Set(live.state.vms.map(v => v.name))
  for (const e of entries.value) if (e.vm) names.add(e.vm)
  if (filter.vm) names.add(filter.vm)
  return [...names].sort()
})
const filtered = computed(() => filter.kinds.length > 0 || !!filter.vm || !!filter.search.trim())
</script>

<template>
  <div class="mx-auto flex w-full max-w-6xl flex-col gap-4" data-testid="activity-page">
    <ActivityFilters v-model="filter" :vms="vmNames" />

    <ReadFailure :resource="timeline" data-testid="activity-error" />
    <!-- a failed read with nothing to show: the failure says it all -->
    <template v-if="!timeline.error.value || entries.length">
      <p v-if="loading && !entries.length" class="text-sm text-muted" data-testid="activity-loading">
        {{ t('activity.loading') }}
      </p>
      <p v-else-if="!entries.length" class="rounded-lg border border-dashed border-default p-8 text-center text-sm text-muted" data-testid="activity-empty">
        {{ filtered ? t('activity.noMatch') : t('activity.empty') }}
      </p>
      <ActivityList v-else :entries="entries" />
    </template>

    <div v-if="truncated" class="flex flex-wrap items-center gap-3 text-sm text-muted" data-testid="activity-truncated">
      <span>{{ t('activity.truncated', { n: entries.length }) }}</span>
      <UButton
        v-if="lines < MAX_LINES"
        color="neutral"
        variant="outline"
        size="sm"
        :label="t('activity.more')"
        :loading="loading"
        data-testid="activity-more"
        @click="showMore"
      />
    </div>
    <p v-if="retentionDays" class="text-xs text-dimmed" data-testid="activity-retention">
      {{ t('activity.retention', { days: retentionDays }) }}
    </p>
  </div>
</template>
