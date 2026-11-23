<script setup lang="ts">
// Update an Image (#104): a new Build, and when it succeeds a Reset of the VMs picked here onto
// it (POST /builds with `reset`). With `everything`, Update now (#114): the version check at
// once, a Build of every Image with an update, then a Reset of the VMs picked onto the newest
// Build of their Image (POST /updates). Says what a new Build would bring by the last check
// and, before anything starts, what happens: the Builds run first; sessions in the picked VMs
// end, their Data disks stay; a failed Build resets nothing. A VM pinned to a Build cannot be
// picked: its Reset would keep that Build.
import type { Available, Image } from '~/utils/images'

const props = defineProps<{
  /** the Image to update; with `everything`, every Image with a Build */
  images: Image[]
  everything?: boolean
  available?: Available | null
  loading?: boolean
  /** why the last attempt failed (translated) */
  error?: string | null
}>()
const emit = defineEmits<{ confirm: [vms: string[]] }>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()

const picked = ref<string[]>([])
watch(open, (value) => {
  if (value) picked.value = []
})

// the Build each VM is pinned to, from the Builds' pinned_by
const pins = computed(() => {
  const out: Record<string, string> = {}
  for (const image of props.images) {
    for (const b of image.builds) {
      for (const vm of b.pinned_by ?? []) out[vm] = b.build
    }
  }
  return out
})
const choices = computed(() => props.images.flatMap(i => i.vms.map(vm => ({ vm, image: i.name, pin: pins.value[vm] ?? null }))))

function pick(vm: string, on: boolean | 'indeterminate') {
  const rest = picked.value.filter(v => v !== vm)
  picked.value = on === true ? [...rest, vm] : rest
}

// what a new Build of each Image would change, by the daily check
const rows = computed(() => props.images.map((image) => {
  const current = image.builds.find(b => b.build === image.current) ?? null
  return {
    image,
    changes: toolRows(current, props.available).filter(r => r.update),
    packages: image.package_updates ?? null,
  }
}))
const ordered = computed(() => choices.value.map(c => c.vm).filter(vm => picked.value.includes(vm)))

const key = (name: string) => `system.${props.everything ? 'updateNow' : 'update'}.${name}`
const title = computed(() => props.everything ? t('system.updateNow.title') : t('system.update.title', { image: props.images[0]?.name ?? '' }))
const confirmLabel = computed(() => {
  if (props.everything) return ordered.value.length ? t('system.updateNow.confirmReset', ordered.value.length) : t('system.updateNow.confirm')
  return ordered.value.length ? t('system.update.confirmReset', ordered.value.length) : t('system.update.confirmBuild')
})
function unchanged(image: Image) {
  if (props.everything) return image.update === false ? t('system.updateNow.nothing') : t('system.updateNow.unknown')
  return image.update === false ? t('system.update.nothing') : t('system.update.unknown')
}
function vmNote(c: { image: string, pin: string | null }) {
  if (c.pin) return t('system.update.pinned', { build: c.pin })
  return props.everything ? t('system.updateNow.image', { image: c.image }) : undefined
}
</script>

<template>
  <VmConfirmDialog
    v-model:open="open"
    :title="title"
    :description="t(key('text'))"
    :confirm-label="confirmLabel"
    :color="ordered.length ? 'warning' : 'primary'"
    :loading="props.loading"
    :error="props.error"
    :testid="props.everything ? 'update-now-dialog' : 'update-dialog'"
    @confirm="emit('confirm', ordered)"
  >
    <div class="space-y-1 text-sm" data-testid="update-changes">
      <p class="font-medium text-highlighted">
        {{ t(key('brings')) }}
      </p>
      <div v-for="r in rows" :key="r.image.name" :data-image="r.image.name">
        <p v-if="props.everything" class="font-mono text-highlighted">
          {{ r.image.name }}
        </p>
        <ul v-if="r.changes.length || r.packages" class="list-disc ps-5">
          <li v-for="c in r.changes" :key="c.tool">
            {{ t('system.update.change', { tool: c.name, from: c.installed ?? t('system.versions.unknown'), to: c.available ?? t('system.versions.unknown') }) }}
          </li>
          <li v-if="r.packages">
            {{ t('system.versions.packageUpdates', r.packages) }} ({{ t('system.versions.packages') }})
          </li>
        </ul>
        <p v-else class="text-muted">
          {{ unchanged(r.image) }}
        </p>
      </div>
      <p v-if="props.everything" class="text-muted">
        {{ t('system.updateNow.checkFirst') }}
      </p>
    </div>

    <fieldset class="space-y-2">
      <legend class="text-sm font-medium text-highlighted">
        {{ t(key('resetLegend')) }}
      </legend>
      <p v-if="!choices.length" class="text-sm text-muted">
        {{ t(key('noVms')) }}
      </p>
      <UCheckbox
        v-for="c in choices"
        :key="c.vm"
        :model-value="picked.includes(c.vm)"
        :label="c.vm"
        :description="vmNote(c)"
        :disabled="!!c.pin || props.loading"
        data-testid="update-vm"
        :data-vm="c.vm"
        @update:model-value="pick(c.vm, $event)"
      />
    </fieldset>

    <UAlert
      v-if="ordered.length"
      color="warning"
      variant="subtle"
      icon="i-lucide-triangle-alert"
      :title="t(key('resetNotice'), { vms: ordered.join(', ') })"
      :description="t(key('failedNotice'))"
      data-testid="update-notice"
    />
    <UAlert
      v-else
      color="info"
      variant="subtle"
      icon="i-lucide-info"
      :title="t(key('buildOnly'))"
      data-testid="update-notice"
    />
  </VmConfirmDialog>
</template>
