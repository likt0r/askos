<script setup lang="ts">
// One secret of the VM, like `agentbox set-secret` / `unset-secret`: add one (name and value),
// replace the value of one, or remove one. The value is typed into a password field, sent
// once (PUT /vms/<vm>/secrets/<name>, which hands it to agentbox on stdin) and dropped from the
// dialog after; nothing ever reads it back, the VM object names the secrets only. Like the VM
// variables, a change reaches the VM at its next start, so a running (or paused) VM keeps its
// secrets until its next Restart, which can follow right here.
import type { Vm } from '~/utils/live'

const props = defineProps<{
  vm: Pick<Vm, 'name' | 'state' | 'libvirt_state' | 'agents' | 'env' | 'secrets'>
  /** the secret to replace or remove; null adds a new one */
  secret: string | null
  mode: 'set' | 'remove'
}>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const name = ref('')
const value = ref('')
const saving = ref(false)
const restarting = ref(false)
const error = ref<string | null>(null)
// changed for a VM that runs or is paused: the dialog then offers the Restart
const saved = ref(false)

const maxBytes = MAX_ENV_VALUE_BYTES

// opened or closed: a fresh form, and no value kept
watch(open, () => {
  name.value = props.secret ?? ''
  value.value = ''
  error.value = null
  saved.value = false
})

const adding = computed(() => props.secret === null)
const removing = computed(() => props.mode === 'remove')
const problem = computed(() => (removing.value ? null : secretProblem(name.value.trim(), value.value, props.vm, adding.value)))
// a name is shown as wrong once something was typed; the value only once it is wrong
const shownProblem = computed(() => (problem.value === 'name' && !name.value ? null : problem.value))
const ready = computed(() => removing.value || problem.value === null)
const off = computed(() => props.vm.state === 'stopped' || props.vm.libvirt_state === null)
const working = computed(() => props.vm.agents?.working ?? 0)
const canRestart = computed(() => lifecycleActions(props.vm).restart)
const busy = computed(() => saving.value || restarting.value)
const title = computed(() => (adding.value
  ? t('vm.secrets.titleAdd', { name: props.vm.name })
  : t(removing.value ? 'vm.secrets.titleRemove' : 'vm.secrets.titleReplace', { name: props.vm.name, secret: props.secret })))

/** agentbox's refusal, naming the secret where it gives one (never its value). */
function refusal(e: unknown): string {
  const named = e instanceof ApiError ? e.params?.name : undefined
  if (e instanceof ApiError && typeof named === 'string'
    && ['secret_name_reserved', 'secret_name_is_env', 'invalid_secret_value', 'secret_too_large'].includes(e.code))
    return t(`vm.secrets.named.${e.code}`, { name: named, max: MAX_ENV_VALUE_BYTES })
  return errorText(e)
}

async function send() {
  if (!ready.value || busy.value) return
  const vm = props.vm.name
  const secret = name.value.trim()
  saving.value = true
  error.value = null
  try {
    const r = removing.value
      ? await api<{ applies?: string | null }>(`/vms/${vm}/secrets/${secret}`, { method: 'DELETE' })
      : await api<{ applies?: string | null }>(`/vms/${vm}/secrets/${secret}`, { method: 'PUT', body: { value: value.value } })
    value.value = ''
    if (r?.applies === 'next_restart') {
      saved.value = true
      return
    }
    open.value = false
    toast.add({
      title: t(removing.value ? 'vm.secrets.removed' : 'vm.secrets.saved', { name: vm, secret }),
      description: r?.applies === 'next_start' ? t('vm.secrets.atNextStart') : undefined,
      color: 'success',
      icon: 'i-lucide-check',
    })
  }
  catch (e) {
    error.value = refusal(e)
  }
  finally {
    saving.value = false
  }
}

async function restart() {
  if (busy.value) return
  const vm = props.vm.name
  restarting.value = true
  error.value = null
  try {
    await api(`/vms/${vm}/restart`, { method: 'POST' })
    open.value = false
    toast.add({ title: t('vm.actions.done.restart', { name: vm }), color: 'success', icon: 'i-lucide-check' })
  }
  catch (e) {
    error.value = errorText(e)
  }
  finally {
    restarting.value = false
  }
}
</script>

<template>
  <UModal
    v-model:open="open"
    :title="title"
    :description="t('vm.secrets.text')"
    :dismissible="!busy"
    :close="!busy"
  >
    <template #body>
      <form v-if="!saved" class="space-y-4" data-testid="secret-dialog" novalidate @submit.prevent="send">
        <p v-if="removing" class="text-sm" data-testid="secret-remove-text">
          {{ t('vm.secrets.removeText') }}
        </p>
        <template v-else>
          <UFormField v-if="adding" :label="t('vm.secrets.name')">
            <UInput
              v-model="name"
              class="w-full font-mono"
              :color="shownProblem === 'name' || shownProblem === 'env' || shownProblem === 'exists' ? 'error' : undefined"
              autocomplete="off"
              spellcheck="false"
              data-testid="secret-name"
            />
          </UFormField>
          <UFormField :label="t('vm.secrets.value')" :help="t('vm.secrets.valueHelp')">
            <UInput
              v-model="value"
              type="password"
              class="w-full font-mono"
              autocomplete="new-password"
              spellcheck="false"
              data-testid="secret-value"
            />
          </UFormField>
          <p v-if="shownProblem" class="text-xs text-error" data-testid="secret-problem">
            {{ t(`vm.secrets.problem.${shownProblem}`, { max: maxBytes }) }}
          </p>
        </template>
        <UAlert
          color="info"
          variant="subtle"
          icon="i-lucide-info"
          :title="off ? t('vm.secrets.atNextStart') : t('vm.secrets.atNextRestart')"
          data-testid="secret-applies"
        />
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="secret-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton color="neutral" variant="ghost" :label="t('vm.confirm.cancel')" :disabled="saving" @click="open = false" />
          <UButton
            type="submit"
            :color="removing ? 'error' : 'primary'"
            :label="removing ? t('vm.secrets.confirmRemove') : t('vm.secrets.save')"
            :loading="saving"
            :disabled="!ready"
            data-testid="secret-send"
          />
        </div>
      </form>

      <div v-else class="space-y-4" data-testid="secret-saved">
        <UAlert
          color="success"
          variant="subtle"
          icon="i-lucide-check"
          :title="t('vm.secrets.savedRestart', { name: props.vm.name })"
          data-testid="secret-saved-text"
        />
        <p v-if="canRestart" class="text-sm text-muted">
          {{ t('vm.restart.text') }}
        </p>
        <p v-if="canRestart && working > 0" class="text-sm text-warning" data-testid="secret-agents">
          {{ t('vm.reset.agentsWorking', working) }}
        </p>
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="secret-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton
            color="neutral"
            variant="ghost"
            :label="t('vm.env.later')"
            :disabled="restarting"
            data-testid="secret-later"
            @click="open = false"
          />
          <UButton
            v-if="canRestart"
            color="warning"
            icon="i-lucide-rotate-cw"
            :label="t('vm.env.restartNow')"
            :loading="restarting"
            data-testid="secret-restart"
            @click="restart"
          />
        </div>
      </div>
    </template>
  </UModal>
</template>
