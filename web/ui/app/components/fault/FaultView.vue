<script setup lang="ts">
// What the owner sees in place of a page: an address no page matches, or a fault that stopped
// a view (in the page area of the layout), or a shell that could not be shown (app/error.vue).
const props = defineProps<{ kind: 'notFound' | 'view' | 'shell', message?: string }>()
const { t } = useI18n()

function reload() {
  window.location.reload()
}
</script>

<template>
  <div class="mx-auto flex max-w-xl flex-col items-center gap-3 p-6 text-center" data-testid="fault" :data-kind="props.kind">
    <UIcon :name="props.kind === 'notFound' ? 'i-lucide-search-x' : 'i-lucide-octagon-alert'" class="size-8 text-dimmed" />
    <h2 class="text-lg font-semibold text-highlighted" data-testid="fault-title">
      {{ t(`fault.${props.kind}.title`) }}
    </h2>
    <p class="text-sm text-muted">
      {{ t(`fault.${props.kind}.text`) }}
    </p>
    <UButton
      v-if="props.kind === 'notFound'"
      to="/vms"
      icon="i-lucide-arrow-left"
      :label="t('fault.notFound.back')"
      data-testid="fault-back"
    />
    <UButton
      v-else
      icon="i-lucide-rotate-cw"
      :label="t('fault.reload')"
      data-testid="fault-reload"
      @click="reload"
    />
    <FaultDetails v-if="props.message !== undefined" :message="props.message" class="w-full text-left" />
  </div>
</template>
