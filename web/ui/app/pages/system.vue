<script setup lang="ts">
// System (#16, #17, #18): the Host's readings, Host reboot, Images and Builds, the GitHub App, services.
definePageMeta({ titleKey: 'nav.system' })
const { t } = useI18n()
</script>

<template>
  <div class="mx-auto flex w-full max-w-6xl flex-col gap-8" data-testid="system-page">
    <SystemHostReboot />

    <section class="flex flex-col gap-3" data-testid="system-host" aria-labelledby="system-host-title">
      <h2 id="system-host-title" class="text-lg font-semibold text-highlighted">
        {{ t('system.host.title') }}
      </h2>
      <HostTiles />
    </section>

    <SystemImages />

    <SystemGithubAppSection />

    <SystemServices />
  </div>
</template>
