<script setup lang="ts">
// A change's failure at the form or in the dialog that sent it (ADR 0026): what failed in the
// UI's words, Details, and what the view adds (`description`). `error` is the change's `shown`:
// none for a code the view words itself or a failure shown elsewhere.
const props = defineProps<{ error: unknown, description?: string }>()
</script>

<template>
  <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" role="alert" data-change="failed">
    <template #description>
      <FailureText :error="props.error" />
      <p v-if="props.description" class="mt-1">
        {{ props.description }}
      </p>
    </template>
  </UAlert>
</template>
