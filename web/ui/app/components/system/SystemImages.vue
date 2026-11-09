<script setup lang="ts">
// Images and Builds: every Image with its Builds and the versions of its tools, update one (a
// Build, then a Reset of the VMs picked), Update now (#114: check, build every Image with an
// update, then reset the VMs picked), rebuild one or all, live progress, and the repository the
// Releases come from (#67).
const { t, locale } = useI18n()
const errorText = useErrorText()
const toast = useToast()
const images = useImages()
const { overview, loading, error, startError, starting, logs, logLoading, logErrors } = images
// an update of every Image runs (Update now, or the automatic one)
const updatingAll = images.updating
const repositoryLink = computed(() => githubLink(overview.value?.repository_url))

const anyRunning = computed(() => (overview.value?.images ?? []).some(i => !!i.running))
// nothing else starts while a Build or an update of every Image runs
const busy = computed(() => !!starting.value || anyRunning.value || updatingAll.value)
const available = computed(() => overview.value?.available ?? null)

// the Image whose Update dialog is open
const updating = ref<string | null>(null)
const updateOpen = computed({
  get: () => updating.value !== null,
  set: (value: boolean) => {
    if (!value) updating.value = null
  },
})
const updateImage = computed(() => overview.value?.images.find(i => i.name === updating.value) ?? null)
const updateError = ref<string | null>(null)

function openUpdate(name: string) {
  updateError.value = null
  startError.value = null
  updating.value = name
}

// Update now: every Image that has a Build, and their VMs
const updateNowOpen = ref(false)
const updateNowError = ref<string | null>(null)
const built = computed(() => (overview.value?.images ?? []).filter(i => !!i.current))
const anyUpdate = computed(() => built.value.some(i => i.update))

function openUpdateNow() {
  updateNowError.value = null
  startError.value = null
  updateNowOpen.value = true
}

async function updateNow(vms: string[]) {
  updateNowError.value = null
  if (await images.startUpdate(vms)) {
    updateNowOpen.value = false
    toast.add({
      title: t('system.updateNow.started'),
      description: vms.length ? t('system.updateNow.startedText', { vms: vms.join(', ') }) : t('system.updateNow.startedBuildOnly'),
      color: 'success',
      icon: 'i-lucide-check',
    })
  }
  else {
    updateNowError.value = errorText(startError.value)
    startError.value = null       // said in the dialog
  }
}

async function update(vms: string[]) {
  const name = updating.value
  if (!name) return
  updateError.value = null
  if (await images.start(name, vms)) {
    updating.value = null
    toast.add({
      title: t('system.update.started', { image: name }),
      description: vms.length ? t('system.update.startedText', { vms: vms.join(', ') }) : undefined,
      color: 'success',
      icon: 'i-lucide-check',
    })
  }
  else {
    updateError.value = errorText(startError.value)
    startError.value = null       // said in the dialog
  }
}
</script>

<template>
  <section class="flex flex-col gap-3" data-testid="system-images" aria-labelledby="system-images-title">
    <div class="flex flex-wrap items-center gap-3">
      <h2 id="system-images-title" class="text-lg font-semibold text-highlighted">
        {{ t('system.images.title') }}
      </h2>
      <span class="flex-1" />
      <UButton
        :color="anyUpdate ? 'primary' : 'neutral'"
        :variant="anyUpdate ? 'solid' : 'outline'"
        size="sm"
        icon="i-lucide-circle-arrow-up"
        :label="t('system.updateNow.open')"
        :loading="starting === 'update' || updatingAll"
        :disabled="busy || !built.length"
        data-testid="images-update-now"
        @click="openUpdateNow"
      />
      <UButton
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-hammer"
        :label="t('system.images.rebuildAll')"
        :loading="starting === 'all'"
        :disabled="busy || !overview?.images.length"
        data-testid="images-rebuild-all"
        @click="images.start('all')"
      />
    </div>

    <UAlert
      v-if="updatingAll"
      color="info"
      variant="subtle"
      icon="i-lucide-loader"
      :title="t('system.updateNow.running')"
      data-testid="images-updating"
    />

    <UAlert
      v-if="startError"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(startError)"
      data-testid="images-start-error"
    />
    <UAlert
      v-if="error"
      color="error"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(error)"
      :actions="[{ label: t('system.retry'), color: 'error', variant: 'outline', onClick: () => images.load() }]"
      data-testid="images-error"
    />
    <p v-else-if="loading && !overview" class="text-sm text-muted">
      {{ t('system.images.loading') }}
    </p>
    <template v-else-if="overview">
      <p class="text-sm text-muted" data-testid="images-source">
        {{ t('system.images.source', { release: overview.release ?? '—', n: overview.builds_kept }) }}
        <template v-if="overview.repository">
          · {{ t('system.images.repository') }}:
          <a
            v-if="repositoryLink"
            :href="repositoryLink"
            target="_blank"
            rel="noopener noreferrer"
            class="font-medium text-highlighted hover:underline"
            data-testid="images-repository"
          >{{ overview.repository }}</a>
          <span v-else class="font-medium text-highlighted" data-testid="images-repository">{{ overview.repository }}</span>
        </template>
      </p>
      <p class="text-sm text-muted" data-testid="images-available">
        <template v-if="available?.checked">
          {{ t('system.versions.checked', { when: formatWhen(available.checked, locale, false), days: available.package_min_age_days ?? '—' }) }}
          <template v-if="available.orca_version">
            {{ t('system.versions.orcaPinned', { version: available.orca_version }) }}
          </template>
        </template>
        <template v-else>
          {{ t('system.versions.notChecked') }}
        </template>
      </p>
      <p v-if="!overview.disks_readable" class="text-sm text-warning">
        {{ t('system.images.disksUnreadable') }}
      </p>
      <p v-if="!overview.images.length" class="rounded-lg border border-dashed border-default p-6 text-center text-sm text-muted">
        {{ t('system.images.none') }}
      </p>
      <SystemImageCard
        v-for="image in overview.images"
        :key="image.name"
        :image="image"
        :available="available"
        :logs="logs"
        :log-loading="logLoading"
        :log-errors="logErrors"
        :busy="busy"
        :starting="starting === image.name"
        @rebuild="images.start(image.name)"
        @update="openUpdate(image.name)"
        @log="images.fetchLog(image.name, $event)"
      />
    </template>

    <SystemUpdateDialog
      v-if="updateImage"
      v-model:open="updateOpen"
      :images="[updateImage]"
      :available="available"
      :loading="starting === updateImage.name"
      :error="updateError"
      @confirm="update"
    />
    <SystemUpdateDialog
      v-if="updateNowOpen"
      v-model:open="updateNowOpen"
      :images="built"
      everything
      :available="available"
      :loading="starting === 'update'"
      :error="updateNowError"
      @confirm="updateNow"
    />
  </section>
</template>
