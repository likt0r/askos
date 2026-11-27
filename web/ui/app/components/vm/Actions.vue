<script setup lang="ts">
// A VM's lifecycle: Start/Wake, Pause, Restart, Reset (asked first), Focus, Focus off,
// Delete. Only the request is sent here; the new state arrives as a `vm` event, so the
// buttons follow what the Host reports, not what was clicked.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const toast = useToast()
const { vms } = useVms()

type Action = 'start' | 'pause' | 'restart' | 'reset' | 'focus' | 'focus-off'
const can = computed(() => lifecycleActions(props.vm))

// a button's change fails as a toast; Restart and Reset, asked first, in their dialog
const failed = () => t('vm.actions.failed')
const changes = {
  'start': useChange('startVm', { failed }),
  'pause': useChange('pauseVm', { failed }),
  'restart': useChange('restartVm', { form: true }),
  'reset': useChange('resetVm', { form: true }),
  'focus': useChange('focus', { failed }),
  'focus-off': useChange('focusOff', { failed }),
}
const busy = computed(() => (Object.keys(changes) as Action[]).find(a => changes[a].busy.value) ?? null)

const restartOpen = ref(false)
const resetOpen = ref(false)
const deleteOpen = ref(false)
const resetStart = ref(true)

// other VMs paused (by a Focus or by hand): Focus off is worth offering
const othersPaused = computed(() => vms.value.some(v => v.name !== props.vm.name && v.state === 'paused'))

async function run(action: Action) {
  if (busy.value) return
  const name = props.vm.name
  if (action === 'focus-off') {
    if (!(await changes['focus-off'].run()).ok) return
    toast.add({ title: t('vm.actions.done.focus-off'), color: 'success', icon: 'i-lucide-check' })
  }
  else if (action === 'focus') {
    const r = await changes.focus.run(name)
    if (!r.ok) return
    const others = ((r.data as { others?: { vm: string }[] } | null)?.others ?? []).map(o => o.vm)
    toast.add({
      title: t('vm.actions.done.focus', { name }),
      description: others.length ? t('vm.actions.pausedOthers', { names: others.join(', ') }) : undefined,
      color: 'success',
      icon: 'i-lucide-check',
    })
  }
  else {
    const r = action === 'reset' ? await changes.reset.run({ vm: name, start: resetStart.value }) : await changes[action].run(name)
    if (!r.ok) return
    toast.add({ title: t(`vm.actions.done.${action}`, { name }), color: 'success', icon: 'i-lucide-check' })
  }
  restartOpen.value = false
  resetOpen.value = false
}

function openReset() {
  resetStart.value = ['running', 'starting', 'not_responding', 'paused'].includes(props.vm.state)
  changes.reset.reset()
  resetOpen.value = true
}

function openRestart() {
  changes.restart.reset()
  restartOpen.value = true
}

const nextBuild = computed(() => props.vm.next_build ?? null)
const diskBuild = computed(() => props.vm.system_disk?.build ?? null)
</script>

<template>
  <div class="flex flex-wrap gap-2" data-testid="vm-actions">
    <UButton
      v-if="can.start"
      :icon="can.wake ? 'i-lucide-sunrise' : 'i-lucide-play'"
      :label="can.wake ? t('vm.actions.wake') : t('vm.actions.start')"
      :loading="busy === 'start'"
      :disabled="!!busy"
      size="sm"
      data-testid="action-start"
      @click="run('start')"
    />
    <UButton
      v-if="can.pause"
      icon="i-lucide-pause"
      :label="t('vm.actions.pause')"
      color="neutral"
      variant="outline"
      size="sm"
      :loading="busy === 'pause'"
      :disabled="!!busy"
      data-testid="action-pause"
      @click="run('pause')"
    />
    <UButton
      v-if="can.restart"
      icon="i-lucide-rotate-cw"
      :label="t('vm.actions.restart')"
      color="neutral"
      variant="outline"
      size="sm"
      :loading="busy === 'restart'"
      :disabled="!!busy"
      data-testid="action-restart"
      @click="openRestart"
    />
    <UButton
      v-if="can.reset"
      icon="i-lucide-refresh-ccw-dot"
      :label="t('vm.actions.reset')"
      color="warning"
      variant="outline"
      size="sm"
      :loading="busy === 'reset'"
      :disabled="!!busy"
      data-testid="action-reset"
      @click="openReset"
    />
    <UButton
      icon="i-lucide-crosshair"
      :label="t('vm.actions.focus')"
      color="neutral"
      variant="outline"
      size="sm"
      :loading="busy === 'focus'"
      :disabled="!!busy"
      data-testid="action-focus"
      @click="run('focus')"
    />
    <UButton
      v-if="othersPaused"
      icon="i-lucide-scan"
      :label="t('vm.actions.focusOff')"
      color="neutral"
      variant="ghost"
      size="sm"
      :loading="busy === 'focus-off'"
      :disabled="!!busy"
      data-testid="action-focus-off"
      @click="run('focus-off')"
    />
    <UButton
      icon="i-lucide-trash-2"
      :label="t('vm.actions.delete')"
      color="error"
      variant="ghost"
      size="sm"
      :disabled="!!busy"
      data-testid="action-delete"
      @click="deleteOpen = true"
    />

    <VmConfirmDialog
      v-model:open="restartOpen"
      :title="t('vm.restart.title', { name: props.vm.name })"
      :description="t('vm.restart.text')"
      :confirm-label="t('vm.actions.restart')"
      color="warning"
      :loading="busy === 'restart'"
      :failure="changes.restart.shown.value"
      testid="restart-dialog"
      @confirm="run('restart')"
    />

    <VmConfirmDialog
      v-model:open="resetOpen"
      :title="t('vm.reset.title', { name: props.vm.name })"
      :description="t('vm.reset.text')"
      :confirm-label="t('vm.actions.reset')"
      color="warning"
      :loading="busy === 'reset'"
      :failure="changes.reset.shown.value"
      testid="reset-dialog"
      @confirm="run('reset')"
    >
      <UAlert
        v-if="props.vm.reset_changes_build"
        color="info"
        variant="subtle"
        icon="i-lucide-package"
        :title="t('vm.reset.changes', { image: props.vm.image ?? '', build: nextBuild ?? '?', from: diskBuild ?? '?' })"
        data-testid="reset-changes"
      />
      <p v-else-if="nextBuild" class="text-sm text-muted" data-testid="reset-same">
        {{ t('vm.reset.same', { image: props.vm.image ?? '', build: nextBuild }) }}
      </p>
      <p v-if="props.vm.agents && (props.vm.agents.working ?? 0) > 0" class="text-sm text-warning" data-testid="reset-agents">
        {{ t('vm.reset.agentsWorking', props.vm.agents.working ?? 0) }}
      </p>
      <UCheckbox v-model="resetStart" :label="t('vm.reset.start')" data-testid="reset-start" />
    </VmConfirmDialog>

    <VmDeleteDialog v-model:open="deleteOpen" :vm="props.vm" />
  </div>
</template>
