<script setup lang="ts">
// Creating a VM with the CLI's rules (`agentbox create`): a name that cannot change
// later, an Image with a Build, memory, Data disk size, GitHub owners, and optionally
// a copy of another VM's Allowlist entries. Network and Orca port are assigned by the
// Host. The VM is not started: pairing or the first Orca connection starts it.
import type { Image } from '~/utils/images'

const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()
const live = useLive()

const NONE = '__none__'
const form = reactive({ name: '', image: '', memory: '' as string | number, disk: '' as string | number, owners: '', copyFrom: NONE })
const touched = ref(false)
const images = ref<Image[] | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)
const errorField = ref<string | null>(null)

interface Setting { value: unknown }

async function load() {
  loading.value = true
  const [imgs, settings] = await Promise.allSettled([
    api<{ images?: Image[] }>('/images'),
    api<{ settings?: Record<string, Setting> }>('/settings'),
  ])
  images.value = imgs.status === 'fulfilled' && Array.isArray(imgs.value?.images) ? imgs.value.images : []
  const s = settings.status === 'fulfilled' ? settings.value?.settings ?? {} : {}
  const usable = images.value.filter(i => i.current).map(i => i.name)
  const wanted = typeof s.default_image?.value === 'string' ? s.default_image.value : 'base'
  form.image = usable.includes(wanted) ? wanted : usable[0] ?? ''
  if (typeof s.default_memory_mib?.value === 'number') form.memory = s.default_memory_mib.value
  if (typeof s.default_data_disk_gib?.value === 'number') form.disk = s.default_data_disk_gib.value
  loading.value = false
}

watch(open, (value) => {
  if (!value) return
  Object.assign(form, { name: '', image: '', memory: '', disk: '', owners: '', copyFrom: NONE })
  touched.value = false
  error.value = null
  errorField.value = null
  void load()
})

const existing = computed(() => live.state.vms.map(v => v.name))
const name = computed(() => form.name.trim())
const nameError = computed(() => {
  const p = nameProblem(name.value, existing.value)
  if (p && (touched.value || (p !== 'required' && name.value))) return t(`vm.create.nameProblem.${p}`)
  return errorField.value === 'name' ? error.value : null
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
  ...live.state.vms.map(v => ({ value: v.name, label: v.name })),
])
const noImage = computed(() => images.value !== null && !imageItems.value.length)

const valid = computed(() => !nameProblem(name.value, existing.value) && !!form.image
  && memory.value !== 'invalid' && disk.value !== 'invalid' && !owners.value.invalid.length)

async function create() {
  touched.value = true
  if (!valid.value || saving.value) return
  saving.value = true
  error.value = null
  errorField.value = null
  try {
    const r = await api<{ vm: string, network?: number, orca_port?: number }>('/vms', {
      method: 'POST',
      body: {
        name: name.value,
        image: form.image || null,
        memory_mib: typeof memory.value === 'number' ? memory.value : null,
        data_disk_gib: typeof disk.value === 'number' ? disk.value : null,
        github_owners: owners.value.owners,
        copy_allowlist_from: form.copyFrom === NONE ? null : form.copyFrom,
      },
    })
    open.value = false
    toast.add({
      title: t('vm.create.done', { name: r?.vm ?? name.value }),
      description: t('vm.create.doneText', { network: r?.network ?? '?', port: r?.orca_port ?? '?' }),
      color: 'success',
      icon: 'i-lucide-check',
    })
    void live.refetchVms()
    await navigateTo(`/vms/${r?.vm ?? name.value}`)
  }
  catch (e) {
    error.value = errorText(e)
    errorField.value = e instanceof ApiError ? e.field ?? null : null
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <UModal v-model:open="open" :title="t('vm.create.title')" :description="t('vm.create.text')" :dismissible="!saving" :close="!saving">
    <template #body>
      <form class="space-y-4" data-testid="create-dialog" novalidate @submit.prevent="create">
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

        <p class="text-xs text-muted">
          {{ t('vm.create.assigned') }}
        </p>

        <UAlert v-if="error && errorField !== 'name'" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="create-error" />

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
