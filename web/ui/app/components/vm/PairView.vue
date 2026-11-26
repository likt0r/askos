<script setup lang="ts">
// A VM's Paired devices (#19): when each was paired and last seen, the Observer (the
// Host itself) marked and never removable. Orca cannot remove a single device for Agent
// Box, so the way to shut a device out is to revoke all pairings (typed confirmation).
// The Pairing code is revealed only after an explicit confirmation, kept only in this
// component's memory, and hidden again after a while or when leaving.
const props = defineProps<{ name: string, hideCodeAfterMs?: number }>()
const { t, locale } = useI18n()
const errorText = useErrorText()
const toast = useToast()

interface Device {
  id: string
  name: string
  scope: 'mobile' | 'runtime' | string
  paired_at: string | null
  last_seen_at: string | null
  observer: boolean
  removable: boolean
}
interface Pairings { vm: string, devices: Device[], observer_known: boolean, single_removal: boolean }

const data = ref<Pairings | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)

async function load() {
  loading.value = true
  loadError.value = null
  const name = props.name
  try {
    const r = await api<Pairings>(`/vms/${name}/pairings`)
    if (name === props.name) data.value = { ...r, devices: Array.isArray(r?.devices) ? r.devices : [] }
  }
  catch (e) {
    if (name === props.name) {
      data.value = null
      loadError.value = errorText(e)
    }
  }
  finally {
    loading.value = false
  }
}

// --- the Pairing code ----------------------------------------------------------

const revealOpen = ref(false)
const revealChange = useChange('revealPairingCode', { form: true })
const revealing = revealChange.busy
// an answer without a code: Orca gave none
const noCode = ref<Error | null>(null)
const revealFailure = computed(() => revealChange.shown.value ?? noCode.value)
const code = ref<string | null>(null)
let hideTimer: ReturnType<typeof setTimeout> | null = null

function hideCode() {
  code.value = null
  if (hideTimer) clearTimeout(hideTimer)
  hideTimer = null
}

async function reveal() {
  noCode.value = null
  const r = await revealChange.run(props.name)
  if (!r.ok) return
  const answer = (r.data as { code?: string } | null)?.code
  if (typeof answer !== 'string' || !answer) {
    noCode.value = new Error('no code')
    return
  }
  revealOpen.value = false
  code.value = answer
  if (hideTimer) clearTimeout(hideTimer)
  hideTimer = setTimeout(hideCode, props.hideCodeAfterMs ?? 120000)
}

async function copyCode() {
  if (!code.value) return
  try {
    await navigator.clipboard.writeText(code.value)
    toast.add({ title: t('vm.pair.copied'), color: 'success', icon: 'i-lucide-clipboard-check' })
  }
  catch {
    toast.add({ title: t('vm.pair.copyFailed'), color: 'warning', icon: 'i-lucide-clipboard-x' })
  }
}

function openReveal() {
  revealChange.reset()
  noCode.value = null
  revealOpen.value = true
}

// --- revoking ------------------------------------------------------------------

const revokeOpen = ref(false)
const revokeChange = useChange('revokePairings', { form: true })

function openRevoke() {
  revokeChange.reset()
  revokeOpen.value = true
}

async function revoke() {
  const r = await revokeChange.run(props.name)
  if (!r.ok) return
  revokeOpen.value = false
  hideCode()           // a code of the old identity is no use any more
  toast.add({
    title: t('vm.pair.revoked', { name: props.name }),
    description: (r.data as { observer_repaired?: boolean | null } | null)?.observer_repaired === true ? t('vm.pair.observerRepaired') : undefined,
    color: 'success',
    icon: 'i-lucide-check',
  })
}

// --- removing one device (only if Orca ever supports it) -------------------------

const removeTarget = ref<Device | null>(null)
const removeOpen = computed({
  get: () => removeTarget.value !== null,
  set: (v: boolean) => {
    if (!v) removeTarget.value = null
  },
})
const removeChange = useChange('removePairing', { form: true })

function openRemove(d: Device) {
  removeChange.reset()
  removeTarget.value = d
}

async function removeDevice() {
  const d = removeTarget.value
  if (!d || !(await removeChange.run({ vm: props.name, device: d.id })).ok) return
  removeTarget.value = null
}

const canRemove = (d: Device) => data.value?.single_removal === true && d.removable && !d.observer

const time = (iso: string | null) => formatTime(iso, locale.value)

onMounted(load)
watch(() => props.name, () => {
  hideCode()
  data.value = null
  void load()
})
onBeforeUnmount(hideCode)
// the revealed code and its timer are not touched by a reload
useReloadOnActivity(load, { codes: ['observe', 'unpair', 'revoke-pairings'], match: entry => entry.vm === props.name })
</script>

<template>
  <div class="space-y-4" data-testid="pair-view">
    <!-- Pairing code -->
    <section class="rounded-lg border border-default bg-default p-4 space-y-3">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0 space-y-1">
          <h3 class="font-medium text-highlighted">
            {{ t('vm.pair.codeTitle') }}
          </h3>
          <p class="text-sm text-muted">
            {{ t('vm.pair.codeText') }}
          </p>
        </div>
        <UButton
          v-if="!code"
          icon="i-lucide-eye"
          :label="t('vm.pair.reveal')"
          color="neutral"
          variant="outline"
          data-testid="pair-reveal"
          @click="openReveal"
        />
      </div>
      <div v-if="code" class="space-y-2" data-testid="pair-code-box">
        <code class="block break-all rounded-md bg-elevated px-3 py-2 font-mono text-sm text-highlighted select-all" data-testid="pair-code">{{ code }}</code>
        <div class="flex flex-wrap items-center gap-2">
          <UButton icon="i-lucide-copy" size="sm" :label="t('vm.pair.copy')" data-testid="pair-copy" @click="copyCode" />
          <UButton icon="i-lucide-eye-off" size="sm" color="neutral" variant="outline" :label="t('vm.pair.hide')" data-testid="pair-hide" @click="hideCode" />
          <span class="text-xs text-muted">{{ t('vm.pair.hidesItself') }}</span>
        </div>
      </div>
    </section>

    <!-- Paired devices -->
    <section class="rounded-lg border border-default bg-default">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-default px-4 py-3">
        <h3 class="font-medium text-highlighted">
          {{ t('vm.pair.devicesTitle') }}
        </h3>
        <UButton icon="i-lucide-refresh-cw" size="xs" color="neutral" variant="ghost" :label="t('vm.pair.refresh')" :loading="loading" data-testid="pair-refresh" @click="load" />
      </div>
      <div class="p-4 space-y-3">
        <p v-if="loading && !data" class="text-sm text-muted">
          {{ t('vm.pair.loading') }}
        </p>
        <UAlert v-if="loadError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="loadError" data-testid="pair-error" />
        <p v-if="data && !data.devices.length" class="text-sm text-muted" data-testid="pair-empty">
          {{ t('vm.pair.none') }}
        </p>
        <ul v-if="data?.devices.length" class="divide-y divide-default" data-testid="pair-devices">
          <li
            v-for="d in data.devices"
            :key="d.id"
            class="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 py-2.5"
            data-testid="pair-device"
            :data-device="d.id"
          >
            <div class="min-w-0 space-y-0.5">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-medium text-highlighted break-all">{{ d.name || t('vm.pair.unnamed') }}</span>
                <UBadge v-if="d.observer" color="info" variant="soft" size="sm" :label="t('vm.pair.observer')" data-testid="pair-observer" />
                <UBadge color="neutral" variant="outline" size="sm" :label="d.scope === 'runtime' ? t('vm.pair.scopeRuntime') : t('vm.pair.scopeMobile')" />
              </div>
              <p class="font-mono text-xs text-dimmed break-all">
                {{ d.id }}
              </p>
              <p class="text-xs text-muted">
                {{ t('vm.pair.pairedAt', { time: time(d.paired_at) ?? '?' }) }} ·
                {{ d.last_seen_at ? t('vm.pair.lastSeen', { time: time(d.last_seen_at) }) : t('vm.pair.neverSeen') }}
              </p>
              <p v-if="d.observer" class="text-xs text-muted">
                {{ t('vm.pair.observerText') }}
              </p>
            </div>
            <UButton
              v-if="canRemove(d)"
              icon="i-lucide-unlink"
              size="xs"
              color="error"
              variant="ghost"
              :label="t('vm.pair.remove')"
              data-testid="pair-remove"
              @click="openRemove(d)"
            />
          </li>
        </ul>
        <p v-if="data && !data.observer_known" class="text-xs text-muted" data-testid="pair-no-observer">
          {{ t('vm.pair.noObserver') }}
        </p>
        <UAlert
          v-if="data && !data.single_removal"
          color="neutral"
          variant="subtle"
          icon="i-lucide-info"
          :title="t('vm.pair.singleTitle')"
          :description="t('vm.pair.singleText')"
          data-testid="pair-single-note"
        />
      </div>
    </section>

    <!-- Revoke all -->
    <section class="rounded-lg border border-error/40 bg-default p-4 space-y-3">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0 space-y-1">
          <h3 class="font-medium text-highlighted">
            {{ t('vm.pair.revokeTitle') }}
          </h3>
          <p class="text-sm text-muted">
            {{ t('vm.pair.revokeText') }}
          </p>
        </div>
        <UButton icon="i-lucide-shield-off" color="error" variant="outline" :label="t('vm.pair.revoke')" data-testid="pair-revoke" @click="openRevoke" />
      </div>
    </section>

    <VmConfirmDialog
      v-model:open="revealOpen"
      :title="t('vm.pair.revealTitle', { name: props.name })"
      :description="t('vm.pair.revealText')"
      :confirm-label="t('vm.pair.revealConfirm')"
      color="warning"
      :loading="revealing"
      :failure="revealFailure"
      testid="reveal-dialog"
      @confirm="reveal"
    >
      <p v-if="revealing" class="text-sm text-muted">
        {{ t('vm.pair.revealWaiting') }}
      </p>
    </VmConfirmDialog>

    <VmConfirmDialog
      v-model:open="revokeOpen"
      :title="t('vm.pair.revokeDialogTitle', { name: props.name })"
      :description="t('vm.pair.revokeDialogText')"
      :confirm-label="t('vm.pair.revoke')"
      :typed="props.name"
      :loading="revokeChange.busy.value"
      :failure="revokeChange.shown.value"
      testid="revoke-dialog"
      @confirm="revoke"
    />

    <VmConfirmDialog
      v-model:open="removeOpen"
      :title="t('vm.pair.removeTitle', { device: removeTarget?.name || removeTarget?.id || '' })"
      :description="t('vm.pair.removeText')"
      :confirm-label="t('vm.pair.remove')"
      :loading="removeChange.busy.value"
      :failure="removeChange.shown.value"
      testid="remove-dialog"
      @confirm="removeDevice"
    />
  </div>
</template>
