<script setup lang="ts">
// The versions of the tools in a VM (#104): those of the Build its System disk was made from,
// next to what is available, from the resource `images`. An extra read: unknown while it fails.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()

const images = useResource('images', { extra: true })
const overview = images.data

const diskBuild = computed(() => props.vm.system_disk?.build ?? null)
const diskImage = computed(() => props.vm.system_disk?.image ?? props.vm.image ?? null)
const image = computed(() => overview.value?.images?.find(i => i.name === diskImage.value) ?? null)
const build = computed(() => image.value?.builds?.find(b => b.build === diskBuild.value) ?? null)
const rows = computed(() => toolRows(build.value, overview.value?.available))
const digests = computed(() => digestRows(build.value, overview.value?.available))
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
    <ReadFailure :resource="images" />
    <p v-if="!diskBuild" class="text-sm text-muted">
      {{ t('vm.overview.versionsNoDisk') }}
    </p>
    <template v-else>
      <SystemToolVersions
        :rows="rows"
        :digests="digests"
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
