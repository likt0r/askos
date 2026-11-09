<script setup lang="ts">
// Asks before something that cannot be undone. With `typed`, the confirm button stays
// disabled until that word (the VM's name) is typed, as the CLI's --confirm <vm>.
const props = defineProps<{
  title: string
  description?: string
  confirmLabel: string
  color?: 'error' | 'warning' | 'primary' | 'neutral'
  /** the word to type before confirming */
  typed?: string
  loading?: boolean
  /** an error from the last attempt (already translated) */
  error?: string | null
  testid?: string
}>()
const emit = defineEmits<{ confirm: [] }>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()

const word = ref('')
watch(open, (value) => {
  if (value) word.value = ''
})
const ready = computed(() => !props.loading && (!props.typed || word.value.trim() === props.typed))

function confirm() {
  if (ready.value) emit('confirm')
}
</script>

<template>
  <UModal
    v-model:open="open"
    :title="props.title"
    :description="props.description"
    :dismissible="!props.loading"
    :close="!props.loading"
  >
    <template #body>
      <form class="space-y-4" :data-testid="props.testid" @submit.prevent="confirm">
        <slot />
        <UFormField v-if="props.typed" :label="t('vm.confirm.typeName', { name: props.typed })">
          <UInput
            v-model="word"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            class="w-full font-mono"
            data-testid="confirm-typed"
          />
        </UFormField>
        <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="props.error" data-testid="confirm-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton color="neutral" variant="ghost" :label="t('vm.confirm.cancel')" :disabled="props.loading" data-testid="confirm-cancel" @click="open = false" />
          <UButton
            type="submit"
            :color="props.color ?? 'error'"
            :label="props.confirmLabel"
            :loading="props.loading"
            :disabled="!ready"
            data-testid="confirm-ok"
          />
        </div>
      </form>
    </template>
  </UModal>
</template>
