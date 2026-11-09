<script setup lang="ts">
// The entries of the timeline, newest first, grouped by day.
import type { ActivityEntry } from '~/utils/activity'

const props = defineProps<{ entries: ActivityEntry[] }>()
const { t, te, locale } = useI18n()

const KIND_COLORS: Record<string, 'error' | 'warning' | 'success' | 'info' | 'neutral' | 'primary' | 'secondary'> = {
  alert: 'error', github: 'warning', allowlist: 'info', signin: 'neutral', action: 'primary',
  pairing: 'secondary', build: 'success', reset: 'warning', settings: 'neutral', credentials: 'secondary',
}

const days = computed(() => {
  const out: { key: string, label: string, entries: ActivityEntry[] }[] = []
  for (const entry of props.entries) {
    const key = localDayKey(entry.time)
    const last = out[out.length - 1]
    if (last && last.key === key) last.entries.push(entry)
    else out.push({ key, label: formatDayHeading(entry.time, locale.value), entries: [entry] })
  }
  return out
})

function kindLabel(kind: string) {
  return te(`activity.kind.${kind}`) ? t(`activity.kind.${kind}`) : kind
}

// the entry in the UI language (#69); agentbox's stored text on hover where that differs
const words = useActivityText()
function detail(e: ActivityEntry, shown: string) {
  return shown !== e.description ? e.description : undefined
}
</script>

<template>
  <div class="flex flex-col gap-4" data-testid="activity-list">
    <section v-for="day in days" :key="day.key" :aria-label="day.label">
      <h2 class="mb-1 text-xs font-medium uppercase tracking-wide text-dimmed">
        {{ day.label }}
      </h2>
      <ul class="divide-y divide-default rounded-lg border border-default bg-default">
        <li
          v-for="(e, i) in day.entries"
          :key="`${e.time}-${i}-${e.action}`"
          class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 px-3 py-2 text-sm md:grid-cols-[5.5rem_6.5rem_7rem_1fr_minmax(8rem,14rem)] md:items-baseline"
          data-testid="activity-row"
          :data-kind="e.kind"
          :data-outcome="e.outcome"
        >
          <time :datetime="e.time" class="tabular-nums text-muted" :title="formatWhen(e.time, locale)">
            {{ formatClock(e.time, locale) }}
          </time>
          <span class="justify-self-start">
            <UBadge :color="KIND_COLORS[e.kind] ?? 'neutral'" variant="subtle" size="sm" :label="kindLabel(e.kind)" />
          </span>
          <span class="col-span-2 font-medium text-highlighted md:col-span-1" :class="{ 'text-dimmed': !e.vm }" data-testid="activity-vm-name">
            {{ e.vm ?? '—' }}
          </span>
          <span
            class="col-span-2 min-w-0 break-words md:col-span-1"
            :class="e.outcome === 'failed' ? 'text-error' : 'text-default'"
            :title="detail(e, words(e))"
            data-testid="activity-text"
          >
            <UIcon v-if="e.outcome === 'failed'" name="i-lucide-circle-x" class="me-1 inline size-4 align-text-bottom" />
            <span class="sr-only">{{ e.outcome === 'failed' ? t('activity.failed') : '' }}</span>
            {{ words(e) }}
          </span>
          <span class="col-span-2 truncate text-xs text-muted md:col-span-1" :title="e.actor ?? undefined">
            {{ e.actor ? t('activity.by', { actor: e.actor }) : '' }}
          </span>
        </li>
      </ul>
    </section>
  </div>
</template>
