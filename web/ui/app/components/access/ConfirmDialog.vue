<script setup lang="ts">
// Asks before a destructive action (removing an Allowlist entry, a harmless mark, ...).
// The parent runs the action on `confirm` and closes the dialog when it is done.
const props = withDefaults(defineProps<{
  title: string
  description?: string
  confirmLabel: string
  color?: 'error' | 'warning' | 'primary'
  busy?: boolean
  error?: string | null
}>(), { color: 'error', description: undefined, busy: false, error: null })
const emit = defineEmits<{ confirm: [] }>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
</script>

<template>
  <UModal v-model:open="open" :title="props.title" :description="props.description" :dismissible="!props.busy">
    <template v-if="$slots.default || props.error" #body>
      <div class="space-y-3" data-testid="confirm-body">
        <slot />
        <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="props.error" role="alert" />
      </div>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="props.busy" data-testid="confirm-cancel" @click="open = false" />
        <UButton
          :color="props.color"
          :label="props.confirmLabel"
          :loading="props.busy"
          data-testid="confirm-action"
          @click="emit('confirm')"
        />
      </div>
    </template>
  </UModal>
</template>
