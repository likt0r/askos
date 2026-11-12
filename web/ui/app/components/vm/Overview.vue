<script setup lang="ts">
// A VM at a glance: state, memory (its limit can be changed), Container image cleanup,
// agents, connected devices, its VM variables (changed in VmEnvDialog),
// System disk (age, Build, Reset due and why an automatic Reset was postponed), Image and
// Build (pin, what the next Reset brings) with the versions of the tools in the VM (VmVersions,
// from GET /images). Everything else comes live from the `vm` and `host` events.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t, te, locale } = useI18n()
const live = useLive()

const imageOpen = ref(false)
const memoryOpen = ref(false)
const cleanupOpen = ref(false)
const envOpen = ref(false)
const env = computed(() => Object.entries(props.vm.env ?? {}).filter(([, v]) => typeof v === 'string'))
const refused = computed(() => new Set(Array.isArray(props.vm.env_refused) ? props.vm.env_refused : []))

const used = computed(() => memoryInUse(live.state.host, props.vm.name))
const limit = computed(() => (typeof props.vm.memory_mib === 'number' ? mibAsGib(props.vm.memory_mib) : null))
const cleanup = computed(() => containerImageCleanup(props.vm))

const agents = computed(() => {
  const counts = agentCounts(props.vm)
  if (counts === null) return null
  return counts.map(c => (te(`vms.agents.${c.key}`) ? t(`vms.agents.${c.key}`, { n: c.n }) : `${c.n} ${c.key}`))
})

const disk = computed(() => props.vm.system_disk ?? null)
const diskSince = computed(() => formatTime(disk.value?.since, locale.value))

// why the last automatic Reset did not happen (Activity), while one is due
const postponed = ref<string | null>(null)
async function loadPostponed() {
  postponed.value = null
  if (!props.vm.reset_due) return
  const name = props.vm.name
  try {
    const r = await api<{ entries?: { action?: string, reason?: string }[] }>(
      `/activity?kind=reset&vm=${encodeURIComponent(name)}&since=-14d&lines=1`)
    const last = r?.entries?.[0]
    if (name === props.vm.name && last?.action === 'reset.postponed') postponed.value = last.reason ?? 'unknown'
  }
  catch {
    // Activity not readable: the Reset due mark stands on its own
  }
}
watch(() => [props.vm.name, props.vm.reset_due, disk.value?.since], loadPostponed, { immediate: true })
const postponedText = computed(() => {
  const r = postponed.value
  if (!r) return null
  return t('vm.overview.resetPostponed', { reason: te(`vm.overview.reasons.${r}`) ? t(`vm.overview.reasons.${r}`) : r })
})
</script>

<template>
  <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" data-testid="vm-overview">
    <!-- state and memory -->
    <section class="rounded-lg border border-default bg-default p-4 space-y-2" data-testid="overview-state">
      <h3 class="text-xs font-medium uppercase tracking-wide text-dimmed">
        {{ t('vm.overview.state') }}
      </h3>
      <div class="flex flex-wrap items-center gap-2">
        <VmStateBadge :state="props.vm.state" />
        <span v-if="props.vm.address" class="font-mono text-xs text-muted">{{ props.vm.address }}</span>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <p class="text-sm" data-testid="overview-memory">
          <span class="text-muted">{{ t('vm.overview.memory') }}:</span>
          <template v-if="used !== null && limit !== null">
            {{ t('vm.overview.memoryUsed', { used: gib(used), limit }) }}
          </template>
          <template v-else-if="limit !== null">
            {{ t('vm.overview.memoryLimit', { limit }) }}
          </template>
          <template v-else>
            —
          </template>
        </p>
        <UButton
          size="xs"
          color="neutral"
          variant="outline"
          icon="i-lucide-memory-stick"
          :label="t('vm.memory.change')"
          :aria-label="t('vm.memory.title', { name: props.vm.name })"
          data-testid="memory-change"
          @click="memoryOpen = true"
        />
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <p class="text-sm" data-testid="overview-container-image-cleanup">
          <span class="text-muted">{{ t('vm.overview.containerImageCleanup') }}:</span>
          {{ cleanup.enabled ? t('vm.overview.containerImageCleanupAfter', { days: cleanup.days }) : t('vm.overview.containerImageCleanupOff') }}
        </p>
        <UButton
          size="xs"
          color="neutral"
          variant="outline"
          icon="i-lucide-images"
          :label="t('vm.containerImageCleanup.change')"
          :aria-label="t('vm.containerImageCleanup.title', { name: props.vm.name })"
          data-testid="container-image-cleanup-change"
          @click="cleanupOpen = true"
        />
      </div>
    </section>

    <!-- agents and devices -->
    <section class="rounded-lg border border-default bg-default p-4 space-y-2">
      <h3 class="text-xs font-medium uppercase tracking-wide text-dimmed">
        {{ t('vm.overview.agents') }}
      </h3>
      <p class="text-sm" data-testid="overview-agents">
        <template v-if="agents === null">
          {{ t('vms.agents.unknown') }}
        </template>
        <template v-else-if="!agents.length">
          {{ t('vms.agents.none') }}
        </template>
        <template v-else>
          {{ agents.join(' · ') }}
        </template>
      </p>
      <h3 class="pt-1 text-xs font-medium uppercase tracking-wide text-dimmed">
        {{ t('vm.overview.devices') }}
      </h3>
      <div class="text-sm" data-testid="overview-devices">
        <span v-if="props.vm.devices == null" class="text-muted">{{ t('vms.unknown') }}</span>
        <span v-else-if="!props.vm.devices.length" class="text-muted">{{ t('vm.overview.noDevices') }}</span>
        <ul v-else class="space-y-0.5">
          <li v-for="d in props.vm.devices" :key="d.address" class="flex flex-wrap items-baseline gap-x-2">
            <span class="font-mono text-xs">{{ d.address }}</span>
            <span class="text-xs text-muted">{{ t('vm.overview.connections', d.connections) }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- System disk -->
    <section class="rounded-lg border border-default bg-default p-4 space-y-2" data-testid="overview-disk">
      <h3 class="text-xs font-medium uppercase tracking-wide text-dimmed">
        {{ t('vm.overview.systemDisk') }}
      </h3>
      <div class="flex flex-wrap items-center gap-2 text-sm">
        <span v-if="typeof disk?.age_days === 'number'">{{ t('vms.diskAge', disk.age_days) }}</span>
        <span v-else class="text-muted">{{ t('vm.overview.noDisk') }}</span>
        <UBadge v-if="props.vm.reset_due" color="warning" variant="soft" size="sm" :label="t('vms.resetDue')" data-testid="overview-reset-due" />
      </div>
      <p v-if="diskSince" class="text-xs text-muted">
        {{ t('vm.overview.diskSince', { date: diskSince }) }}
      </p>
      <p v-if="disk?.build" class="text-xs text-muted">
        {{ t('vm.overview.fromBuild', { image: disk.image ?? props.vm.image ?? '', build: disk.build }) }}
      </p>
      <p v-if="postponedText" class="text-xs text-warning" data-testid="overview-postponed">
        {{ postponedText }}
      </p>
    </section>

    <!-- Image and Build -->
    <section class="rounded-lg border border-default bg-default p-4 space-y-2 sm:col-span-2 xl:col-span-3" data-testid="overview-image">
      <div class="flex items-center justify-between gap-2">
        <h3 class="text-xs font-medium uppercase tracking-wide text-dimmed">
          {{ t('vm.overview.image') }}
        </h3>
        <UButton size="xs" color="neutral" variant="outline" icon="i-lucide-package" :label="t('vm.image.change')" data-testid="image-change" @click="imageOpen = true" />
      </div>
      <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt class="text-muted">
          {{ t('vm.overview.imageName') }}
        </dt>
        <dd class="font-mono">
          {{ props.vm.image ?? '—' }}
        </dd>
        <dt class="text-muted">
          {{ t('vm.overview.currentBuild') }}
        </dt>
        <dd class="font-mono">
          {{ props.vm.image_build ?? t('vm.overview.noBuild') }}
        </dd>
        <dt class="text-muted">
          {{ t('vm.overview.pin') }}
        </dt>
        <dd data-testid="overview-pin">
          <span v-if="props.vm.build_pin" class="font-mono">{{ props.vm.build_pin }}</span>
          <span v-else class="text-muted">{{ t('vm.overview.noPin') }}</span>
        </dd>
        <dt class="text-muted">
          {{ t('vm.overview.nextReset') }}
        </dt>
        <dd class="flex flex-wrap items-center gap-2" data-testid="overview-next-reset">
          <span class="font-mono">{{ props.vm.next_build ?? '—' }}</span>
          <UBadge
            v-if="props.vm.reset_changes_build"
            color="info"
            variant="soft"
            size="sm"
            :label="t('vm.overview.resetChanges')"
            data-testid="overview-reset-changes"
          />
          <span v-else-if="props.vm.next_build" class="text-xs text-muted">{{ t('vm.overview.resetSame') }}</span>
        </dd>
      </dl>
      <VmVersions :vm="props.vm" />
    </section>

    <!-- VM variables -->
    <section class="rounded-lg border border-default bg-default p-4 space-y-2 sm:col-span-2 xl:col-span-3" data-testid="overview-env">
      <div class="flex items-center justify-between gap-2">
        <h3 class="text-xs font-medium uppercase tracking-wide text-dimmed">
          {{ t('vm.env.heading') }}
        </h3>
        <UButton
          size="xs"
          color="neutral"
          variant="outline"
          icon="i-lucide-variable"
          :label="t('vm.env.change')"
          :aria-label="t('vm.env.title', { name: props.vm.name })"
          data-testid="env-change"
          @click="envOpen = true"
        />
      </div>
      <p v-if="!env.length" class="text-sm text-muted" data-testid="overview-env-none">
        {{ t('vm.env.none') }}
      </p>
      <dl v-else class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm" data-testid="overview-env-list">
        <template v-for="[name, value] in env" :key="name">
          <dt class="font-mono text-xs leading-5">
            {{ name }}
          </dt>
          <dd class="font-mono text-xs leading-5 break-all">
            {{ value }}
            <span v-if="refused.has(name)" class="font-sans text-warning" :data-testid="`overview-env-refused-${name}`">
              {{ t('vm.env.refused') }}
            </span>
          </dd>
        </template>
      </dl>
    </section>

    <!-- the rest of the record -->
    <section class="rounded-lg border border-default bg-default p-4 sm:col-span-2 xl:col-span-3">
      <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr_auto_1fr]">
        <dt class="text-muted">
          {{ t('vm.overview.dataDisk') }}
        </dt>
        <dd>{{ typeof props.vm.data_disk_gib === 'number' ? t('vm.overview.gib', { n: props.vm.data_disk_gib }) : '—' }}</dd>
        <dt class="text-muted">
          {{ t('vm.overview.network') }}
        </dt>
        <dd class="font-mono text-xs leading-5">
          {{ props.vm.network ?? '—' }} · {{ t('vm.overview.port', { port: props.vm.orca_port ?? '—' }) }}
        </dd>
        <dt class="text-muted">
          {{ t('vm.overview.owners') }}
        </dt>
        <dd class="break-words">
          {{ props.vm.github_owners?.length ? props.vm.github_owners.join(', ') : t('vm.overview.noOwners') }}
        </dd>
        <dt class="text-muted">
          {{ t('vm.overview.created') }}
        </dt>
        <dd>{{ formatTime(props.vm.created as string | undefined, locale) ?? '—' }}</dd>
      </dl>
    </section>

    <VmImageDialog v-model:open="imageOpen" :vm="props.vm" />
    <VmMemoryDialog v-model:open="memoryOpen" :vm="props.vm" />
    <VmContainerImageCleanupDialog v-model:open="cleanupOpen" :vm="props.vm" />
    <VmEnvDialog v-model:open="envOpen" :vm="props.vm" />
  </div>
</template>
