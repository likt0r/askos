<script setup lang="ts">
// The GitHub Apps in System (several since askos#25, one per trust boundary): for each, where
// it is installed and which VMs rely on each Installation, and the link to install it
// elsewhere; GitHub owners of VMs that no App is installed on (missing Installations); adding
// an App, replacing one App's key (checked with GitHub before it is stored; the key is never
// shown again) and removing an App.
import type { GithubApp } from '~/utils/access'

const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

// the Apps, read again on the Activity entries that change them; no App set up is a state of
// the resource (`none`): there is no overview then
const apps = useResource('githubApp')
const missingApp = computed(() => apps.data.value?.none === true)
const overview = computed(() => (missingApp.value ? null : apps.data.value ?? null))
const loading = apps.fetching
const copied = ref<number | null>(null)

// the form: a new App, or a new key for the App with this ID
const editing = ref<'add' | 'replace' | null>(null)
const appId = ref('')
const key = ref('')
const keyFile = ref<string | null>(null)
const confirming = ref(false)
// a key file too large to be one, refused before anything is sent
const fileError = ref<unknown>(null)
const saved = ref<{ id: number, added: boolean } | null>(null)
const put = useChange('setGithubApp', { form: true })
const saving = put.busy

const removing = ref<GithubApp | null>(null)
const removeOpen = ref(false)
const del = useChange('removeGithubApp', { form: true })

const appIdValid = computed(() => /^[1-9]\d{0,11}$/.test(appId.value.trim()))
const keyLooksRight = computed(() => /-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(key.value))
const canSave = computed(() => appIdValid.value && keyLooksRight.value && !saving.value)
const known = computed(() => overview.value?.apps.some(a => String(a.id) === appId.value.trim()) ?? false)

function appError(app: GithubApp) {
  return app.error ? new ApiError(0, app.error.error, app.error.message) : null
}

async function copyLink(app: GithubApp) {
  if (!app.install_url) return
  try {
    await navigator.clipboard.writeText(app.install_url)
    copied.value = app.id
    setTimeout(() => (copied.value = null), 2000)
  }
  catch {
    toast.add({ color: 'warning', title: t('githubApp.copyFailed') })
  }
}

function startEdit(mode: 'add' | 'replace', app?: GithubApp) {
  appId.value = app ? String(app.id) : ''
  key.value = ''
  keyFile.value = null
  fileError.value = null
  put.reset()
  saved.value = null
  editing.value = mode
}

function cancelEdit() {
  key.value = ''
  keyFile.value = null
  editing.value = null
}

async function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > 8000) {
    put.reset()
    fileError.value = new ApiError(400, 'invalid_private_key', t('errors.invalid_private_key'))
    return
  }
  fileError.value = null
  key.value = await file.text()
  keyFile.value = file.name
}

async function save() {
  if (!canSave.value) return
  const body = { app_id: Number(appId.value.trim()), private_key: key.value }
  // the key leaves the page with the request
  key.value = ''
  keyFile.value = null
  fileError.value = null
  const r = await put.run(body)
  confirming.value = false
  if (!r.ok) return
  const answer = r.data as { id?: number, added?: boolean } | null
  saved.value = { id: answer?.id ?? body.app_id, added: answer?.added ?? false }
  editing.value = null
  toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t(saved.value.added ? 'githubApp.add.done' : 'githubApp.replace.done', { id: saved.value.id }) })
}

function askRemove(app: GithubApp) {
  removing.value = app
  del.reset()
  removeOpen.value = true
}

async function remove() {
  const app = removing.value
  if (!app || !(await del.run(app.id)).ok) return
  removeOpen.value = false
  saved.value = null
  // a new key for the App just removed has nothing left to replace
  if (editing.value === 'replace' && appId.value === String(app.id)) cancelEdit()
  toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('githubApp.remove.done', { id: app.id }) })
}
</script>

<template>
  <section class="space-y-4" data-testid="github-app">
    <div class="flex flex-wrap items-center gap-2">
      <UIcon name="i-lucide-github" class="size-5 text-muted" />
      <h2 class="text-lg font-semibold text-highlighted">
        {{ t('githubApp.title') }}
      </h2>
      <span class="flex-1" />
      <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-refresh-cw" :loading="loading" :aria-label="t('access.common.refresh')" @click="apps.retry()" />
      <UButton v-if="!editing" color="neutral" variant="outline" size="sm" icon="i-lucide-plus" :label="t('githubApp.add.open')" data-testid="app-add-open" @click="startEdit('add')" />
    </div>
    <p class="text-sm text-muted">
      {{ t('githubApp.intro') }}
    </p>

    <UAlert v-if="saved" color="success" variant="subtle" icon="i-lucide-circle-check" :title="t(saved.added ? 'githubApp.add.done' : 'githubApp.replace.done', { id: saved.id })" :description="t(saved.added ? 'githubApp.add.doneText' : 'githubApp.replace.doneText')" data-testid="app-replaced" />

    <UAlert
      v-if="missingApp"
      color="warning"
      variant="subtle"
      icon="i-lucide-circle-alert"
      :title="t('errors.github_app_missing')"
      :description="t('githubApp.missingApp')"
      data-testid="app-error"
    />
    <ReadFailure :resource="apps" />
    <p v-if="apps.loading.value" class="text-sm text-muted">
      {{ t('access.common.loading') }}
    </p>

    <template v-if="overview">
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

      <div v-for="app in overview.apps" :key="app.id" class="space-y-3 rounded-lg border border-default p-4 text-sm" data-testid="app-info" :data-app-id="app.id">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-medium text-highlighted">{{ app.name ?? app.slug ?? '?' }}</span>
          <UBadge color="neutral" variant="subtle" size="sm" :label="t('githubApp.appId', { id: app.id })" />
          <span v-if="app.owner" class="text-muted">{{ t('githubApp.owner', { owner: app.owner }) }}</span>
          <span class="flex-1" />
          <UButton v-if="app.html_url" :to="app.html_url" target="_blank" rel="noopener noreferrer" color="neutral" variant="link" size="sm" trailing-icon="i-lucide-external-link" :label="t('githubApp.onGithub')" />
          <UButton v-if="!editing" color="neutral" variant="outline" size="xs" icon="i-lucide-key-round" :label="t('githubApp.replace.open')" data-testid="app-replace-open" @click="startEdit('replace', app)" />
          <UButton color="error" variant="ghost" size="xs" icon="i-lucide-trash-2" :label="t('githubApp.remove.open')" data-testid="app-remove-open" @click="askRemove(app)" />
        </div>

        <UAlert v-if="app.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(appError(app))" :description="app.error.message" data-testid="app-item-error" />

        <div v-if="app.install_url" class="space-y-1">
          <p class="text-muted">
            {{ t('githubApp.installLink') }}
          </p>
          <div class="flex flex-wrap items-center gap-2">
            <code class="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-elevated px-2 py-1 font-mono text-xs" data-testid="install-url">{{ app.install_url }}</code>
            <UButton
              color="neutral"
              variant="outline"
              size="sm"
              :icon="copied === app.id ? 'i-lucide-check' : 'i-lucide-copy'"
              :label="copied === app.id ? t('githubApp.copied') : t('githubApp.copy')"
              data-testid="install-copy"
              @click="copyLink(app)"
            />
          </div>
        </div>

        <div v-if="!app.error" class="space-y-2">
          <h3 class="font-medium text-highlighted">
            {{ t('githubApp.installations.title') }}
          </h3>
          <p v-if="!app.installations.length" class="text-muted">
            {{ t('githubApp.installations.none') }}
          </p>
          <ul v-else class="divide-y divide-default rounded-lg border border-default bg-default">
            <li v-for="i in app.installations" :key="i.id ?? i.account ?? ''" class="flex flex-col gap-1 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-3" data-testid="installation" :data-account="i.account">
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
      </div>
    </template>

    <form v-if="editing" class="space-y-3 rounded-lg border border-default p-4" novalidate data-testid="app-replace" @submit.prevent="confirming = true">
      <div>
        <h3 class="font-medium text-highlighted">
          {{ editing === 'add' ? t('githubApp.add.title') : t('githubApp.replace.title', { id: appId }) }}
        </h3>
        <p class="text-sm text-muted">
          {{ editing === 'add' ? t('githubApp.add.intro') : t('githubApp.replace.intro') }}
        </p>
      </div>
      <UFormField :label="t('githubApp.replace.appId')" name="app_id" :error="appId && !appIdValid ? t('errors.invalid_app_id') : undefined" :help="editing === 'add' && known ? t('githubApp.add.known') : undefined">
        <UInput v-model="appId" inputmode="numeric" autocomplete="off" :disabled="editing === 'replace'" class="w-full sm:w-48" data-testid="app-id" />
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
      <UAlert v-if="fileError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(fileError)" :description="t('githubApp.replace.nothingChanged')" role="alert" data-testid="app-replace-error" />
      <ChangeFailure :error="put.shown.value" :description="t('githubApp.replace.nothingChanged')" data-testid="app-replace-error" />
      <div class="flex flex-wrap justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="saving" @click="cancelEdit" />
        <UButton type="submit" icon="i-lucide-shield-check" :label="editing === 'add' ? t('githubApp.add.submit') : t('githubApp.replace.submit')" :disabled="!canSave" data-testid="app-replace-submit" />
      </div>
    </form>

    <AccessConfirmDialog
      v-model:open="confirming"
      :title="editing === 'add' && !known ? t('githubApp.add.confirmTitle') : t('githubApp.replace.confirmTitle')"
      :description="editing === 'add' && !known ? t('githubApp.add.confirmText', { id: appId }) : t('githubApp.replace.confirmText', { id: appId })"
      :confirm-label="editing === 'add' && !known ? t('githubApp.add.confirm') : t('githubApp.replace.confirm')"
      color="warning"
      :busy="saving"
      @confirm="save"
    />

    <AccessConfirmDialog
      v-model:open="removeOpen"
      :title="t('githubApp.remove.confirmTitle', { id: removing?.id ?? '' })"
      :description="t('githubApp.remove.confirmText')"
      :confirm-label="t('githubApp.remove.confirm')"
      :busy="del.busy.value"
      :failure="del.shown.value"
      @confirm="remove"
    />
  </section>
</template>
