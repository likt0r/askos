<script setup lang="ts">
// Every address no other page matches (the backend answers every app path with index.html).
definePageMeta({ titleKey: 'fault.notFound.nav' })
</script>

<template>
  <FaultView kind="notFound" />
</template>
