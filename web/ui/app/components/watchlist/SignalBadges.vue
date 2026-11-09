<script setup lang="ts">
// Signal names as badges (blocked destinations, Alerts), each explained in a tooltip.
const props = defineProps<{ names: string[] }>()
const { t, te } = useI18n()

function label(s: string) {
  return te(`signals.names.${s}`) ? t(`signals.names.${s}`) : s
}
function about(s: string) {
  return te(`signals.about.${s}`) ? t(`signals.about.${s}`) : label(s)
}
</script>

<template>
  <span class="inline-flex flex-wrap gap-1" data-testid="signal-badges">
    <UTooltip v-for="s in props.names" :key="s" :text="about(s)">
      <UBadge color="warning" variant="subtle" size="sm" :label="label(s)" :data-signal="s" />
    </UTooltip>
  </span>
</template>
