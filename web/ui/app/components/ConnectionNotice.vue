<script setup lang="ts">
// Says plainly when what is shown may be stale: the live connection dropped, or the
// Host's root helper (agentbox watch) that feeds it is not running.
const { t } = useI18n()
const live = useLive()
const now = useNow()

const dropped = computed(() => live.state.connection === 'reconnecting')
const watchStopped = computed(() => live.state.connection === 'live' && live.state.watch?.running === false)
const description = computed(() => {
  if (!live.state.retryAt) return t('connection.reconnecting')
  const seconds = Math.max(0, Math.ceil((live.state.retryAt - now.value) / 1000))
  return t('connection.reconnectingIn', { seconds })
})
</script>

<template>
  <div v-if="dropped || watchStopped" class="px-4 pt-3 sm:px-6" data-testid="connection-notice">
    <UAlert
      v-if="dropped"
      color="warning"
      variant="subtle"
      icon="i-lucide-unplug"
      :title="t('connection.dropped')"
      :description="description"
      :actions="[{ label: t('connection.retryNow'), color: 'warning', variant: 'outline', onClick: () => live.reconnectNow() }]"
    />
    <UAlert
      v-else
      color="warning"
      variant="subtle"
      icon="i-lucide-triangle-alert"
      :title="t('connection.watchStopped')"
    />
  </div>
</template>
