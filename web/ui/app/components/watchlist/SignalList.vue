<script setup lang="ts">
// Signals of a destination with why each applies (check-domain). The reason names the list
// entry, Feed or pattern that matched, worded from its code and parameters (#69); the
// classifier's English text where the UI cannot word it, and on hover.
import type { SignalReport } from '~/utils/access'

const props = defineProps<{ signals: SignalReport[] }>()
const { t, te } = useI18n()
const tr = useTranslator()

function name(s: string) {
  return signalName(s, tr)
}
function about(s: string) {
  return te(`signals.about.${s}`) ? t(`signals.about.${s}`) : ''
}
function reason(s: SignalReport) {
  return signalReason(s, tr)
}
</script>

<template>
  <ul class="space-y-2" data-testid="signal-list">
    <li v-for="s in props.signals" :key="s.signal + s.reason" class="text-sm" :data-signal="s.signal">
      <div class="flex flex-wrap items-center gap-2">
        <UBadge :color="s.strong ? 'error' : 'warning'" variant="subtle" :label="name(s.signal)" />
        <UBadge v-if="s.strong" color="error" variant="outline" size="sm" :label="t('signals.strong')" />
        <UBadge v-if="s.suppressed" color="neutral" variant="outline" size="sm" :label="t('signals.suppressed')" />
      </div>
      <p v-if="about(s.signal)" class="mt-1 text-muted">
        {{ about(s.signal) }}
      </p>
      <p class="mt-0.5 break-words text-xs text-dimmed" data-testid="signal-reason" :title="reason(s) !== s.reason ? s.reason : undefined">
        {{ reason(s) }}
      </p>
    </li>
  </ul>
</template>
