<script setup lang="ts">
const route = useRoute()
</script>

<template>
  <VmPairView :name="String(route.params.vm)" />
</template>
