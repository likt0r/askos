<script setup lang="ts">
// A reading's history as a line (inline SVG: attributes only, no style attribute, CSP).
import type { HostHistoryPoint } from '~/utils/host'

const props = defineProps<{
  history: HostHistoryPoint[]
  id: string
  /** fixed scale (100 for percentages); otherwise the series' own range */
  max?: number
  min?: number
  windowSeconds: number
  over?: boolean
}>()

const W = 120
const H = 32
const points = computed(() => hostSeries(props.history, props.id))
const line = computed(() => {
  const last = points.value[points.value.length - 1]
  return sparkPoints(points.value, W, H, {
    min: props.min, max: props.max,
    from: last ? last.time - props.windowSeconds : undefined, to: last?.time,
  })
})
</script>

<template>
  <svg
    :viewBox="`0 0 ${W} ${H}`"
    class="h-8 w-full"
    preserveAspectRatio="none"
    role="img"
    :aria-label="id"
    data-testid="sparkline"
  >
    <polyline
      v-if="line"
      :points="line"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linejoin="round"
      stroke-linecap="round"
      vector-effect="non-scaling-stroke"
      :class="over ? 'text-error' : 'text-primary'"
    />
  </svg>
</template>
