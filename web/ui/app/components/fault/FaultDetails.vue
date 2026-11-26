<script setup lang="ts">
// "Details" of a fault: its message, folded away and copyable; never a stack trace.
const props = defineProps<{ message: string }>()
const { t } = useI18n()
const copied = ref(false)

async function copy() {
  try {
    await navigator.clipboard.writeText(props.message)
    copied.value = true
  }
  catch {
    // the text stays selectable
  }
}
</script>

<template>
  <details class="text-sm" data-testid="fault-details">
    <summary class="cursor-pointer text-muted">
      {{ t('fault.details') }}
    </summary>
    <div class="mt-2 flex items-start gap-2">
      <pre class="min-w-0 flex-1 whitespace-pre-wrap break-words rounded bg-elevated p-2 font-mono text-xs select-all" data-testid="fault-message">{{ props.message }}</pre>
      <UButton
        color="neutral"
        variant="ghost"
        size="xs"
        :icon="copied ? 'i-lucide-clipboard-check' : 'i-lucide-clipboard'"
        :label="copied ? t('fault.copied') : t('fault.copy')"
        data-testid="fault-copy"
        @click="copy"
      />
    </div>
  </details>
</template>
