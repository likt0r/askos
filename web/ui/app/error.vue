<script setup lang="ts">
// Shown in place of the whole app when the shell itself fails (the layout cannot be shown);
// a fault in one view stays in the page area (components/fault/FaultBoundary.vue).
import type { NuxtError } from '#app'

const props = defineProps<{ error: NuxtError }>()
const notFound = computed(() => props.error.status === 404)
</script>

<template>
  <UApp>
    <main class="min-h-svh flex items-center justify-center bg-muted p-4" data-testid="error-page">
      <FaultView :kind="notFound ? 'notFound' : 'shell'" :message="notFound ? undefined : faultMessage(props.error.cause ?? props.error)" />
    </main>
  </UApp>
</template>
