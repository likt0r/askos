<script setup lang="ts">
// "A new version of the Control Room is installed": reload with one click.
const { t } = useI18n()
const { visible, reload, dismiss } = useReloadPrompt()
</script>

<template>
  <div v-if="visible" class="px-4 pt-3 sm:px-6" data-testid="reload-prompt">
    <UAlert
      color="info"
      variant="subtle"
      icon="i-lucide-refresh-cw"
      :title="t('reload.title')"
      :description="t('reload.text')"
      :actions="[{ label: t('reload.now'), 'data-testid': 'reload-now', color: 'info', variant: 'solid', onClick: reload }, { label: t('reload.later'), 'data-testid': 'reload-later', color: 'neutral', variant: 'ghost', onClick: dismiss }]"
    />
  </div>
</template>
