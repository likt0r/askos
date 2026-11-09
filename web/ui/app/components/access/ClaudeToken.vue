<script setup lang="ts">
// Sets or renews the VM's Claude token. The backend checks it with Anthropic before it
// replaces the old one. The token is never shown again: the field is a password field,
// emptied as soon as it was sent, whatever the answer.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const live = useLive()
const errorText = useErrorText()
const when = useWhen()

const token = ref('')
const busy = ref(false)
const error = ref<unknown>(null)
const stored = ref<{ length: number } | null>(null)

const since = computed(() => (typeof props.vm.claude_token_since === 'string' ? props.vm.claude_token_since : null))
const looksRight = computed(() => {
  const v = token.value.replace(/\s+/g, '')
  return !v || v.startsWith('sk-ant-')
})

/** a refusal of the token itself: the stored one is still in use */
const oldStays = computed(() => error.value instanceof ApiError
  && ['claude_token_rejected', 'anthropic_unavailable', 'invalid_claude_token', 'invalid_field'].includes(error.value.code))

async function submit() {
  const value = token.value.replace(/\s+/g, '')
  if (!value || busy.value) return
  // gone from the page before the answer comes
  token.value = ''
  busy.value = true
  error.value = null
  stored.value = null
  try {
    const r = await api<{ stored: boolean, length: number }>(`/vms/${props.vm.name}/claude-token`, { method: 'PUT', body: { token: value } })
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
  <section class="space-y-3 rounded-lg border border-default p-4" data-testid="claude-token">
    <div>
      <h3 class="font-medium text-highlighted">
        {{ t('access.claude.title') }}
      </h3>
      <p class="text-sm" data-testid="claude-since">
        <template v-if="since">
          {{ t('access.claude.since', { when: when.at(since) }) }}
        </template>
        <template v-else>
          {{ t('access.claude.none') }}
        </template>
      </p>
      <p class="text-sm text-muted">
        {{ t('access.claude.intro') }}
      </p>
    </div>
    <form class="space-y-2" novalidate @submit.prevent="submit">
      <UFormField :label="since ? t('access.claude.renew') : t('access.claude.set')" name="claude-token" :help="t('access.claude.help')">
        <UInput
          v-model="token"
          type="password"
          autocomplete="off"
          spellcheck="false"
          placeholder="sk-ant-oat…"
          class="w-full font-mono"
          :color="looksRight ? undefined : 'error'"
          data-testid="claude-input"
        />
      </UFormField>
      <p v-if="!looksRight" class="text-sm text-error">
        {{ t('errors.invalid_claude_token') }}
      </p>
      <div class="flex justify-end">
        <UButton type="submit" icon="i-lucide-shield-check" :label="t('access.claude.submit')" :loading="busy" :disabled="!token.trim() || !looksRight" data-testid="claude-submit" />
      </div>
    </form>
    <UAlert
      v-if="stored"
      color="success"
      variant="subtle"
      icon="i-lucide-circle-check"
      :title="t('access.claude.stored', { length: stored.length })"
      :description="t('access.claude.restart', { vm: props.vm.name })"
      data-testid="claude-stored"
    />
    <UAlert
      v-if="error"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(error)"
      :description="oldStays ? t('access.claude.oldStays') : undefined"
      role="alert"
      data-testid="claude-error"
    />
  </section>
</template>
