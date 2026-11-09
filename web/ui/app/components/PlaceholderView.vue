<script setup lang="ts">
// An empty view for a page that a later ticket fills.
const props = defineProps<{ issue: string }>()
const { t } = useI18n()
</script>

<template>
  <div class="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-default p-10 text-center" data-testid="placeholder">
    <UIcon name="i-lucide-construction" class="size-8 text-dimmed" />
    <p class="font-medium text-highlighted">
      {{ t('placeholder.title') }}
    </p>
    <p class="text-sm text-muted">
      {{ t('placeholder.text', { issue: props.issue }) }}
    </p>
  </div>
</template>
