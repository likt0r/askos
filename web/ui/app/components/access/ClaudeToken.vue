<script setup lang="ts">
// Sets, renews or removes the VM's Claude token. The backend checks a new one with Anthropic
// before it replaces the old one. The token never enters the VM: its Anthropic route on the Host
// uses it from the VM's next request on (ADR 0023). It is never shown again: the field is a
// password field, emptied as soon as it was sent, whatever the answer.
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
const removeOpen = ref(false)
const removeBusy = ref(false)
const removeError = ref<string | null>(null)
const removed = ref(false)

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
  removed.value = false
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

function askRemove() {
  removeError.value = null
  removeOpen.value = true
}

async function remove() {
  if (removeBusy.value) return
  removeBusy.value = true
  removeError.value = null
  error.value = null
  stored.value = null
  try {
    await api(`/vms/${props.vm.name}/claude-token`, { method: 'DELETE' })
    removed.value = true
    removeOpen.value = false
    void live.refetchVms()
  }
  catch (e) {
    removeError.value = errorText(e)
  }
  finally {
    removeBusy.value = false
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
      <div class="flex flex-wrap justify-end gap-2">
        <UButton
          v-if="since"
          type="button"
          color="error"
          variant="outline"
          icon="i-lucide-trash-2"
          :label="t('access.claude.remove.button')"
          :disabled="busy || removeBusy"
          data-testid="claude-remove"
          @click="askRemove"
        />
        <UButton type="submit" icon="i-lucide-shield-check" :label="t('access.claude.submit')" :loading="busy" :disabled="!token.trim() || !looksRight" data-testid="claude-submit" />
      </div>
    </form>
    <UAlert
      v-if="stored"
      color="success"
      variant="subtle"
      icon="i-lucide-circle-check"
      :title="t('access.claude.stored', { length: stored.length })"
      :description="t('access.claude.effect', { vm: props.vm.name })"
      data-testid="claude-stored"
    />
    <UAlert
      v-if="removed"
      color="success"
      variant="subtle"
      icon="i-lucide-circle-check"
      :title="t('access.claude.remove.done', { vm: props.vm.name })"
      :description="t('access.claude.remove.revoke')"
      data-testid="claude-removed"
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
    <AccessConfirmDialog
      v-model:open="removeOpen"
      :title="t('access.claude.remove.title', { vm: props.vm.name })"
      :description="t('access.claude.remove.text', { vm: props.vm.name })"
      :confirm-label="t('access.claude.remove.confirm')"
      :busy="removeBusy"
      :error="removeError"
      @confirm="remove"
    />
  </section>
</template>
