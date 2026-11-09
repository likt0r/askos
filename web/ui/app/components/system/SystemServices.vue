<script setup lang="ts">
// The Host's agentbox units and Ollama: running, stopped or failed. A service that a timer or
// a socket starts (systemd's TriggeredBy) is shown under it, and waits for it while inactive.
const { t } = useI18n()
const live = useLive()

interface Unit { unit: string, load: string, active: string, sub: string, description: string, triggered_by?: string[] | null }
interface Group { unit: Unit, started: Unit[] }
const units = computed<Unit[] | null>(() => {
  const s = live.state.services as { units?: Unit[] } | null
  return Array.isArray(s?.units) ? s.units : null
})

/** The timers and sockets that start the unit (none when unknown). */
function triggers(u: Unit): string[] {
  return Array.isArray(u.triggered_by) ? u.triggered_by : []
}

/** A unit that should run: a service (or socket) that is not active. Timers and one-shot
 * services (a Build, the Reset check) are fine inactive between their runs. */
function color(u: Unit): 'success' | 'error' | 'warning' | 'neutral' {
  if (u.active === 'failed' || u.load === 'not-found' || u.load === 'error') return 'error'
  if (u.active === 'active' || u.active === 'activating' || u.active === 'reloading') return 'success'
  return 'neutral'
}

/** Each unit under the first listed timer or socket that starts it (one level deep), the
 * groups with a failed unit first, else by name. */
const groups = computed<Group[]>(() => {
  const list = units.value ?? []
  const listed = new Set(list.map(u => u.unit))
  const startedBy = (u: Unit) => triggers(u).filter(n => n !== u.unit && listed.has(n))
  const heads = new Map<string, Group>()
  for (const u of list) {
    if (!startedBy(u).length) heads.set(u.unit, { unit: u, started: [] })
  }
  for (const u of list) {
    if (heads.has(u.unit)) continue
    const head = startedBy(u).map(n => heads.get(n)).find(g => g !== undefined)
    if (head) head.started.push(u)
    else heads.set(u.unit, { unit: u, started: [] })
  }
  const byName = (a: Unit, b: Unit) => a.unit.localeCompare(b.unit)
  const hasError = (g: Group) => [g.unit, ...g.started].some(u => color(u) === 'error')
  return [...heads.values()]
    .map(g => ({ unit: g.unit, started: [...g.started].sort(byName) }))
    .sort((a, b) => Number(hasError(b)) - Number(hasError(a)) || byName(a.unit, b.unit))
})
const failed = computed(() => (units.value ?? []).filter(u => color(u) === 'error').length)

/** systemd's state; for a unit a timer or socket starts, inactive between its runs, what it waits for. */
function stateText(u: Unit) {
  const by = triggers(u)
  if (by.length && u.active === 'inactive' && color(u) === 'neutral') {
    if (by.some(n => n.endsWith('.socket'))) return t('system.services.startsOnConnection')
    if (by.some(n => n.endsWith('.timer'))) return t('system.services.waitsForTimer')
    return t('system.services.waitsForTrigger')
  }
  return u.sub && u.sub !== u.active ? `${u.active} (${u.sub})` : u.active
}
</script>

<template>
  <section class="flex flex-col gap-3" data-testid="system-services" aria-labelledby="system-services-title">
    <div class="flex flex-wrap items-center gap-3">
      <h2 id="system-services-title" class="text-lg font-semibold text-highlighted">
        {{ t('system.services.title') }}
      </h2>
      <UBadge v-if="units && failed" color="error" variant="subtle" :label="t('system.services.failed', { n: failed })" data-testid="services-failed" />
    </div>
    <p v-if="!units" class="text-sm text-muted" data-testid="services-unknown">
      {{ t('system.services.unknown') }}
    </p>
    <ul v-else class="divide-y divide-default rounded-lg border border-default bg-default">
      <li v-for="g in groups" :key="g.unit.unit" data-testid="service-group" :data-unit="g.unit.unit">
        <div
          v-for="(u, i) in [g.unit, ...g.started]"
          :key="u.unit"
          class="grid items-center gap-x-3 gap-y-0.5 py-2 pe-4 text-sm"
          :class="i ? 'grid-cols-[auto_1fr_auto] ps-6' : 'grid-cols-[1fr_auto] ps-4'"
          data-testid="service-row"
          :data-unit="u.unit"
          :data-color="color(u)"
          :data-trigger="i ? g.unit.unit : undefined"
        >
          <UIcon v-if="i" name="i-lucide-corner-down-right" class="row-span-2 mt-0.5 size-4 self-start text-dimmed" />
          <span class="min-w-0 truncate font-mono text-highlighted">{{ u.unit }}</span>
          <UBadge :color="color(u)" variant="subtle" size="sm" :label="stateText(u)" />
          <span class="col-span-2 truncate text-xs text-muted">{{ u.description }}</span>
        </div>
      </li>
    </ul>
  </section>
</template>
