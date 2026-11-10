<script setup lang="ts">
// The VM's VM variables, like `agentbox set-env --replace`: a table of names and values
// that is saved as a whole (PUT /vms/<vm>/env). They reach orca serve, the agents and
// terminals it starts and login shells at the VM's next start, so a running (or paused)
// VM keeps its variables until its next Restart, which can follow right here. Names a
// Safeguard or Agent Box itself reads are refused by agentbox (env_name_reserved); the
// dialog says which.
import type { Vm } from '~/utils/live'
import type { EnvRow } from '~/utils/vmRules'

const props = defineProps<{ vm: Pick<Vm, 'name' | 'state' | 'libvirt_state' | 'agents' | 'env'> }>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const maxVariables = MAX_ENV_VARIABLES
const rows = ref<EnvRow[]>([])
const saving = ref(false)
const restarting = ref(false)
const error = ref<string | null>(null)
// the variables just saved for a VM that runs or is paused: the dialog then offers the Restart
const saved = ref(false)

function current(): Record<string, string> {
  const env = props.vm.env
  if (!env || typeof env !== 'object') return {}
  return Object.fromEntries(Object.entries(env as Record<string, unknown>).filter(([, v]) => typeof v === 'string')) as Record<string, string>
}

watch(open, (v) => {
  if (!v) return
  rows.value = Object.entries(current()).map(([name, value]) => ({ name, value }))
  if (!rows.value.length) rows.value.push({ name: '', value: '' })
  error.value = null
  saved.value = false
})

const checked = computed(() => envFromRows(rows.value))
const changed = computed(() => JSON.stringify(Object.entries(checked.value.env)) !== JSON.stringify(Object.entries(current())))
const ready = computed(() => !Object.keys(checked.value.problems).length && !checked.value.tooMany && changed.value)
// off, or libvirt has no domain yet: the next start brings them (agentbox's next_start)
const off = computed(() => props.vm.state === 'stopped' || props.vm.libvirt_state === null)
const working = computed(() => props.vm.agents?.working ?? 0)
const canRestart = computed(() => lifecycleActions(props.vm).restart)
const busy = computed(() => saving.value || restarting.value)

function problemText(i: number): string | undefined {
  const p = checked.value.problems[i]
  return p ? t(`vm.env.problem.${p}`, { max: MAX_ENV_VALUE_BYTES }) : undefined
}

function add() {
  rows.value.push({ name: '', value: '' })
}

function remove(i: number) {
  rows.value.splice(i, 1)
}

/** agentbox's refusal, naming the variable where it gives one. */
function refusal(e: unknown): string {
  const name = e instanceof ApiError ? e.params?.name : undefined
  if (e instanceof ApiError && typeof name === 'string' && ['env_name_reserved', 'invalid_env_value', 'env_too_large'].includes(e.code))
    return t(`vm.env.named.${e.code}`, { name, max: MAX_ENV_VALUE_BYTES })
  return errorText(e)
}

async function save() {
  if (!ready.value || busy.value) return
  const name = props.vm.name
  saving.value = true
  error.value = null
  try {
    const r = await api<{ applies?: string | null }>(`/vms/${name}/env`, { method: 'PUT', body: { env: checked.value.env } })
    if (r?.applies === 'next_restart') {
      saved.value = true
      return
    }
    open.value = false
    toast.add({
      title: t('vm.env.saved', { name }),
      description: r?.applies === 'next_start' ? t('vm.env.atNextStart') : undefined,
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
  const name = props.vm.name
  restarting.value = true
  error.value = null
  try {
    await api(`/vms/${name}/restart`, { method: 'POST' })
    open.value = false
    toast.add({ title: t('vm.actions.done.restart', { name }), color: 'success', icon: 'i-lucide-check' })
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
    :title="t('vm.env.title', { name: props.vm.name })"
    :description="t('vm.env.text')"
    :dismissible="!busy"
    :close="!busy"
  >
    <template #body>
      <form v-if="!saved" class="space-y-4" data-testid="env-dialog" novalidate @submit.prevent="save">
        <table class="w-full text-sm" data-testid="env-table">
          <thead>
            <tr class="text-left text-xs text-muted">
              <th class="pb-1 pr-2 font-medium">
                {{ t('vm.env.name') }}
              </th>
              <th class="pb-1 pr-2 font-medium">
                {{ t('vm.env.value') }}
              </th>
              <th class="w-8" />
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in rows" :key="i" class="align-top" data-testid="env-row">
              <td class="py-1 pr-2">
                <UInput
                  v-model="row.name"
                  class="w-full font-mono"
                  :aria-label="t('vm.env.name')"
                  :color="checked.problems[i] === 'name' || checked.problems[i] === 'twice' ? 'error' : undefined"
                  autocomplete="off"
                  spellcheck="false"
                  :data-testid="`env-name-${i}`"
                />
                <p v-if="problemText(i)" class="mt-1 text-xs text-error" :data-testid="`env-problem-${i}`">
                  {{ problemText(i) }}
                </p>
              </td>
              <td class="py-1 pr-2">
                <UInput
                  v-model="row.value"
                  class="w-full font-mono"
                  :aria-label="t('vm.env.value')"
                  autocomplete="off"
                  spellcheck="false"
                  :data-testid="`env-value-${i}`"
                />
              </td>
              <td class="py-1">
                <UButton
                  color="neutral"
                  variant="ghost"
                  icon="i-lucide-trash-2"
                  :aria-label="t('vm.env.remove')"
                  :data-testid="`env-remove-${i}`"
                  @click="remove(i)"
                />
              </td>
            </tr>
          </tbody>
        </table>
        <UButton
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-plus"
          :label="t('vm.env.add')"
          :disabled="rows.length >= maxVariables"
          data-testid="env-add"
          @click="add"
        />
        <p v-if="checked.tooMany" class="text-sm text-error" data-testid="env-too-many">
          {{ t('vm.env.tooMany', { max: maxVariables }) }}
        </p>
        <p class="text-xs text-muted">
          {{ t('vm.env.safeguards') }}
        </p>
        <UAlert
          color="info"
          variant="subtle"
          icon="i-lucide-info"
          :title="off ? t('vm.env.atNextStart') : t('vm.env.atNextRestart')"
          data-testid="env-applies"
        />
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="env-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton color="neutral" variant="ghost" :label="t('vm.confirm.cancel')" :disabled="saving" @click="open = false" />
          <UButton type="submit" :label="t('vm.env.save')" :loading="saving" :disabled="!ready" data-testid="env-save" />
        </div>
      </form>

      <div v-else class="space-y-4" data-testid="env-saved">
        <UAlert
          color="success"
          variant="subtle"
          icon="i-lucide-check"
          :title="t('vm.env.savedRestart', { name: props.vm.name })"
          data-testid="env-saved-text"
        />
        <p v-if="canRestart" class="text-sm text-muted">
          {{ t('vm.restart.text') }}
        </p>
        <p v-if="canRestart && working > 0" class="text-sm text-warning" data-testid="env-agents">
          {{ t('vm.reset.agentsWorking', working) }}
        </p>
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="env-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton
            color="neutral"
            variant="ghost"
            :label="t('vm.env.later')"
            :disabled="restarting"
            data-testid="env-later"
            @click="open = false"
          />
          <UButton
            v-if="canRestart"
            color="warning"
            icon="i-lucide-rotate-cw"
            :label="t('vm.env.restartNow')"
            :loading="restarting"
            data-testid="env-restart"
            @click="restart"
          />
        </div>
      </div>
    </template>
  </UModal>
</template>
