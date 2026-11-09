<script setup lang="ts">
// Which events this browser shows as notifications (kept in this browser only), and the
// browser's permission for them.
const { t } = useI18n()
const { prefs, support, setKind, enable, refresh } = useNotificationPrefs()
const asking = ref(false)

// the permission can be changed in the browser's site settings while this page stays open
onMounted(() => {
  refresh()
  window.addEventListener('focus', refresh)
  document.addEventListener('visibilitychange', refresh)
})
onBeforeUnmount(() => {
  window.removeEventListener('focus', refresh)
  document.removeEventListener('visibilitychange', refresh)
})

async function ask() {
  asking.value = true
  try {
    await enable()
  }
  finally {
    asking.value = false
  }
}
</script>

<template>
  <section class="rounded-lg border border-default bg-default" data-testid="notification-prefs">
    <header class="border-b border-default px-4 py-3">
      <h2 class="font-semibold text-highlighted">
        {{ t('notifications.title.section') }}
      </h2>
      <p class="text-sm text-muted">
        {{ t('notifications.intro') }}
      </p>
    </header>
    <div class="flex flex-col gap-3 px-4 py-3">
      <UAlert
        v-if="support === 'unsupported'"
        color="warning"
        variant="subtle"
        icon="i-lucide-bell-off"
        :title="t('notifications.unsupported')"
        data-testid="notify-unsupported"
      />
      <UAlert
        v-else-if="support === 'denied'"
        color="warning"
        variant="subtle"
        icon="i-lucide-bell-off"
        :title="t('notifications.denied')"
        data-testid="notify-denied"
      />
      <div v-else-if="support === 'default'" class="flex flex-wrap items-center gap-3" data-testid="notify-ask">
        <UButton icon="i-lucide-bell-ring" :label="t('notifications.enable')" :loading="asking" @click="ask" />
        <span class="text-sm text-muted">{{ t('notifications.enableHint') }}</span>
      </div>
      <p v-else class="flex items-center gap-1.5 text-sm text-success" data-testid="notify-granted">
        <UIcon name="i-lucide-bell-ring" class="size-4" />
        {{ t('notifications.granted') }}
      </p>

      <ul class="divide-y divide-default">
        <li v-for="kind in NOTIFICATION_KINDS" :key="kind" class="flex items-center justify-between gap-4 py-2" data-testid="notify-kind" :data-kind="kind">
          <div class="min-w-0">
            <p class="text-sm font-medium text-highlighted">
              {{ t(`notifications.kind.${kind}.label`) }}
            </p>
            <p class="text-xs text-muted">
              {{ t(`notifications.kind.${kind}.help`) }}
            </p>
          </div>
          <USwitch
            :model-value="prefs[kind]"
            :aria-label="t(`notifications.kind.${kind}.label`)"
            data-testid="notify-switch"
            @update:model-value="setKind(kind, $event)"
          />
        </li>
      </ul>
    </div>
  </section>
</template>
