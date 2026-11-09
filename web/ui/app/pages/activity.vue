<script setup lang="ts">
// Activity (#15): one timeline of everything that happened, live.
definePageMeta({ titleKey: 'nav.activity' })
const { t } = useI18n()
const live = useLive()
const errorText = useErrorText()
const { filter, entries, truncated, retentionDays, loading, error, reload, showMore, lines } = useActivity()

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

    <UAlert
      v-if="error"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(error)"
      :actions="[{ label: t('activity.retry'), color: 'error', variant: 'outline', onClick: () => reload() }]"
      data-testid="activity-error"
    />
    <p v-else-if="loading && !entries.length" class="text-sm text-muted" data-testid="activity-loading">
      {{ t('activity.loading') }}
    </p>
    <p v-else-if="!entries.length" class="rounded-lg border border-dashed border-default p-8 text-center text-sm text-muted" data-testid="activity-empty">
      {{ filtered ? t('activity.noMatch') : t('activity.empty') }}
    </p>
    <ActivityList v-else :entries="entries" />

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
