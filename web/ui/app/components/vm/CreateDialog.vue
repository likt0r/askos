<script setup lang="ts">
// Creating a VM with the CLI's rules (`agentbox create`): a name that cannot change
// later, an Image with a Build, memory, Data disk size, GitHub owners, and optionally
// a copy of another VM's Allowlist entries and of its SSH keys (once, ADR 0001; SSH access
// stays off). Network and Orca port are assigned by the Host. The VM is not started: pairing or
// the first Orca connection starts it.
import type { Image } from '~/utils/images'

const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const toast = useToast()
const { vms } = useVms()

const NONE = '__none__'
const form = reactive({ name: '', image: '', memory: '' as string | number, disk: '' as string | number, owners: '', copyFrom: NONE, copySshFrom: NONE })
const touched = ref(false)
const overview = useResource('images', { enabled: open })
const images = computed<Image[] | null>(() => (overview.data.value ? overview.data.value.images ?? [] : null))
// the Settings' defaults (an extra: unreadable, the dialog goes on without them); null while
// they are read
const settings = useResource('settings', { enabled: open, extra: true })
const defaults = computed<Record<string, Setting> | null>(() => settings.data.value?.settings ?? (settings.error.value ? {} : null))
const loading = computed(() => overview.loading.value || defaults.value === null)
const create = useChange('createVm', { form: true })
const saving = create.busy
// a refusal of the name shows at the name; any other in the dialog
const nameRefused = computed(() => {
  const e = create.shown.value
  return e instanceof ApiError && e.field === 'name'
})

interface Setting { value: unknown }

watch(open, (value) => {
  if (!value) return
  Object.assign(form, { name: '', image: '', memory: '', disk: '', owners: '', copyFrom: NONE, copySshFrom: NONE })
  touched.value = false
  create.reset()
  defaultsTaken = false
})

// the memory and Data disk the Settings name, once per opening, as soon as they are known (not
// from a copy that is being read again; after the form was reset for this opening: watched later)
let defaultsTaken = false
watch([open, defaults, settings.fetching], ([isOpen, d, fetching]) => {
  if (!isOpen || !d || fetching || defaultsTaken) return
  defaultsTaken = true
  if (typeof d.default_memory_mib?.value === 'number') form.memory = d.default_memory_mib.value
  if (typeof d.default_data_disk_gib?.value === 'number') form.disk = d.default_data_disk_gib.value
})

// the Image the Settings name, else the first with a Build, once both are known (not from a
// copy that is being read again)
watch([open, images, defaults, overview.fetching, settings.fetching], ([isOpen, list, s, fetching, reading]) => {
  if (!isOpen || form.image || !list || !s || fetching || reading) return
  const usable = list.filter(i => i.current).map(i => i.name)
  const wanted = typeof s.default_image?.value === 'string' ? s.default_image.value : 'base'
  form.image = usable.includes(wanted) ? wanted : usable[0] ?? ''
})

const existing = computed(() => vms.value.map(v => v.name))
const name = computed(() => form.name.trim())
const nameError = computed(() => {
  const p = nameProblem(name.value, existing.value)
  if (p && (touched.value || (p !== 'required' && name.value))) return t(`vm.create.nameProblem.${p}`)
  return nameRefused.value ? create.text.value : null
})
const memory = computed(() => wholeNumber(form.memory, MIN_MEMORY_MIB, Number.MAX_SAFE_INTEGER))
const memoryError = computed(() => (memory.value === 'invalid' ? t('vm.create.memoryInvalid', { min: MIN_MEMORY_MIB }) : null))
const memoryHint = computed(() => (typeof memory.value === 'number' ? t('vm.create.inGib', { n: mibAsGib(memory.value) }) : t('vm.create.defaultHint')))
const disk = computed(() => wholeNumber(form.disk, 1, MAX_DATA_DISK_GIB))
const diskError = computed(() => (disk.value === 'invalid' ? t('vm.create.diskInvalid', { max: MAX_DATA_DISK_GIB }) : null))
const owners = computed(() => parseOwners(form.owners))
const ownersError = computed(() => (owners.value.invalid.length ? t('vm.create.ownersInvalid', { names: owners.value.invalid.join(', ') }) : null))

const imageItems = computed(() => (images.value ?? []).filter(i => i.current).map(i => ({
  value: i.name,
  label: `${i.name} (${i.current})`,
})))
const copyItems = computed(() => [
  { value: NONE, label: t('vm.create.copyNone') },
  ...vms.value.map(v => ({ value: v.name, label: v.name })),
])
const noImage = computed(() => images.value !== null && !imageItems.value.length)

const valid = computed(() => !nameProblem(name.value, existing.value) && !!form.image
  && memory.value !== 'invalid' && disk.value !== 'invalid' && !owners.value.invalid.length)

async function submit() {
  touched.value = true
  if (!valid.value || saving.value) return
  const r = await create.run({
    name: name.value,
    image: form.image || null,
    memory_mib: typeof memory.value === 'number' ? memory.value : null,
    data_disk_gib: typeof disk.value === 'number' ? disk.value : null,
    github_owners: owners.value.owners,
    copy_allowlist_from: form.copyFrom === NONE ? null : form.copyFrom,
    copy_ssh_keys_from: form.copySshFrom === NONE ? null : form.copySshFrom,
  })
  if (!r.ok) return
  const made = r.data as { vm?: string, network?: number, orca_port?: number, ssh_keys?: unknown[] } | null
  open.value = false
  toast.add({
    title: t('vm.create.done', { name: made?.vm ?? name.value }),
    description: [
      t('vm.create.doneText', { network: made?.network ?? '?', port: made?.orca_port ?? '?' }),
      ...(form.copySshFrom === NONE ? [] : [t('vm.create.doneSsh', { from: form.copySshFrom }, Array.isArray(made?.ssh_keys) ? made.ssh_keys.length : 0)]),
    ].join(' '),
    color: 'success',
    icon: 'i-lucide-check',
  })
  await navigateTo(`/vms/${made?.vm ?? name.value}`)
}
</script>

<template>
  <UModal v-model:open="open" :title="t('vm.create.title')" :description="t('vm.create.text')" :dismissible="!saving" :close="!saving">
    <template #body>
      <form class="space-y-4" data-testid="create-dialog" novalidate @submit.prevent="submit">
        <UFormField :label="t('vm.create.name')" :help="t('vm.create.nameHelp')" :error="nameError ?? undefined" required>
          <UInput
            v-model="form.name"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            maxlength="16"
            class="w-full font-mono"
            data-testid="create-name"
            @blur="touched = true"
          />
        </UFormField>

        <ReadFailure :resource="overview" />
        <UFormField :label="t('vm.create.image')" :error="noImage ? t('vm.create.noImage') : undefined">
          <USelect
            v-model="form.image"
            :items="imageItems"
            :loading="loading"
            :placeholder="t('vm.create.imagePlaceholder')"
            class="w-full"
            data-testid="create-image"
          />
        </UFormField>

        <div class="grid gap-4 sm:grid-cols-2">
          <UFormField :label="t('vm.create.memory')" :help="memoryError ? undefined : memoryHint" :error="memoryError ?? undefined">
            <UInput v-model="form.memory" inputmode="numeric" class="w-full" data-testid="create-memory">
              <template #trailing>
                <span class="text-xs text-muted">MiB</span>
              </template>
            </UInput>
          </UFormField>
          <UFormField :label="t('vm.create.disk')" :help="diskError ? undefined : t('vm.create.diskHelp')" :error="diskError ?? undefined">
            <UInput v-model="form.disk" inputmode="numeric" class="w-full" data-testid="create-disk">
              <template #trailing>
                <span class="text-xs text-muted">GiB</span>
              </template>
            </UInput>
          </UFormField>
        </div>

        <UFormField :label="t('vm.create.owners')" :help="ownersError ? undefined : t('vm.create.ownersHelp')" :error="ownersError ?? undefined">
          <UInput
            v-model="form.owners"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            class="w-full"
            :placeholder="t('vm.create.ownersPlaceholder')"
            data-testid="create-owners"
          />
        </UFormField>

        <UFormField :label="t('vm.create.copyFrom')" :help="t('vm.create.copyFromHelp')">
          <USelect v-model="form.copyFrom" :items="copyItems" class="w-full" data-testid="create-copy" />
        </UFormField>

        <UFormField :label="t('vm.create.copySshFrom')" :help="t('vm.create.copySshFromHelp')" name="copy_ssh_keys_from">
          <USelect v-model="form.copySshFrom" name="copy_ssh_keys_from" :items="copyItems" class="w-full" data-testid="create-copy-ssh" />
        </UFormField>

        <p class="text-xs text-muted">
          {{ t('vm.create.assigned') }}
        </p>

        <ChangeFailure :error="nameRefused ? null : create.shown.value" data-testid="create-error" />

        <div class="flex flex-wrap justify-end gap-2">
          <UButton color="neutral" variant="ghost" :label="t('vm.confirm.cancel')" :disabled="saving" @click="open = false" />
          <UButton
            type="submit"
            icon="i-lucide-plus"
            :label="saving ? t('vm.create.creating') : t('vm.create.submit')"
            :loading="saving"
            :disabled="!valid"
            data-testid="create-submit"
          />
        </div>
      </form>
    </template>
  </UModal>
</template>
