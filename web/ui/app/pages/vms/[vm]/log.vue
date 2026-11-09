<script setup lang="ts">
const route = useRoute()
</script>

<template>
  <VmLogView :name="String(route.params.vm)" />
</template>
