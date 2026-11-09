<script setup lang="ts">
// The Host's readings as tiles with their 30-minute history, and the memory of each VM.
// Used by the expanded host bar and by the System page.
import type { HostReading } from '~/utils/host'

const { t, te } = useI18n()
const { sample, readings, history, windowSeconds, unavailable } = useHost()

const memory = computed(() => vmMemory(sample.value?.vms))
const maxVm = computed(() => Math.max(1, ...memory.value.rows.map(r => r.bytes)))
const minutes = computed(() => Math.round(windowSeconds.value / 60))

function label(r: HostReading) {
  return te(`host.kind.${r.kind}`) ? t(`host.kind.${r.kind}`) : r.kind
}

function note(r: HostReading) {
  if (r.value === null) return te(`host.unavailable.${r.unavailable}`) ? t(`host.unavailable.${r.unavailable}`) : t('host.unavailable.failed')
  if ((r.kind === 'memory' || r.kind === 'swap') && r.total_bytes) return `${formatHostBytes(r.used_bytes)} / ${formatHostBytes(r.total_bytes)}`
  if (r.kind === 'cpu_load' && Array.isArray(r.loadavg)) return t('host.loadavg', { avg: r.loadavg.map(n => n.toFixed(2)).join(' ') })
  return ''
}

function percentKind(r: HostReading) {
  return r.unit === '%'
}
</script>

<template>
  <div class="flex flex-col gap-3" data-testid="host-tiles">
    <p v-if="!readings.length" class="text-sm text-muted" data-testid="host-empty">
      {{ unavailable ? t('host.unavailable.all') : t('host.waiting') }}
    </p>
    <ul v-else class="grid grid-cols-1 gap-3 min-[30rem]:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
      <li
        v-for="r in readings"
        :key="r.id"
        class="rounded-lg border bg-default p-3"
        :class="r.over ? 'border-error ring-1 ring-error/40' : 'border-default'"
        data-testid="host-tile"
        :data-id="r.id"
        :data-over="r.over ? 'true' : 'false'"
      >
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <p class="text-xs font-medium uppercase tracking-wide text-dimmed">
              {{ label(r) }}
            </p>
            <p v-if="r.device" class="truncate text-xs text-muted" :title="r.device">
              {{ r.device }}
            </p>
          </div>
          <UBadge
            v-if="r.over"
            color="error"
            variant="subtle"
            size="sm"
            icon="i-lucide-flame"
            :label="t('host.over', { threshold: formatReading(r.threshold, r.unit) })"
          />
        </div>
        <p class="mt-1 text-2xl font-semibold tabular-nums" :class="r.over ? 'text-error' : 'text-highlighted'" data-testid="host-value">
          {{ formatReading(r.value, r.unit) }}
        </p>
        <p class="min-h-4 text-xs text-muted">
          {{ note(r) }}
        </p>
        <HostSparkline
          v-if="r.value !== null"
          class="mt-1"
          :history="history"
          :id="r.id"
          :min="percentKind(r) ? 0 : undefined"
          :max="percentKind(r) ? 100 : undefined"
          :window-seconds="windowSeconds"
          :over="r.over"
        />
        <p v-if="r.threshold !== null" class="text-xs text-dimmed">
          {{ t('host.threshold', { value: formatReading(r.threshold, r.unit) }) }}
        </p>
      </li>
    </ul>

    <div v-if="memory.rows.length" class="rounded-lg border border-default bg-default p-3" data-testid="host-vm-memory">
      <p class="text-xs font-medium uppercase tracking-wide text-dimmed">
        {{ t('host.vmMemory', { total: formatHostBytes(memory.total) }) }}
      </p>
      <ul class="mt-2 flex flex-col gap-1.5">
        <li v-for="row in memory.rows" :key="row.name" class="grid grid-cols-[minmax(4rem,8rem)_1fr_5rem] items-center gap-2 text-sm" data-testid="host-vm-row">
          <span class="truncate font-medium text-highlighted">{{ row.name }}</span>
          <span class="h-2 rounded-full bg-elevated">
            <span class="block h-2 rounded-full bg-primary" :style="{ width: `${Math.max(2, (row.bytes / maxVm) * 100)}%` }" />
          </span>
          <span class="text-right tabular-nums text-muted">{{ formatHostBytes(row.bytes) }}</span>
        </li>
      </ul>
    </div>
    <p class="text-xs text-dimmed">
      {{ t('host.historyNote', { minutes }) }}
    </p>
  </div>
</template>
