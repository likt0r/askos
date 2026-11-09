<script setup lang="ts">
// The host bar: memory per VM and the key readings in one line; expands to tiles with the
// 30-minute history. Readings over their threshold are highlighted in the collapsed bar too.
// Open or closed is remembered in this browser.
const { t, te } = useI18n()
const KEY = 'agentbox-hostbar-open'

function stored() {
  try {
    return localStorage.getItem(KEY) === '1'
  }
  catch {
    return false
  }
}

const open = ref(stored())
watch(open, (value) => {
  try {
    localStorage.setItem(KEY, value ? '1' : '0')
  }
  catch {
    // private mode
  }
})

const { sample, readings, unavailable } = useHost()
const chips = computed(() => barReadings(readings.value))
const over = computed(() => overReadings(readings.value))
const memory = computed(() => vmMemory(sample.value?.vms))

function label(r: { kind: string }) {
  return te(`host.short.${r.kind}`) ? t(`host.short.${r.kind}`) : r.kind
}
</script>

<template>
  <section
    class="border-b bg-elevated/40"
    :class="over.length ? 'border-error' : 'border-default'"
    data-testid="host-bar"
    :data-over="over.length ? 'true' : 'false'"
    :data-open="open ? 'true' : 'false'"
  >
    <div class="flex items-center gap-x-4 gap-y-1 px-4 py-1.5 text-sm sm:px-6">
      <div class="flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-1">
        <span class="flex items-center gap-1.5 font-medium text-muted">
          <UIcon name="i-lucide-activity" class="size-4 shrink-0" />
          {{ t('hostBar.title') }}
        </span>
        <span v-if="!chips.length" class="text-dimmed" data-testid="host-bar-empty">
          {{ unavailable ? t('host.unavailable.all') : t('host.waiting') }}
        </span>
        <ul v-else class="flex flex-wrap items-center gap-x-3 gap-y-1" :aria-label="t('hostBar.readings')">
          <li
            v-for="r in chips"
            :key="r.id"
            class="flex items-center gap-1 px-1.5 tabular-nums"
            :class="r.over ? 'rounded bg-error/10 text-error' : 'text-muted'"
            data-testid="host-chip"
            :data-id="r.id"
            :data-over="r.over ? 'true' : 'false'"
            :title="r.device ?? undefined"
          >
            <!-- fixed widths: changing numbers or an over-state must not move the other readings -->
            <UIcon name="i-lucide-flame" class="size-3.5 shrink-0" :class="r.over ? '' : 'invisible'" />
            <span class="inline-block min-w-[7ch] text-right" :class="r.over ? '' : 'text-highlighted'">{{ formatReading(r.value, r.unit) }}</span>
            <span>{{ label(r) }}</span>
          </li>
        </ul>
        <p v-if="memory.rows.length" class="flex flex-wrap items-center gap-x-2 text-muted" data-testid="host-bar-memory">
          <UIcon name="i-lucide-memory-stick" class="size-4 shrink-0" />
          <span v-for="row in memory.rows" :key="row.name" class="tabular-nums" data-testid="host-bar-memory-row" :data-vm="row.name" :data-bytes="row.bytes">
            <span class="inline-block min-w-[7ch] text-right">{{ formatHostBytes(row.bytes) }}</span> <span class="font-medium text-highlighted">{{ row.name }}</span>
          </span>
        </p>
      </div>
      <UButton
        color="neutral"
        variant="ghost"
        size="sm"
        :icon="open ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
        :aria-label="open ? t('hostBar.collapse') : t('hostBar.expand')"
        :aria-expanded="open"
        aria-controls="host-bar-details"
        data-testid="host-bar-toggle"
        @click="open = !open"
      />
    </div>
    <div v-if="open" id="host-bar-details" class="max-h-[55svh] overflow-y-auto px-4 pb-3 sm:px-6" data-testid="host-bar-details">
      <HostTiles />
    </div>
  </section>
</template>
