<script setup lang="ts">
// Whether updates need a Host reboot (checked every six hours), with the reminder that
// the disk passphrase must be entered at boot.
const { t, locale } = useI18n()
const live = useLive()
const reboot = computed(() => live.state.hostReboot as { needed: boolean | null, packages?: string[], checked?: string } | null)
</script>

<template>
  <section aria-labelledby="system-reboot-title" data-testid="system-reboot" :data-needed="String(reboot?.needed ?? 'unknown')">
    <h2 id="system-reboot-title" class="sr-only">
      {{ t('system.reboot.title') }}
    </h2>
    <UAlert
      v-if="reboot?.needed === true"
      color="warning"
      variant="subtle"
      icon="i-lucide-power"
      :title="t('system.reboot.needed')"
    >
      <template #description>
        <p>{{ t('system.reboot.passphrase') }}</p>
        <p v-if="reboot.packages?.length" class="mt-1 text-xs" data-testid="reboot-packages">
          {{ t('system.reboot.packages', { packages: reboot.packages.slice(0, 12).join(', ') }) }}<template v-if="reboot.packages.length > 12">
            {{ t('system.reboot.more', { n: reboot.packages.length - 12 }) }}
          </template>
        </p>
      </template>
    </UAlert>
    <p v-else class="flex items-center gap-2 text-sm text-muted">
      <UIcon :name="reboot?.needed === false ? 'i-lucide-circle-check' : 'i-lucide-circle-help'" class="size-4" :class="reboot?.needed === false ? 'text-success' : ''" />
      {{ reboot?.needed === false ? t('system.reboot.notNeeded') : t('system.reboot.unknown') }}
      <span v-if="reboot?.checked" class="text-dimmed">· {{ t('system.reboot.checked', { when: formatWhen(reboot.checked, locale, false) }) }}</span>
    </p>
  </section>
</template>
