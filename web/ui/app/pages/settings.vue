<script setup lang="ts">
// Settings (#22) and this browser's notifications (#21).
definePageMeta({ titleKey: 'nav.settings' })
</script>

<template>
  <div class="mx-auto flex w-full max-w-4xl flex-col gap-4" data-testid="settings-page">
    <SettingsForm />
    <NotificationsPreferences />
  </div>
</template>
