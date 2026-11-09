<script setup lang="ts">
// One Image: its current Build, who uses it, its definition (the Containerfile, #67), the versions
// of its tools next to what is available (marked when a new Build would bring something), the Update (a Build, then a Reset of
// the VMs picked) and rebuild buttons with live progress, and its Builds with status, reasons of a rejected or failed one, and which VMs use
// or are pinned to each.
import type { Available, Build, BuildLog, Image } from '~/utils/images'

const props = defineProps<{
  image: Image
  available?: Available | null
  logs: Record<string, BuildLog>
  logLoading: Record<string, boolean>
  logErrors: Record<string, unknown>
  busy: boolean
  starting: boolean
}>()
const emit = defineEmits<{ rebuild: [], update: [], log: [build: string] }>()
const { t, te, locale } = useI18n()

const STATUS_COLOR: Record<string, 'success' | 'info' | 'error' | 'warning' | 'neutral'> = {
  ok: 'success', running: 'info', failed: 'error', rejected: 'error', migrated: 'neutral', missing: 'warning',
}

const open = ref(new Set<string>())
const running = computed(() => props.image.running)
const currentBuild = computed(() => props.image.builds.find(b => b.build === props.image.current) ?? null)
const tools = computed(() => toolRows(currentBuild.value, props.available))

function toggle(build: string) {
  const next = new Set(open.value)
  if (next.has(build)) next.delete(build)
  else {
    next.add(build)
    emit('log', build)
  }
  open.value = next
}

// a running Build shows its log without being asked
watch(() => running.value?.build, (build) => {
  if (build && !open.value.has(build)) {
    open.value = new Set([...open.value, build])
    emit('log', build)
  }
}, { immediate: true })

function status(b: Build) {
  return te(`system.build.status.${b.status}`) ? t(`system.build.status.${b.status}`) : b.status
}

function agentsText(b: Build) {
  return Object.entries(b.agents ?? {}).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(' · ')
}

function stepLabel(step: string) {
  return te(`system.build.step.${step}`) ? t(`system.build.step.${step}`) : step
}

const stepsDone = computed(() => stepIndex(running.value?.step))
const log = (b: string) => props.logs[logKey(props.image.name, b)]
</script>

<template>
  <section class="rounded-lg border border-default bg-default" data-testid="image-card" :data-image="image.name">
    <header class="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-default px-4 py-3">
      <h3 class="font-mono text-base font-semibold text-highlighted">
        {{ image.name }}
      </h3>
      <UBadge v-if="image.base" color="primary" variant="subtle" size="sm" :label="t('system.image.base')" />
      <UBadge v-if="image.in_release === false" color="warning" variant="subtle" size="sm" :label="t('system.image.notInRelease')" />
      <UBadge v-if="image.update" color="warning" variant="soft" size="sm" icon="i-lucide-circle-arrow-up" :label="t('system.versions.updateAvailable')" data-testid="image-update" />
      <span class="text-sm text-muted">
        {{ t('system.image.current') }}:
        <span class="font-mono text-highlighted" data-testid="image-current">{{ image.current ?? t('system.image.noBuild') }}</span>
      </span>
      <span class="flex-1" />
      <UButton
        v-if="image.current"
        size="sm"
        :color="image.update ? 'primary' : 'neutral'"
        :variant="image.update ? 'solid' : 'outline'"
        icon="i-lucide-package"
        :label="t('system.update.open')"
        :disabled="busy || !!running"
        data-testid="image-update-open"
        @click="emit('update')"
      />
      <UButton
        size="sm"
        color="neutral"
        variant="outline"
        icon="i-lucide-hammer"
        :label="t('system.image.rebuild')"
        :loading="starting"
        :disabled="busy || !!running"
        data-testid="image-rebuild"
        @click="emit('rebuild')"
      />
    </header>

    <p class="px-4 pt-3 text-sm text-muted" data-testid="image-vms">
      <template v-if="image.vms.length">
        {{ t('system.image.usedBy') }}:
        <span v-for="(vm, i) in image.vms" :key="vm">
          <NuxtLink :to="`/vms/${vm}`" class="font-medium text-highlighted hover:underline">{{ vm }}</NuxtLink><template v-if="i < image.vms.length - 1">, </template>
        </span>
      </template>
      <template v-else>
        {{ t('system.image.unused') }}
      </template>
    </p>

    <SystemImageDefinition v-if="image.containerfile" :image="image" :build="currentBuild" />

    <div v-if="currentBuild" class="px-4 pt-3" data-testid="image-versions">
      <SystemToolVersions
        :rows="tools"
        :available="available"
        :installed-label="t('system.versions.inBuild', { build: currentBuild.build })"
        :packages="image.package_updates ?? null"
      />
    </div>

    <div v-if="running" class="mx-4 mt-3 rounded-md border border-info/40 bg-info/5 p-3" data-testid="build-progress" :data-step="running.step ?? ''">
      <p class="flex items-center gap-2 text-sm font-medium text-info">
        <UIcon name="i-lucide-loader-circle" class="size-4 animate-spin" />
        {{ t('system.build.running', { build: running.build }) }}
      </p>
      <ol class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <li
          v-for="(step, i) in BUILD_STEPS"
          :key="step"
          class="flex items-center gap-1"
          :class="i < stepsDone ? 'text-success' : i === stepsDone ? 'font-semibold text-info' : 'text-dimmed'"
          :aria-current="i === stepsDone ? 'step' : undefined"
        >
          <UIcon :name="i < stepsDone ? 'i-lucide-check' : i === stepsDone ? 'i-lucide-circle-dot' : 'i-lucide-circle'" class="size-3.5" />
          {{ stepLabel(step) }}
        </li>
      </ol>
    </div>

    <p v-if="!image.builds.length" class="px-4 py-4 text-sm text-muted">
      {{ t('system.image.noBuilds') }}
    </p>
    <ul v-else class="mt-2 divide-y divide-default" data-testid="build-list">
      <li
        v-for="b in image.builds"
        :key="b.build"
        class="flex flex-col gap-1.5 px-4 py-3 text-sm"
        data-testid="build-row"
        :data-build="b.build"
        :data-status="b.status"
      >
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span class="font-mono font-medium text-highlighted">{{ b.build }}</span>
          <UBadge :color="STATUS_COLOR[b.status] ?? 'neutral'" variant="subtle" size="sm" :label="status(b)" data-testid="build-status" />
          <UBadge v-if="b.current" color="primary" variant="outline" size="sm" :label="t('system.build.current')" />
          <span v-if="b.started" class="text-muted">{{ formatWhen(b.started, locale, false) }}</span>
          <span v-if="b.release" class="text-xs text-muted" data-testid="build-release">{{ t('system.build.release', { release: b.release }) }}</span>
          <span v-else-if="b.commit" class="font-mono text-xs text-muted" :title="b.commit" data-testid="build-commit">{{ b.commit.slice(0, 12) }}</span>
          <span v-if="b.actor" class="text-xs text-dimmed">{{ t('system.build.by', { actor: b.actor }) }}</span>
          <span class="flex-1" />
          <UButton
            size="xs"
            color="neutral"
            variant="ghost"
            :icon="open.has(b.build) ? 'i-lucide-chevron-up' : 'i-lucide-scroll-text'"
            :label="open.has(b.build) ? t('system.build.hideLog') : t('system.build.showLog')"
            :aria-expanded="open.has(b.build)"
            data-testid="build-log-toggle"
            @click="toggle(b.build)"
          />
        </div>
        <p v-if="agentsText(b)" class="text-xs text-muted" data-testid="build-agents">
          {{ agentsText(b) }}
        </p>
        <p v-if="b.used_by?.length || b.pinned_by?.length" class="text-xs text-muted" data-testid="build-users">
          <span v-if="b.used_by?.length">{{ t('system.build.usedBy', { vms: b.used_by.join(', ') }) }}</span>
          <span v-if="b.used_by?.length && b.pinned_by?.length"> · </span>
          <span v-if="b.pinned_by?.length">{{ t('system.build.pinnedBy', { vms: b.pinned_by.join(', ') }) }}</span>
        </p>
        <div v-if="buildBad(b)" class="rounded-md border border-error/40 bg-error/5 p-2 text-xs text-error" data-testid="build-reasons">
          <p class="font-medium">
            {{ b.status === 'rejected' ? t('system.build.rejectedHint') : t('system.build.failedHint') }}
          </p>
          <ul v-if="b.problems?.length" class="mt-1 list-disc ps-4">
            <li v-for="p in b.problems" :key="p">
              {{ p }}
            </li>
          </ul>
          <p v-else-if="b.error" class="mt-1">
            {{ b.error }}
          </p>
        </div>
        <SystemBuildLog
          v-if="open.has(b.build)"
          :lines="log(b.build)?.lines ?? []"
          :loading="logLoading[logKey(image.name, b.build)]"
          :error="logErrors[logKey(image.name, b.build)]"
          :running="b.status === 'running'"
        />
      </li>
    </ul>
  </section>
</template>
