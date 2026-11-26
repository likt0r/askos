<script setup lang="ts">
// A failure in the UI's words (useErrorText), with the Host's text behind Details
// (failureDetails: its code and message, never its detail).
const props = defineProps<{ error: unknown }>()
const errorText = useErrorText()
const details = computed(() => failureDetails(props.error))
</script>

<template>
  <div class="space-y-1" data-testid="failure-text">
    <p>{{ errorText(props.error) }}</p>
    <FaultDetails v-if="details" :message="details" />
  </div>
</template>
