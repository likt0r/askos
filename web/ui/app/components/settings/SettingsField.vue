<script setup lang="ts">
// One setting: label and help, the input, its default, when it applies, and its problem.
import type { Problem, SettingInfo } from '~/utils/settings'

const props = defineProps<{
  name: string
  info: SettingInfo
  modelValue: string
  error?: Problem | { code: 'server', error: unknown }
}>()
const emit = defineEmits<{ 'update:modelValue': [string], 'restore': [] }>()
const { t, te } = useI18n()
const errorText = useErrorText()

const spec = computed(() => SETTING_SPECS[props.name])
const id = computed(() => `setting-${props.name}`)
const changed = computed(() => settingChanged(props.name, props.modelValue, props.info))
const atDefault = computed(() => props.modelValue.trim() === String(props.info.default))
const label = computed(() => te(`settings.fields.${props.name}.label`) ? t(`settings.fields.${props.name}.label`) : props.name)
const help = computed(() => te(`settings.fields.${props.name}.help`) ? t(`settings.fields.${props.name}.help`) : '')
// the API's own text (English) only for a key this UI does not know
const later = computed(() => {
  if (!props.info.applies_later) return ''
  return te(`settings.later.${props.name}`) ? t(`settings.later.${props.name}`) : props.info.applies_later
})

function optionLabel(value: string) {
  return te(`settings.options.${props.name}.${value}`) ? t(`settings.options.${props.name}.${value}`) : value
}

function show(v: string | number | boolean) {
  const s = String(v)
  const sp = spec.value
  if (sp?.type === 'choice') return optionLabel(s)
  if (sp?.type === 'version' && !s) return t('settings.automatic')
  return sp?.type === 'int' && sp.unit ? `${s} ${sp.unit}` : s
}

const message = computed(() => {
  const e = props.error
  if (!e) return ''
  if (e.code === 'server') return `${errorText(e.error)} ${hint.value}`.trim()
  if (e.code === 'empty') return t('settings.problem.empty')
  // the options by their labels (Monday, 1 hour), not by their codes
  if (e.code === 'choice') return hint.value
  return t(`settings.problem.${e.code}`, e)
})

const hint = computed(() => {
  const sp = spec.value
  if (!sp) return props.info.allowed
  if (sp.type === 'int') return t('settings.problem.int', { min: sp.min, max: sp.max })
  if (sp.type === 'choice') return t('settings.problem.choice', { options: sp.options.map(optionLabel).join(', ') })
  return t(`settings.problem.${sp.type}`)
})
</script>

<template>
  <div
    class="grid gap-x-6 gap-y-1.5 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]"
    data-testid="setting"
    :data-key="name"
    :data-changed="changed ? 'true' : 'false'"
  >
    <div class="min-w-0">
      <label :for="id" class="text-sm font-medium text-highlighted">{{ label }}</label>
      <p v-if="help" class="text-sm text-muted">
        {{ help }}
      </p>
      <p v-if="later" class="mt-1 flex items-start gap-1 text-xs text-info" data-testid="setting-later">
        <UIcon name="i-lucide-clock" class="mt-0.5 size-3.5 shrink-0" />
        <span>{{ later }}</span>
      </p>
    </div>
    <div class="flex min-w-0 flex-col gap-1">
      <select
        v-if="spec?.type === 'choice'"
        :id="id"
        :value="modelValue"
        class="w-full rounded-md border bg-default px-2.5 py-1.5 text-sm text-highlighted focus-visible:outline-2 focus-visible:outline-primary"
        :class="error ? 'border-error' : 'border-default'"
        :aria-invalid="!!error"
        :aria-describedby="`${id}-note`"
        data-testid="setting-input"
        @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="o in spec.options" :key="o" :value="o">
          {{ optionLabel(o) }}
        </option>
      </select>
      <UInput
        v-else
        :id="id"
        :model-value="modelValue"
        :type="spec?.type === 'time' ? 'time' : 'text'"
        :placeholder="spec?.type === 'version' ? t('settings.automatic') : undefined"
        :inputmode="spec?.type === 'int' ? 'numeric' : undefined"
        :color="error ? 'error' : undefined"
        :highlight="!!error"
        :aria-invalid="!!error"
        :aria-describedby="`${id}-note`"
        :ui="{ base: 'tabular-nums' }"
        data-testid="setting-input"
        @update:model-value="emit('update:modelValue', String($event))"
      >
        <template v-if="spec?.type === 'int' && spec.unit" #trailing>
          <span class="text-xs text-dimmed">{{ spec.unit }}</span>
        </template>
      </UInput>
      <div :id="`${id}-note`" class="flex flex-wrap items-center gap-x-2 text-xs">
        <p v-if="error" class="text-error" role="alert" data-testid="setting-error">
          {{ message }}
        </p>
        <template v-else>
          <span class="text-dimmed" data-testid="setting-default">{{ t('settings.default', { value: show(info.default) }) }}</span>
          <button
            v-if="!atDefault"
            type="button"
            class="text-primary underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-primary"
            data-testid="setting-restore"
            @click="emit('restore')"
          >
            {{ t('settings.restore') }}
          </button>
        </template>
      </div>
    </div>
  </div>
</template>
