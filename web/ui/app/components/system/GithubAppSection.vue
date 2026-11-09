<script setup lang="ts">
// The GitHub App in System: where it is installed and which VMs rely on each Installation,
// GitHub owners of VMs without one (missing Installations), the link to install it
// elsewhere, and replacing App ID and private key (checked with GitHub before they replace
// the working ones; the key is never shown again).
import type { GithubAppOverview } from '~/utils/access'

const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const overview = ref<GithubAppOverview | null>(null)
const loading = ref(false)
const error = ref<unknown>(null)
const copied = ref(false)

const replacing = ref(false)
const appId = ref('')
const key = ref('')
const keyFile = ref<string | null>(null)
const confirming = ref(false)
const saving = ref(false)
const saveError = ref<unknown>(null)
const saved = ref<{ id: number, slug: string | null } | null>(null)

const appIdValid = computed(() => /^[1-9]\d{0,11}$/.test(appId.value.trim()))
const keyLooksRight = computed(() => /-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(key.value))
const canReplace = computed(() => appIdValid.value && keyLooksRight.value && !saving.value)
const missingApp = computed(() => error.value instanceof ApiError && error.value.code === 'github_app_missing')

async function load() {
  loading.value = true
  try {
    overview.value = await api<GithubAppOverview>('/github-app')
    error.value = null
  }
  catch (e) {
    error.value = e
  }
  finally {
    loading.value = false
  }
}

async function copyLink() {
  const link = overview.value?.app.install_url
  if (!link) return
  try {
    await navigator.clipboard.writeText(link)
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  }
  catch {
    toast.add({ color: 'warning', title: t('githubApp.copyFailed') })
  }
}

function startReplace() {
  appId.value = overview.value ? String(overview.value.app.id) : ''
  key.value = ''
  keyFile.value = null
  saveError.value = null
  saved.value = null
  replacing.value = true
}

function cancelReplace() {
  key.value = ''
  keyFile.value = null
  replacing.value = false
}

async function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > 8000) {
    saveError.value = new ApiError(400, 'invalid_private_key', t('errors.invalid_private_key'))
    return
  }
  key.value = await file.text()
  keyFile.value = file.name
}

async function replace() {
  if (!canReplace.value) return
  const body = { app_id: Number(appId.value.trim()), private_key: key.value }
  // the key leaves the page with the request
  key.value = ''
  keyFile.value = null
  saving.value = true
  saveError.value = null
  try {
    const r = await api<{ id: number, slug: string | null }>('/github-app', { method: 'PUT', body })
    saved.value = { id: r?.id ?? body.app_id, slug: r?.slug ?? null }
    confirming.value = false
    replacing.value = false
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('githubApp.replace.done', { id: saved.value.id }) })
    void load()
  }
  catch (e) {
    saveError.value = e
    confirming.value = false
  }
  finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="space-y-4" data-testid="github-app">
    <div class="flex flex-wrap items-center gap-2">
      <UIcon name="i-lucide-github" class="size-5 text-muted" />
      <h2 class="text-lg font-semibold text-highlighted">
        {{ t('githubApp.title') }}
      </h2>
      <span class="flex-1" />
      <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-refresh-cw" :loading="loading" :aria-label="t('access.common.refresh')" @click="load" />
      <UButton v-if="!replacing" color="neutral" variant="outline" size="sm" icon="i-lucide-key-round" :label="t('githubApp.replace.open')" data-testid="app-replace-open" @click="startReplace" />
    </div>

    <UAlert v-if="saved" color="success" variant="subtle" icon="i-lucide-circle-check" :title="t('githubApp.replace.done', { id: saved.id })" :description="t('githubApp.replace.doneText')" data-testid="app-replaced" />

    <UAlert
      v-if="error"
      :color="missingApp ? 'warning' : 'error'"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="errorText(error)"
      :description="missingApp ? t('githubApp.missingApp') : undefined"
      data-testid="app-error"
    />
    <p v-if="!overview && !error" class="text-sm text-muted">
      {{ t('access.common.loading') }}
    </p>

    <template v-if="overview">
      <div class="space-y-2 rounded-lg border border-default p-4 text-sm" data-testid="app-info">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-medium text-highlighted">{{ overview.app.name ?? overview.app.slug ?? '?' }}</span>
          <UBadge color="neutral" variant="subtle" size="sm" :label="t('githubApp.appId', { id: overview.app.id })" />
          <span v-if="overview.app.owner" class="text-muted">{{ t('githubApp.owner', { owner: overview.app.owner }) }}</span>
          <span class="flex-1" />
          <UButton v-if="overview.app.html_url" :to="overview.app.html_url" target="_blank" rel="noopener noreferrer" color="neutral" variant="link" size="sm" trailing-icon="i-lucide-external-link" :label="t('githubApp.onGithub')" />
        </div>
        <div v-if="overview.app.install_url" class="space-y-1">
          <p class="text-muted">
            {{ t('githubApp.installLink') }}
          </p>
          <div class="flex flex-wrap items-center gap-2">
            <code class="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-elevated px-2 py-1 font-mono text-xs" data-testid="install-url">{{ overview.app.install_url }}</code>
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              :icon="copied ? 'i-lucide-check' : 'i-lucide-copy'"
              :label="copied ? t('githubApp.copied') : t('githubApp.copy')"
              data-testid="install-copy"
              @click="copyLink"
            />
          </div>
        </div>
      </div>

      <div v-if="overview.missing.length" class="space-y-2" data-testid="app-missing">
        <UAlert color="warning" variant="subtle" icon="i-lucide-triangle-alert" :title="t('githubApp.missing.title', overview.missing.length)" :description="t('githubApp.missing.text')" />
        <ul class="divide-y divide-default rounded-lg border border-warning/40">
          <li v-for="m in overview.missing" :key="m.owner" class="flex flex-wrap items-center gap-2 px-4 py-2 text-sm" data-testid="missing-owner" :data-owner="m.owner">
            <UIcon name="i-lucide-building-2" class="size-4 text-warning" />
            <span class="font-medium text-highlighted">{{ m.owner }}</span>
            <span class="text-muted">{{ t('githubApp.vmsNeed', { vms: m.vms.join(', ') }, m.vms.length) }}</span>
          </li>
        </ul>
      </div>

      <div class="space-y-2">
        <h3 class="font-medium text-highlighted">
          {{ t('githubApp.installations.title') }}
        </h3>
        <p v-if="!overview.installations.length" class="text-sm text-muted">
          {{ t('githubApp.installations.none') }}
        </p>
        <ul v-else class="divide-y divide-default rounded-lg border border-default bg-default">
          <li v-for="i in overview.installations" :key="i.id ?? i.account ?? ''" class="flex flex-col gap-1 px-4 py-2.5 text-sm sm:flex-row sm:items-center sm:gap-3" data-testid="installation" :data-account="i.account">
            <span class="flex min-w-0 flex-1 flex-wrap items-center gap-2">
              <UIcon :name="i.type === 'Organization' ? 'i-lucide-building-2' : 'i-lucide-user'" class="size-4 text-muted" />
              <span class="font-medium text-highlighted">{{ i.account ?? '?' }}</span>
              <UBadge color="neutral" variant="subtle" size="sm" :label="i.repository_selection === 'all' ? t('githubApp.installations.allRepos') : t('githubApp.installations.someRepos')" />
              <UBadge v-if="i.suspended" color="error" variant="subtle" size="sm" :label="t('githubApp.installations.suspended')" />
            </span>
            <span class="text-xs text-muted">
              {{ i.vms.length ? t('githubApp.installations.vms', { vms: i.vms.join(', ') }) : t('githubApp.installations.noVms') }}
            </span>
            <UButton v-if="i.html_url" :to="i.html_url" target="_blank" rel="noopener noreferrer" color="neutral" variant="link" size="xs" trailing-icon="i-lucide-external-link" :label="t('githubApp.installations.settings')" class="self-start px-0 sm:self-center" />
          </li>
        </ul>
      </div>
    </template>

    <form v-if="replacing" class="space-y-3 rounded-lg border border-default p-4" novalidate data-testid="app-replace" @submit.prevent="confirming = true">
      <div>
        <h3 class="font-medium text-highlighted">
          {{ t('githubApp.replace.title') }}
        </h3>
        <p class="text-sm text-muted">
          {{ t('githubApp.replace.intro') }}
        </p>
      </div>
      <UFormField :label="t('githubApp.replace.appId')" name="app_id" :error="appId && !appIdValid ? t('errors.invalid_app_id') : undefined">
        <UInput v-model="appId" inputmode="numeric" autocomplete="off" class="w-full sm:w-48" data-testid="app-id" />
      </UFormField>
      <UFormField :label="t('githubApp.replace.key')" name="private_key" :help="keyFile ? t('githubApp.replace.fromFile', { file: keyFile }) : t('githubApp.replace.keyHelp')">
        <div class="space-y-2">
          <UTextarea
            v-if="!keyFile"
            v-model="key"
            :rows="4"
            autocomplete="off"
            spellcheck="false"
            placeholder="-----BEGIN RSA PRIVATE KEY-----"
            class="w-full font-mono text-xs [&_textarea]:[-webkit-text-security:disc]"
            data-testid="app-key"
          />
          <label class="inline-flex cursor-pointer items-center gap-2 text-sm text-primary">
            <UIcon name="i-lucide-key-round" class="size-4" />
            <span>{{ keyFile ? t('githubApp.replace.otherFile') : t('githubApp.replace.chooseFile') }}</span>
            <input type="file" accept=".pem,application/x-pem-file,text/plain" class="sr-only" data-testid="app-key-file" @change="chooseFile">
          </label>
        </div>
      </UFormField>
      <p v-if="key && !keyLooksRight" class="text-sm text-error">
        {{ t('errors.invalid_private_key') }}
      </p>
      <UAlert v-if="saveError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(saveError)" :description="t('githubApp.replace.nothingChanged')" role="alert" data-testid="app-replace-error" />
      <div class="flex flex-wrap justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="saving" @click="cancelReplace" />
        <UButton type="submit" icon="i-lucide-shield-check" :label="t('githubApp.replace.submit')" :disabled="!canReplace" data-testid="app-replace-submit" />
      </div>
    </form>

    <AccessConfirmDialog
      v-model:open="confirming"
      :title="t('githubApp.replace.confirmTitle')"
      :description="t('githubApp.replace.confirmText', { id: appId })"
      :confirm-label="t('githubApp.replace.confirm')"
      color="warning"
      :busy="saving"
      @confirm="replace"
    />
  </section>
</template>
