<script setup lang="ts">
// The VM's memory limit, like `agentbox set-memory`: a whole number of MiB from 1024 up to
// the Host's memory (agentbox checks the upper bound). It applies at the VM's next start,
// so a running VM keeps its memory until its next Restart, which can follow right here.
// A paused VM is refused: its saved memory belongs to the current limit.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Pick<Vm, 'name' | 'state' | 'libvirt_state' | 'memory_mib' | 'agents'> }>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const value = ref<string | number>('')
const saving = ref(false)
const restarting = ref(false)
const error = ref<string | null>(null)
// the limit just saved for a running VM: the dialog then offers the Restart
const saved = ref<number | null>(null)

watch(open, (v) => {
  if (!v) return
  value.value = typeof props.vm.memory_mib === 'number' ? props.vm.memory_mib : ''
  error.value = null
  saved.value = null
})

const current = computed(() => (typeof props.vm.memory_mib === 'number' ? props.vm.memory_mib : null))
const memory = computed(() => wholeNumber(value.value, MIN_MEMORY_MIB, Number.MAX_SAFE_INTEGER))
const inputError = computed(() => (memory.value === 'invalid' ? t('vm.memory.invalid', { min: MIN_MEMORY_MIB }) : null))
const hint = computed(() => (typeof memory.value === 'number' ? t('vm.create.inGib', { n: mibAsGib(memory.value) }) : undefined))
const paused = computed(() => props.vm.state === 'paused')
// off, or libvirt has no domain yet: the next start uses the new limit (agentbox's next_start)
const off = computed(() => props.vm.state === 'stopped' || props.vm.libvirt_state === null)
const working = computed(() => props.vm.agents?.working ?? 0)
const ready = computed(() => typeof memory.value === 'number' && memory.value !== current.value && !paused.value)
const canRestart = computed(() => lifecycleActions(props.vm).restart)
const busy = computed(() => saving.value || restarting.value)

async function save() {
  if (!ready.value || busy.value) return
  const name = props.vm.name
  const mib = memory.value as number
  saving.value = true
  error.value = null
  try {
    const r = await api<{ applies?: string | null }>(`/vms/${name}/memory`, { method: 'PUT', body: { memory_mib: mib } })
    if (r?.applies === 'next_restart') {
      saved.value = mib
      return
    }
    open.value = false
    toast.add({
      title: t('vm.memory.saved', { name, gib: mibAsGib(mib) }),
      description: r?.applies === 'next_start' ? t('vm.memory.atNextStart') : undefined,
      color: 'success',
      icon: 'i-lucide-check',
    })
  }
  catch (e) {
    error.value = errorText(e)
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
    :title="t('vm.memory.title', { name: props.vm.name })"
    :description="t('vm.memory.text')"
    :dismissible="!busy"
    :close="!busy"
  >
    <template #body>
      <form v-if="saved === null" class="space-y-4" data-testid="memory-dialog" novalidate @submit.prevent="save">
        <p class="text-sm" data-testid="memory-current">
          <span class="text-muted">{{ t('vm.memory.current') }}:</span>
          {{ current !== null ? t('vm.memory.mib', { mib: current, gib: mibAsGib(current) }) : '—' }}
        </p>
        <UFormField :label="t('vm.memory.limit')" :help="inputError ? undefined : hint" :error="inputError ?? undefined">
          <UInput v-model="value" inputmode="numeric" class="w-full" data-testid="memory-input">
            <template #trailing>
              <span class="text-xs text-muted">MiB</span>
            </template>
          </UInput>
        </UFormField>
        <UAlert
          v-if="paused"
          color="warning"
          variant="subtle"
          icon="i-lucide-pause"
          :title="t('vm.memory.paused')"
          data-testid="memory-paused"
        />
        <UAlert
          v-else
          color="info"
          variant="subtle"
          icon="i-lucide-info"
          :title="off ? t('vm.memory.atNextStart') : t('vm.memory.atNextRestart')"
          data-testid="memory-applies"
        />
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="memory-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton color="neutral" variant="ghost" :label="t('vm.confirm.cancel')" :disabled="saving" @click="open = false" />
          <UButton type="submit" :label="t('vm.memory.save')" :loading="saving" :disabled="!ready" data-testid="memory-save" />
        </div>
      </form>

      <div v-else class="space-y-4" data-testid="memory-saved">
        <UAlert
          color="success"
          variant="subtle"
          icon="i-lucide-check"
          :title="t('vm.memory.savedRestart', { name: props.vm.name, gib: mibAsGib(saved ?? 0) })"
          data-testid="memory-saved-text"
        />
        <p v-if="canRestart" class="text-sm text-muted">
          {{ t('vm.restart.text') }}
        </p>
        <p v-if="canRestart && working > 0" class="text-sm text-warning" data-testid="memory-agents">
          {{ t('vm.reset.agentsWorking', working) }}
        </p>
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="memory-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton
            color="neutral"
            variant="ghost"
            :label="t('vm.memory.later')"
            :disabled="restarting"
            data-testid="memory-later"
            @click="open = false"
          />
          <UButton
            v-if="canRestart"
            color="warning"
            icon="i-lucide-rotate-cw"
            :label="t('vm.memory.restartNow')"
            :loading="restarting"
            data-testid="memory-restart"
            @click="restart"
          />
        </div>
      </div>
    </template>
  </UModal>
</template>
