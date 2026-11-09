<script setup lang="ts">
// English, German, or Automatic: no choice in this browser, so the default language of the
// Settings (once signed in) or the browser's decides. Collapsed (narrow sidebar, sign-in page):
// a toggle between the languages, and next to it an Automatic button once one was chosen.
const props = defineProps<{ collapsed?: boolean }>()
const { t } = useI18n()
const { locale, options, chosen, choose } = useLanguage()

const items = computed(() => [{ value: AUTO_LANGUAGE, label: t('language.auto') }, ...options.value])
const next = computed(() => options.value.find(o => o.value !== locale.value)?.value ?? 'en')
</script>

<template>
  <template v-if="props.collapsed">
    <UButton
      color="neutral"
      variant="ghost"
      icon="i-lucide-languages"
      :aria-label="t('language.label')"
      :label="locale.toUpperCase()"
      data-testid="language-toggle"
      @click="choose(next)"
    />
    <UButton
      v-if="chosen"
      color="neutral"
      variant="ghost"
      icon="i-lucide-undo-2"
      :aria-label="t('language.auto')"
      :title="t('language.auto')"
      data-testid="language-auto"
      @click="choose(AUTO_LANGUAGE)"
    />
  </template>
  <USelect
    v-else
    :model-value="chosen ?? AUTO_LANGUAGE"
    :items="items"
    icon="i-lucide-languages"
    variant="ghost"
    :aria-label="t('language.label')"
    data-testid="language-switch"
    @update:model-value="(v: string) => choose(v)"
  />
</template>
