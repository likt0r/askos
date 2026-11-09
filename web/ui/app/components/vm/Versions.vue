<script setup lang="ts">
// The versions of the tools in a VM (#104): those of the Build its System disk was made from,
// next to what is available, from GET /images. Fetched again when the System disk or the
// Image's current Build changes; unknown when GET /images fails.
import type { ImagesOverview } from '~/utils/images'
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()

const overview = ref<ImagesOverview | null>(null)
let token = 0
async function load() {
  const mine = ++token
  try {
    const data = await api<ImagesOverview>('/images')
    if (mine === token) overview.value = data
  }
  catch {
    // not readable now: every version shows as unknown
    if (mine === token) overview.value = null
  }
}
watch(() => [props.vm.name, props.vm.system_disk?.build, props.vm.image_build], load, { immediate: true })

const diskBuild = computed(() => props.vm.system_disk?.build ?? null)
const diskImage = computed(() => props.vm.system_disk?.image ?? props.vm.image ?? null)
const image = computed(() => overview.value?.images?.find(i => i.name === diskImage.value) ?? null)
const build = computed(() => image.value?.builds?.find(b => b.build === diskBuild.value) ?? null)
const rows = computed(() => toolRows(build.value, overview.value?.available))
// package updates are counted for an Image's current Build only
const packages = computed(() => (build.value && build.value.build === image.value?.current
  ? (image.value?.package_updates ?? null)
  : undefined))
const changes = computed(() => (build.value?.updates?.length ?? 0) > 0 || !!packages.value)
</script>

<template>
  <div class="space-y-1 pt-2" data-testid="overview-versions">
    <h4 class="text-xs font-medium uppercase tracking-wide text-dimmed">
      {{ t('vm.overview.versions') }}
    </h4>
    <p v-if="!diskBuild" class="text-sm text-muted">
      {{ t('vm.overview.versionsNoDisk') }}
    </p>
    <template v-else>
      <SystemToolVersions
        :rows="rows"
        :available="overview?.available"
        :installed-label="t('vm.overview.inVm')"
        :packages="packages"
      />
      <p v-if="changes" class="text-xs text-muted" data-testid="overview-versions-hint">
        {{ t('vm.overview.versionsHint', { image: diskImage ?? '' }) }}
      </p>
    </template>
  </div>
</template>
