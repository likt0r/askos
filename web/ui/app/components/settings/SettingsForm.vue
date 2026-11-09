<script setup lang="ts">
// All settings, in groups, with one Save for the changed ones (all or nothing).
const { t, te } = useI18n()
const errorText = useErrorText()
const s = useSettings()
const { info, draft, errors, keys, dirty, loading, loadError, saving, saveError, saved } = s
const { chosen, followDefaultLanguage } = useLanguage()

onMounted(() => void s.load())

// a new default language applies in this browser as soon as PATCH answers, before the form
// reloads (unless a language was chosen here)
watch(saved, (answer) => {
  const language = answer?.changed?.default_language
  if (language) void followDefaultLanguage(language.to)
})

// settings of a key this UI does not know yet (a newer backend) still show, at the end
const extra = computed(() => keys.value.filter(k => !(k in SETTING_SPECS)))
const groups = computed(() => [
  ...SETTING_GROUPS.map(g => ({ ...g, keys: g.keys.filter(k => k in info.value) })).filter(g => g.keys.length),
  ...(extra.value.length ? [{ id: 'other', keys: extra.value }] : []),
])

const changedKeys = computed(() => saved.value ? Object.keys(saved.value.changed ?? {}) : [])
const laterKeys = computed(() => changedKeys.value.filter(k => saved.value?.applies_later && k in saved.value.applies_later
  && !(k === 'default_language' && !chosen.value)))      // it applied here already
const problems = computed(() => Object.keys(errors).length)

function label(key: string) {
  const path = `settings.fields.${key}.label`
  return te(path) ? t(path) : key
}
</script>

<template>
  <div class="flex flex-col gap-4" data-testid="settings-form">
    <p v-if="loading && !keys.length" class="text-sm text-muted" data-testid="settings-loading">
      {{ t('settings.loading') }}
    </p>
    <UAlert
      v-if="loadError"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(loadError)"
      :actions="[{ label: t('settings.retry'), color: 'error', variant: 'outline', onClick: () => s.load() }]"
      data-testid="settings-load-error"
    />

    <UAlert
      v-if="saved"
      color="success"
      variant="subtle"
      icon="i-lucide-check"
      :title="changedKeys.length ? t('settings.saved', changedKeys.length) : t('settings.unchanged')"
      data-testid="settings-saved"
    >
      <template v-if="changedKeys.length" #description>
        <ul class="list-disc ps-4">
          <li v-for="k in changedKeys" :key="k">
            {{ label(k) }}<span v-if="laterKeys.includes(k)"> — {{ t('settings.savedLater') }}</span>
          </li>
        </ul>
      </template>
    </UAlert>
    <UAlert
      v-if="saveError"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(saveError)"
      data-testid="settings-save-error"
    />
    <UAlert
      v-if="problems"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="t('settings.problems', { n: problems })"
      data-testid="settings-problems"
    />

    <section v-for="g in groups" :key="g.id" class="rounded-lg border border-default bg-default" :data-group="g.id">
      <header class="border-b border-default px-4 py-3">
        <h2 class="font-semibold text-highlighted">
          {{ t(`settings.groups.${g.id}`) }}
        </h2>
      </header>
      <div class="divide-y divide-default px-4">
        <SettingsField
          v-for="k in g.keys"
          :key="k"
          :name="k"
          :info="info[k]!"
          :model-value="draft[k] ?? ''"
          :error="errors[k]"
          @update:model-value="s.setDraft(k, $event)"
          @restore="s.restoreDefault(k)"
        />
      </div>
    </section>

    <div v-if="dirty.length || saving" class="sticky bottom-0 -mx-4 flex flex-wrap items-center gap-3 border-t border-default bg-default/95 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6" data-testid="settings-actions">
      <UButton :label="t('settings.save', dirty.length)" :loading="saving" icon="i-lucide-save" data-testid="settings-save" @click="s.save()" />
      <UButton :label="t('settings.discard')" color="neutral" variant="ghost" :disabled="saving" data-testid="settings-discard" @click="s.discard()" />
    </div>

    <p class="flex items-start gap-2 text-sm text-muted" data-testid="settings-host-only">
      <UIcon name="i-lucide-lock" class="mt-0.5 size-4 shrink-0" />
      <span>{{ t('settings.hostOnly') }}</span>
    </p>
  </div>
</template>
