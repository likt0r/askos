<script setup lang="ts">
// Sets, renews or removes the VM's Claude token. The backend checks a new one with Anthropic
// before it replaces the old one. The token never enters the VM: its Anthropic route on the Host
// uses it from the VM's next request on (ADR 0023). It is never shown again: the field is a
// password field, emptied as soon as it was sent, whatever the answer.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const when = useWhen()

const token = ref('')
const set = useChange('setClaudeToken', { form: true })
const unset = useChange('removeClaudeToken', { form: true })
const busy = set.busy
const removeBusy = unset.busy
const stored = ref<{ length: number } | null>(null)
const removeOpen = ref(false)
const removed = ref(false)

const since = computed(() => (typeof props.vm.claude_token_since === 'string' ? props.vm.claude_token_since : null))
const looksRight = computed(() => {
  const v = token.value.replace(/\s+/g, '')
  return !v || v.startsWith('sk-ant-')
})

/** a refusal of the token itself: the stored one is still in use */
const oldStays = computed(() => set.shown.value instanceof ApiError
  && ['claude_token_rejected', 'anthropic_unavailable', 'invalid_claude_token', 'invalid_field'].includes(set.shown.value.code))

async function submit() {
  const value = token.value.replace(/\s+/g, '')
  if (!value || busy.value) return
  // gone from the page before the answer comes
  token.value = ''
  stored.value = null
  removed.value = false
  const r = await set.run({ vm: props.vm.name, token: value })
  if (!r.ok) return
  const length = (r.data as { length?: number } | null)?.length
  stored.value = { length: typeof length === 'number' ? length : 0 }
}

function askRemove() {
  unset.reset()
  removeOpen.value = true
}

async function remove() {
  if (removeBusy.value) return
  set.reset()
  stored.value = null
  if (!(await unset.run(props.vm.name)).ok) return
  removed.value = true
  removeOpen.value = false
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
    <ChangeFailure :error="set.shown.value" :description="oldStays ? t('access.claude.oldStays') : undefined" data-testid="claude-error" />
    <AccessConfirmDialog
      v-model:open="removeOpen"
      :title="t('access.claude.remove.title', { vm: props.vm.name })"
      :description="t('access.claude.remove.text', { vm: props.vm.name })"
      :confirm-label="t('access.claude.remove.confirm')"
      :busy="removeBusy"
      :failure="unset.shown.value"
      @confirm="remove"
    />
  </section>
</template>
