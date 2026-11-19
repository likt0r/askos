<script setup lang="ts">
// byAlert: a Pause an Alert with a strong Signal made (#11)
const props = defineProps<{ state: string, byAlert?: boolean }>()
const { t, te } = useI18n()

const alerted = computed(() => props.state === 'paused' && props.byAlert)
const color = computed(() => alerted.value ? 'error' : ({
  running: 'success',
  // libvirt reports both as running; Orca has not answered yet / for too long (#54)
  starting: 'info',
  not_responding: 'error',
  paused: 'info',
  stopped: 'neutral',
  suspended: 'warning',
  shutting_down: 'warning',
  crashed: 'error',
} as const)[props.state] ?? 'neutral')
const label = computed(() => alerted.value ? t('state.paused_by_alert')
  : te(`state.${props.state}`) ? t(`state.${props.state}`) : t('state.unknown'))
</script>

<template>
  <UBadge :color="color" variant="subtle" :label="label" data-testid="vm-state" :data-state="props.state" :data-color="color" :data-by-alert="alerted ? 'true' : undefined" />
</template>
