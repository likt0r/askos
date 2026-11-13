<script setup lang="ts">
// Sets or renews the VM's OpenCode key (an API key from opencode.ai, for OpenCode Go). The
// backend checks it with opencode.ai before it replaces the old one. The key is never shown
// again: the field is a password field, emptied as soon as it was sent, whatever the answer.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const live = useLive()
const errorText = useErrorText()
const when = useWhen()

const key = ref('')
const busy = ref(false)
const error = ref<unknown>(null)
const stored = ref<{ length: number } | null>(null)

const since = computed(() => (typeof props.vm.opencode_key_since === 'string' ? props.vm.opencode_key_since : null))
const looksRight = computed(() => {
  const v = key.value.replace(/\s+/g, '')
  return !v || (v.startsWith('sk-') && !v.startsWith('sk-ant-'))
})

/** a refusal of the key itself: the stored one is still in use */
const oldStays = computed(() => error.value instanceof ApiError
  && ['opencode_key_rejected', 'opencode_unavailable', 'invalid_opencode_key', 'invalid_field'].includes(error.value.code))

async function submit() {
  const value = key.value.replace(/\s+/g, '')
  if (!value || busy.value) return
  // gone from the page before the answer comes
  key.value = ''
  busy.value = true
  error.value = null
  stored.value = null
  try {
    const r = await api<{ stored: boolean, length: number }>(`/vms/${props.vm.name}/opencode-key`, { method: 'PUT', body: { key: value } })
    stored.value = { length: typeof r?.length === 'number' ? r.length : 0 }
    void live.refetchVms()
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="space-y-3 rounded-lg border border-default p-4" data-testid="opencode-key">
    <div>
      <h3 class="font-medium text-highlighted">
        {{ t('access.opencode.title') }}
      </h3>
      <p class="text-sm" data-testid="opencode-since">
        <template v-if="since">
          {{ t('access.opencode.since', { when: when.at(since) }) }}
        </template>
        <template v-else>
          {{ t('access.opencode.none') }}
        </template>
      </p>
      <p class="text-sm text-muted">
        {{ t('access.opencode.intro') }}
      </p>
    </div>
    <form class="space-y-2" novalidate @submit.prevent="submit">
      <UFormField :label="since ? t('access.opencode.renew') : t('access.opencode.set')" name="opencode-key" :help="t('access.opencode.help')">
        <UInput
          v-model="key"
          type="password"
          autocomplete="off"
          spellcheck="false"
          placeholder="sk-…"
          class="w-full font-mono"
          :color="looksRight ? undefined : 'error'"
          data-testid="opencode-input"
        />
      </UFormField>
      <p v-if="!looksRight" class="text-sm text-error">
        {{ t('errors.invalid_opencode_key') }}
      </p>
      <div class="flex justify-end">
        <UButton type="submit" icon="i-lucide-shield-check" :label="t('access.opencode.submit')" :loading="busy" :disabled="!key.trim() || !looksRight" data-testid="opencode-submit" />
      </div>
    </form>
    <UAlert
      v-if="stored"
      color="success"
      variant="subtle"
      icon="i-lucide-circle-check"
      :title="t('access.opencode.stored', { length: stored.length })"
      :description="t('access.opencode.restart', { vm: props.vm.name })"
      data-testid="opencode-stored"
    />
    <UAlert
      v-if="error"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(error)"
      :description="oldStays ? t('access.opencode.oldStays') : undefined"
      role="alert"
      data-testid="opencode-error"
    />
  </section>
</template>
