<script setup lang="ts">
const props = defineProps<{ state: string }>()
const { t, te } = useI18n()

const color = computed(() => ({
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
const label = computed(() => te(`state.${props.state}`) ? t(`state.${props.state}`) : t('state.unknown'))
</script>

<template>
  <UBadge :color="color" variant="subtle" :label="label" data-testid="vm-state" :data-state="props.state" :data-color="color" />
</template>
