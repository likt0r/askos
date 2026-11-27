<script setup lang="ts">
// The VM's SSH access (#135, ADR 0020): the switch, its SSH keys (added from a .pub file or
// pasted text, removed after a confirmation that says open sessions end, copied once from
// another VM), a change the VM has not taken yet, and the connection help. The help is what
// `agentbox ssh-config <vm> --json` gives (GET /vms/<vm>/ssh-config), shown as it is: no second
// version of the block here.
import type { SshConfig, SshCopiedKey, SshCopy } from '~/utils/access'
import type { SshKey, Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t, locale } = useI18n()
const live = useLive()
const errorText = useErrorText()
const toast = useToast()

const ssh = computed(() => props.vm.ssh ?? null)
const enabled = computed(() => ssh.value?.enabled === true)
const keys = computed<SshKey[]>(() => {
  const s = ssh.value
  return s && Array.isArray(s.keys) ? s.keys : []
})
const port = computed(() => ssh.value?.port ?? '—')

// a change agentbox saved all the same, only not (yet) in the VM (SSH_SAVED_CODES): said here, in
// the view's words, whichever change it was
const SAVED = SSH_SAVED_CODES
const savedError = ref<ApiError | null>(null)
const savedCode = computed(() => savedError.value?.code)
const missing = computed(() => savedCode.value === 'ssh_missing')
// a key file too large to be one, refused before anything is sent
const fileError = ref<ApiError | null>(null)

function refresh() {
  void live.refetchVms()
}

function clearFailures() {
  savedError.value = null
  fileError.value = null
  for (const c of [switchChange, addChange, copyChange]) c.reset()
}

function starting() {
  clearFailures()
  copyResult.value = null
}

/** Whether the change failed as saved but not applied: then it is said here. */
function savedAnyway(r: { ok: boolean, error?: unknown }) {
  if (!r.ok && r.error instanceof ApiError && SAVED.includes(r.error.code)) {
    savedError.value = r.error
    return true
  }
  return false
}

// --- the switch: one control without a form, whose failure is a toast ---
const switchChange = useChange('setSsh', { own: SAVED, failed: () => t('access.ssh.switchFailed') })
const switching = switchChange.busy
const wanted = ref(false)
const switchValue = computed(() => (switching.value ? wanted.value : enabled.value))

async function setEnabled(on: boolean) {
  if (switching.value || on === enabled.value) return
  wanted.value = on
  starting()
  const r = await switchChange.run({ vm: props.vm.name, enabled: on })
  if (r.ok) toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t(on ? 'access.ssh.turnedOn' : 'access.ssh.turnedOff', { vm: props.vm.name }) })
  else savedAnyway(r)
  refresh()
}

// --- adding a key: its text, from a .pub file or pasted, goes in the request body only ---
const keyText = ref('')
const keyName = ref('')
const keyFile = ref<string | null>(null)
const addChange = useChange('addSshKey', { form: true, own: SAVED })
const adding = addChange.busy
const isPrivate = computed(() => /PRIVATE KEY/.test(keyText.value))
const nameValid = computed(() => looksLikeSshKeyName(keyName.value.trim()))
const full = computed(() => keys.value.length >= SSH_MAX_KEYS)
const canAdd = computed(() => !!keyText.value.trim() && !isPrivate.value && nameValid.value && !full.value && !adding.value)

async function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  clearFailures()
  if (file.size > SSH_KEY_MAX_BYTES) {
    fileError.value = new ApiError(400, 'ssh_key_invalid', t('errors.ssh_key_invalid'))
    return
  }
  keyText.value = (await file.text()).trim()
  keyFile.value = file.name
}

function clearForm() {
  keyText.value = ''
  keyName.value = ''
  keyFile.value = null
}

async function addKey() {
  if (!canAdd.value) return
  starting()
  const r = await addChange.run({ vm: props.vm.name, key: keyText.value, name: keyName.value.trim() })
  if (r.ok) {
    const added = r.data as { name?: string | null, fingerprint?: string, enabled?: boolean } | null
    toast.add({
      color: 'success',
      icon: 'i-lucide-circle-check',
      title: t(added?.enabled ? 'access.ssh.add.done' : 'access.ssh.add.doneOff', { name: added?.name || added?.fingerprint || '', vm: props.vm.name }),
    })
    clearForm()
  }
  // saved, only not (yet) in the VM: the key is entered
  else if (savedAnyway(r)) clearForm()
  refresh()
}

// --- copying the keys of another VM, once (ADR 0001): agentbox adds each by the rules of one
// key and names those it leaves out (entered already, beyond 16 keys) with why ---
const copyFrom = ref<string | undefined>(undefined)
const copyChange = useChange('copySshKeys', { form: true, own: SAVED })
const copying = copyChange.busy
const copyResult = ref<SshCopy | null>(null)
const others = computed(() => live.state.vms.map(v => v.name).filter(n => n !== props.vm.name))
const copyItems = computed(() => others.value.map(n => ({ value: n, label: n })))
const canCopy = computed(() => !!copyFrom.value && others.value.includes(copyFrom.value) && !full.value && !copying.value)

watch(() => props.vm.name, () => {
  copyFrom.value = undefined
  copyResult.value = null
})

async function copyKeys() {
  if (!canCopy.value) return
  const from = copyFrom.value!
  starting()
  const r = await copyChange.run({ vm: props.vm.name, from })
  if (r.ok) {
    const copy = r.data as SshCopy | null
    copyResult.value = {
      ...copy,
      from: copy?.from ?? from,
      added: Array.isArray(copy?.added) ? copy.added : [],
      skipped: Array.isArray(copy?.skipped) ? copy.skipped : [],
    } as SshCopy
    copyFrom.value = undefined
  }
  else savedAnyway(r)
  refresh()
}

/** a form's failure that is not "saved anyway": below the forms */
const formFailure = computed(() => addChange.shown.value ?? copyChange.shown.value)
const formCode = computed(() => (formFailure.value instanceof ApiError ? formFailure.value.code : undefined))

function copiedLabel(k: SshCopiedKey) {
  return k.name || k.fingerprint || t('access.ssh.copy.unreadable')
}

/** Why agentbox left a key out: its code translated, else agentbox's own message. */
function skippedReason(k: { error: string, message: string }) {
  return errorText(new ApiError(0, k.error, k.message))
}

// --- removing a key: every open SSH session of the VM ends ---
const removing = ref<SshKey | null>(null)
const removeOpen = ref(false)
const removeChange = useChange('removeSshKey', { form: true, own: SAVED })
const removeBusy = removeChange.busy

function keyLabel(k: SshKey) {
  return k.name || k.fingerprint
}

function askRemove(k: SshKey) {
  removing.value = k
  removeChange.reset()
  removeOpen.value = true
}

async function removeKey() {
  const k = removing.value
  if (!k || removeBusy.value) return
  starting()
  const r = await removeChange.run({ vm: props.vm.name, fingerprint: k.fingerprint })
  if (r.ok) {
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('access.ssh.remove.done', { name: keyLabel(k), vm: props.vm.name }) })
    removeOpen.value = false
  }
  // saved, only not (yet) in the VM: said in the section, the dialog closes
  else if (savedAnyway(r)) removeOpen.value = false
  refresh()
}

// --- the connection help: agentbox ssh-config for this VM ---
// read again when this VM's event turns SSH access on or off or brings a new host key (resources
// module)
const help = useResource('sshConfig', { params: () => ({ vm: props.vm.name }) })
const config = computed<SshConfig | null>(() => {
  const r = help.data.value
  return r && Array.isArray(r.hosts) ? r : null
})

// only while SSH access is on (the VM says so at once; the help itself is read again then)
const host = computed(() => (enabled.value ? config.value?.hosts.find(h => h.vm === props.vm.name) ?? null : null))
const localOnly = computed(() => config.value?.mode === 'local')
const knownHostsFile = computed(() => String(host.value?.options?.UserKnownHostsFile ?? ''))
const mineCommand = computed(() => `sudo agentbox add-ssh-key ${props.vm.name} --mine`)

const copied = ref<string | null>(null)
async function copy(what: string, text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = what
    setTimeout(() => {
      if (copied.value === what) copied.value = null
    }, 2000)
  }
  catch {
    toast.add({ color: 'warning', icon: 'i-lucide-clipboard-x', title: t('access.ssh.help.copyFailed') })
  }
}
</script>

<template>
  <section class="space-y-4 rounded-lg border border-default p-4" data-testid="ssh-access">
    <!-- the switch -->
    <div class="flex flex-wrap items-start gap-3">
      <div class="min-w-0 flex-1">
        <h3 class="font-medium text-highlighted">
          {{ t('access.ssh.title') }}
        </h3>
        <p class="text-sm text-muted">
          {{ t('access.ssh.intro') }}
        </p>
      </div>
      <USwitch
        :model-value="switchValue"
        :loading="switching"
        :disabled="switching || !ssh"
        :aria-label="t('access.ssh.switch', { vm: props.vm.name })"
        data-testid="ssh-switch"
        @update:model-value="setEnabled"
      />
    </div>
    <p class="text-sm" data-testid="ssh-state" :data-enabled="enabled">
      {{ enabled ? t('access.ssh.on', { port }) : t('access.ssh.off', { port }) }}
    </p>
    <p v-if="ssh?.host_key_fingerprint" class="text-sm" data-testid="ssh-host-key">
      <span class="text-muted">{{ t('access.ssh.hostKey') }}:</span>
      <span class="ms-1 font-mono text-xs break-all">{{ ssh.host_key_fingerprint }}</span>
    </p>

    <UAlert
      v-if="ssh?.pending"
      color="warning"
      variant="subtle"
      icon="i-lucide-hourglass"
      :title="t('access.ssh.pending')"
      data-testid="ssh-pending"
    />
    <UAlert
      v-if="savedError"
      color="warning"
      variant="subtle"
      icon="i-lucide-triangle-alert"
      :title="errorText(savedError)"
      :description="missing ? t('access.ssh.missingHint') : undefined"
      role="alert"
      data-testid="ssh-error"
      :data-code="savedCode"
    />
    <UAlert v-else-if="fileError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(fileError)" role="alert" data-testid="ssh-error" :data-code="fileError.code" />
    <ChangeFailure v-else :error="formFailure" data-testid="ssh-error" :data-code="formCode" />

    <!-- the keys -->
    <div class="space-y-2">
      <h4 class="text-sm font-medium text-highlighted">
        {{ t('access.ssh.keys.title') }}
        <span class="font-normal text-muted">· {{ t('access.ssh.keys.count', keys.length) }}</span>
      </h4>
      <p v-if="!keys.length" class="text-sm text-muted" data-testid="ssh-no-keys">
        {{ t('access.ssh.keys.none') }}
      </p>
      <ul v-else class="divide-y divide-default rounded-md border border-default" data-testid="ssh-keys">
        <li
          v-for="k in keys"
          :key="k.fingerprint"
          class="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2"
          data-testid="ssh-key"
          :data-fingerprint="k.fingerprint"
        >
          <div class="min-w-0 flex-1 space-y-0.5">
            <p class="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span v-if="k.name" class="font-medium text-highlighted break-all" data-testid="ssh-key-name">{{ k.name }}</span>
              <span v-else class="text-muted" data-testid="ssh-key-name">{{ t('access.ssh.keys.noName') }}</span>
              <UBadge color="neutral" variant="subtle" size="sm" :label="k.type" data-testid="ssh-key-type" />
            </p>
            <p class="font-mono text-xs break-all text-muted" data-testid="ssh-key-fingerprint">
              {{ k.fingerprint }}
            </p>
            <p class="text-xs text-muted" data-testid="ssh-key-added">
              {{ t('access.ssh.keys.added', { when: formatTime(k.added, locale) ?? '—' }) }}
            </p>
          </div>
          <UButton
            size="xs"
            color="error"
            variant="outline"
            icon="i-lucide-trash-2"
            :label="t('access.ssh.keys.remove')"
            :aria-label="t('access.ssh.keys.removeOne', { name: keyLabel(k) })"
            data-testid="ssh-key-remove"
            @click="askRemove(k)"
          />
        </li>
      </ul>
    </div>

    <!-- adding a key -->
    <form class="space-y-2" novalidate data-testid="ssh-add" @submit.prevent="addKey">
      <h4 class="text-sm font-medium text-highlighted">
        {{ t('access.ssh.add.title') }}
      </h4>
      <p v-if="full" class="text-sm text-warning" data-testid="ssh-full">
        {{ t('access.ssh.add.full') }}
      </p>
      <UFormField :label="t('access.ssh.add.key')" name="ssh-key" :help="keyFile ? t('access.ssh.add.fromFile', { file: keyFile }) : t('access.ssh.add.keyHelp')">
        <div class="space-y-2">
          <UTextarea
            v-model="keyText"
            :rows="3"
            autocomplete="off"
            spellcheck="false"
            :placeholder="t('access.ssh.add.placeholder')"
            :disabled="full"
            class="w-full font-mono text-xs"
            :color="isPrivate ? 'error' : undefined"
            data-testid="ssh-key-input"
            @update:model-value="keyFile = null"
          />
          <label class="inline-flex cursor-pointer items-center gap-2 text-sm text-primary">
            <UIcon name="i-lucide-file-key" class="size-4" />
            <span>{{ t('access.ssh.add.file') }}</span>
            <input type="file" accept=".pub,text/plain" class="sr-only" :disabled="full" data-testid="ssh-key-file" @change="chooseFile">
          </label>
        </div>
      </UFormField>
      <p v-if="isPrivate" class="text-sm text-error" data-testid="ssh-private-key">
        {{ t('access.ssh.add.privateKey') }}
      </p>
      <UFormField
        :label="t('access.ssh.add.name')"
        :hint="t('access.ssh.add.optional')"
        name="ssh-key-name"
        :help="t('access.ssh.add.nameHelp')"
        :error="nameValid ? undefined : t('access.ssh.add.nameInvalid')"
      >
        <UInput v-model="keyName" autocomplete="off" spellcheck="false" maxlength="100" :disabled="full" class="w-full sm:w-72" data-testid="ssh-key-name-input" />
      </UFormField>
      <div class="flex justify-end">
        <UButton type="submit" icon="i-lucide-plus" :label="t('access.ssh.add.submit')" :loading="adding" :disabled="!canAdd" data-testid="ssh-key-add" />
      </div>
    </form>

    <!-- copying the keys of another VM -->
    <form class="space-y-2" novalidate data-testid="ssh-copy" @submit.prevent="copyKeys">
      <h4 class="text-sm font-medium text-highlighted">
        {{ t('access.ssh.copy.title') }}
      </h4>
      <p class="text-sm text-muted">
        {{ t('access.ssh.copy.help') }}
      </p>
      <p v-if="!others.length" class="text-sm text-muted" data-testid="ssh-copy-no-other">
        {{ t('access.ssh.copy.noOther') }}
      </p>
      <div v-else class="flex flex-wrap items-end gap-2">
        <UFormField :label="t('access.ssh.copy.from')" name="ssh-copy-from">
          <USelect
            v-model="copyFrom"
            name="ssh-copy-from"
            :items="copyItems"
            :placeholder="t('access.ssh.copy.choose')"
            :disabled="full || copying"
            class="w-48"
            data-testid="ssh-copy-from"
          />
        </UFormField>
        <UButton type="submit" icon="i-lucide-copy-plus" :label="t('access.ssh.copy.submit')" :loading="copying" :disabled="!canCopy" data-testid="ssh-copy-submit" />
      </div>
      <div v-if="copyResult" class="space-y-2 rounded-md border border-default p-3" data-testid="ssh-copy-result">
        <p class="text-sm" data-testid="ssh-copy-summary">
          {{ copyResult.added.length || copyResult.skipped.length
            ? t('access.ssh.copy.added', { from: copyResult.from }, copyResult.added.length)
            : t('access.ssh.copy.nothing', { from: copyResult.from }) }}
          <template v-if="copyResult.added.length && !copyResult.enabled">
            {{ t('access.ssh.copy.off') }}
          </template>
        </p>
        <ul v-if="copyResult.added.length" class="space-y-1" data-testid="ssh-copy-added">
          <li
            v-for="(k, i) in copyResult.added"
            :key="k.fingerprint ?? i"
            class="flex flex-wrap items-baseline gap-x-2 text-sm"
            data-testid="ssh-copy-added-key"
            :data-fingerprint="k.fingerprint"
          >
            <UIcon name="i-lucide-check" class="size-4 shrink-0 self-center text-success" />
            <span class="font-medium break-all">{{ copiedLabel(k) }}</span>
            <span v-if="k.name && k.fingerprint" class="font-mono text-xs break-all text-muted">{{ k.fingerprint }}</span>
          </li>
        </ul>
        <template v-if="copyResult.skipped.length">
          <p class="text-sm" data-testid="ssh-copy-skipped-title">
            {{ t('access.ssh.copy.skipped') }}
          </p>
          <ul class="space-y-1" data-testid="ssh-copy-skipped">
            <li
              v-for="(k, i) in copyResult.skipped"
              :key="k.fingerprint ?? i"
              class="flex flex-wrap items-baseline gap-x-2 text-sm"
              data-testid="ssh-copy-skipped-key"
              :data-fingerprint="k.fingerprint"
              :data-error="k.error"
            >
              <UIcon name="i-lucide-minus" class="size-4 shrink-0 self-center text-warning" />
              <span class="font-medium break-all" data-testid="ssh-copy-skipped-name">{{ copiedLabel(k) }}</span>
              <span class="text-muted" data-testid="ssh-copy-reason">{{ skippedReason(k) }}</span>
            </li>
          </ul>
        </template>
      </div>
    </form>

    <!-- the connection help -->
    <div class="space-y-3 border-t border-default pt-3" data-testid="ssh-help">
      <h4 class="text-sm font-medium text-highlighted">
        {{ t('access.ssh.help.title') }}
      </h4>
      <ReadFailure :resource="help" data-testid="ssh-help-failed" />
      <p v-if="!enabled && !help.error.value" class="text-sm text-muted" data-testid="ssh-help-off">
        {{ t('access.ssh.help.off') }}
      </p>
      <template v-if="host">
        <p class="text-sm text-muted">
          {{ t('access.ssh.help.intro') }}
        </p>
        <div class="space-y-1" data-testid="ssh-help-known-hosts">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="text-sm font-medium">
              {{ t('access.ssh.help.knownHosts') }}
            </p>
            <UButton
              v-if="host.known_hosts"
              size="xs"
              color="neutral"
              variant="outline"
              :icon="copied === 'known_hosts' ? 'i-lucide-check' : 'i-lucide-copy'"
              :label="copied === 'known_hosts' ? t('access.ssh.help.copied') : t('access.ssh.help.copy')"
              data-testid="ssh-copy-known-hosts"
              @click="copy('known_hosts', host.known_hosts + '\n')"
            />
          </div>
          <template v-if="host.known_hosts">
            <p class="text-xs text-muted">
              {{ t('access.ssh.help.knownHostsHelp', { file: knownHostsFile }) }}
            </p>
            <pre class="overflow-x-auto rounded-md bg-elevated p-2 font-mono text-xs" data-testid="ssh-known-hosts">{{ host.known_hosts }}</pre>
          </template>
          <p v-else class="text-sm text-warning" data-testid="ssh-no-host-key">
            {{ t('access.ssh.help.noHostKey') }}
          </p>
        </div>
        <div class="space-y-1">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="text-sm font-medium">
              {{ t('access.ssh.help.config') }}
            </p>
            <UButton
              size="xs"
              color="neutral"
              variant="outline"
              :icon="copied === 'config' ? 'i-lucide-check' : 'i-lucide-copy'"
              :label="copied === 'config' ? t('access.ssh.help.copied') : t('access.ssh.help.copy')"
              data-testid="ssh-copy-config"
              @click="copy('config', host.config)"
            />
          </div>
          <p class="text-xs text-muted">
            {{ t('access.ssh.help.configHelp', { host: host.host }) }}
          </p>
          <pre class="overflow-x-auto rounded-md bg-elevated p-2 font-mono text-xs" data-testid="ssh-config-block">{{ host.config }}</pre>
        </div>
        <div class="space-y-1">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="text-sm font-medium">
              {{ t('access.ssh.help.command') }}
            </p>
            <UButton
              size="xs"
              color="neutral"
              variant="outline"
              :icon="copied === 'command' ? 'i-lucide-check' : 'i-lucide-copy'"
              :label="copied === 'command' ? t('access.ssh.help.copied') : t('access.ssh.help.copy')"
              data-testid="ssh-copy-command"
              @click="copy('command', host.command)"
            />
          </div>
          <pre class="overflow-x-auto rounded-md bg-elevated p-2 font-mono text-xs" data-testid="ssh-command">{{ host.command }}</pre>
        </div>
      </template>
      <div v-if="localOnly" class="space-y-1" data-testid="ssh-local-only">
        <p class="text-sm text-muted">
          {{ t('access.ssh.help.localOnly') }}
        </p>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <p class="text-sm">
            {{ t('access.ssh.help.mine') }}
          </p>
          <UButton
            size="xs"
            color="neutral"
            variant="outline"
            :icon="copied === 'mine' ? 'i-lucide-check' : 'i-lucide-copy'"
            :label="copied === 'mine' ? t('access.ssh.help.copied') : t('access.ssh.help.copy')"
            data-testid="ssh-copy-mine"
            @click="copy('mine', mineCommand)"
          />
        </div>
        <pre class="overflow-x-auto rounded-md bg-elevated p-2 font-mono text-xs" data-testid="ssh-mine">{{ mineCommand }}</pre>
      </div>
    </div>

    <AccessConfirmDialog
      v-model:open="removeOpen"
      :title="t('access.ssh.remove.title', { name: removing ? keyLabel(removing) : '' })"
      :description="t('access.ssh.remove.text', { vm: props.vm.name })"
      :confirm-label="t('access.ssh.remove.confirm')"
      :busy="removeBusy"
      :failure="removeChange.shown.value"
      @confirm="removeKey"
    />
  </section>
</template>
