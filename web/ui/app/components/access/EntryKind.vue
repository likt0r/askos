<script setup lang="ts">
// Domain or Regex: what a new entry of a list is (ADR 0013). A domain covers its subdomains; a
// pattern matches the whole host name.
const kind = defineModel<'domain' | 'regex'>({ default: 'domain' })
const props = withDefaults(defineProps<{ size?: 'xs' | 'sm' }>(), { size: 'sm' })
const { t } = useI18n()
const kinds = ['domain', 'regex'] as const
</script>

<template>
  <div class="flex gap-1" role="radiogroup" :aria-label="t('regex.kind.label')" data-testid="entry-kind">
    <UButton
      v-for="k in kinds"
      :key="k"
      role="radio"
      :aria-checked="kind === k"
      :color="kind === k ? 'primary' : 'neutral'"
      :variant="kind === k ? 'solid' : 'outline'"
      :size="props.size"
      :label="t(`regex.kind.${k}`)"
      :data-kind="k"
      @click="kind = k"
    />
  </div>
</template>
