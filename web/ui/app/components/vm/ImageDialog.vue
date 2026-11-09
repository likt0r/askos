<script setup lang="ts">
// The VM's Image, at its current Build or pinned to one (going back to an earlier
// Build). Like `agentbox set-image`, it applies at the next Reset, never in the middle
// of work.
import type { Image } from '~/utils/images'
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Pick<Vm, 'name' | 'image' | 'build_pin'> }>()
const open = defineModel<boolean>('open', { default: false })
const { t, locale } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const CURRENT = '__current__'      // a radio value cannot be empty
const images = ref<Image[] | null>(null)
const loadError = ref<string | null>(null)
const error = ref<string | null>(null)
const saving = ref(false)
const image = ref('')
const build = ref(CURRENT)

async function load() {
  images.value = null
  loadError.value = null
  try {
    const r = await api<{ images?: Image[] }>('/images')
    images.value = Array.isArray(r?.images) ? r.images : []
  }
  catch (e) {
    loadError.value = errorText(e)
  }
}

watch(open, (value) => {
  if (!value) return
  image.value = props.vm.image ?? ''
  build.value = props.vm.build_pin ?? CURRENT
  error.value = null
  void load()
})

const selected = computed(() => images.value?.find(i => i.name === image.value))
const imageItems = computed(() => (images.value ?? [])
  .filter(i => i.current || i.name === props.vm.image)
  .map(i => ({
    value: i.name,
    label: i.name,
    description: [i.base ? t('vm.image.base') : null, i.current ? t('vm.image.current', { build: i.current }) : t('vm.image.noBuild')]
      .filter(Boolean).join(' · '),
  })))

function agentsText(agents: unknown): string | null {
  if (!agents || typeof agents !== 'object') return null
  const parts = Object.entries(agents as Record<string, unknown>)
    .filter(([, v]) => typeof v === 'string' && v)
    .map(([k, v]) => `${k} ${v}`)
  return parts.length ? parts.join(' · ') : null
}

const buildItems = computed(() => {
  const img = selected.value
  const items = [{
    value: CURRENT,
    label: t('vm.image.followCurrent'),
    description: img?.current ? t('vm.image.followCurrentNow', { build: img.current }) : t('vm.image.noBuild'),
  }]
  for (const b of pinnableBuilds(img)) {
    const when = formatTime(b.started ?? null, locale.value)
    items.push({
      value: b.build,
      label: t('vm.image.pinTo', { build: b.build }) + (b.current ? ` (${t('vm.image.isCurrent')})` : ''),
      description: [when, agentsText(b.agents)].filter(Boolean).join(' · '),
    })
  }
  return items
})

function chooseImage(value: unknown) {
  if (typeof value !== 'string' || value === image.value) return
  image.value = value
  build.value = CURRENT          // a pin belongs to its Image
}

const unchanged = computed(() => image.value === (props.vm.image ?? '') && (build.value === CURRENT ? null : build.value) === (props.vm.build_pin ?? null))

async function save() {
  if (!image.value || unchanged.value) return
  saving.value = true
  error.value = null
  try {
    await api(`/vms/${props.vm.name}/image`, {
      method: 'PUT',
      body: { image: image.value, build: build.value === CURRENT ? null : build.value },
    })
    open.value = false
    toast.add({ title: t('vm.image.saved', { name: props.vm.name }), description: t('vm.image.atNextReset'), color: 'success', icon: 'i-lucide-check' })
  }
  catch (e) {
    error.value = errorText(e)
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <UModal v-model:open="open" :title="t('vm.image.title', { name: props.vm.name })" :description="t('vm.image.text')">
    <template #body>
      <form class="space-y-5" data-testid="image-dialog" @submit.prevent="save">
        <p v-if="!images && !loadError" class="text-sm text-muted">
          {{ t('vm.image.loading') }}
        </p>
        <UAlert v-if="loadError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="loadError" />
        <template v-if="images">
          <URadioGroup
            :model-value="image"
            @update:model-value="chooseImage"
            :legend="t('vm.image.image')"
            :items="imageItems"
            data-testid="image-choice"
          />
          <URadioGroup
            v-if="selected"
            v-model="build"
            :legend="t('vm.image.build')"
            :items="buildItems"
            data-testid="build-choice"
          />
          <UAlert color="info" variant="subtle" icon="i-lucide-info" :title="t('vm.image.atNextReset')" />
        </template>
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="image-error" />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton color="neutral" variant="ghost" :label="t('vm.confirm.cancel')" @click="open = false" />
          <UButton type="submit" :label="t('vm.image.save')" :loading="saving" :disabled="!images || unchanged" data-testid="image-save" />
        </div>
      </form>
    </template>
  </UModal>
</template>
