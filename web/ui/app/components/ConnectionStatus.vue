<script setup lang="ts">
// The live connection's state, always visible (sidebar footer, navbar on phones).
const props = defineProps<{ collapsed?: boolean, compact?: boolean }>()

const { t } = useI18n()
const live = useLive()
const now = useNow()

// whole class names, so Tailwind finds them
const dot = computed(() => {
  switch (live.state.connection) {
    case 'live': return live.state.watch?.running === false ? 'bg-warning' : 'bg-success'
    case 'connecting': return 'bg-info'
    case 'reconnecting': return 'bg-warning'
    default: return 'bg-inverted/40'
  }
})

const label = computed(() => {
  const c = live.state.connection
  if (c === 'reconnecting' && live.state.retryAt) {
    const seconds = Math.max(0, Math.ceil((live.state.retryAt - now.value) / 1000))
    return t('connection.reconnectingIn', { seconds })
  }
  return t(`connection.${c}`)
})
</script>

<template>
  <UTooltip :text="label" :disabled="!props.collapsed && !props.compact">
    <div
      class="flex items-center gap-2 text-sm px-2.5 py-1.5 min-w-0"
      role="status"
      :aria-label="t('connection.label') + ': ' + label"
      data-testid="connection-status"
      :data-connection="live.state.connection"
    >
      <span class="relative flex size-2.5 shrink-0">
        <span
          v-if="live.state.connection === 'live'"
          class="absolute inline-flex size-full rounded-full opacity-60 animate-ping motion-reduce:animate-none"
          :class="dot"
        />
        <span class="relative inline-flex size-2.5 rounded-full" :class="dot" />
      </span>
      <span v-if="!props.collapsed && !props.compact" class="truncate text-muted">{{ label }}</span>
    </div>
  </UTooltip>
</template>
