<script setup lang="ts">
// Deleting a VM, with the CLI's rules: only after typing its name; its Data disk is
// archived on the Host unless deleting it is chosen as well.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Pick<Vm, 'name' | 'data_disk_gib'> }>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const deleteData = ref(false)
const loading = ref(false)
const error = ref<string | null>(null)

watch(open, (value) => {
  if (value) {
    deleteData.value = false
    error.value = null
  }
})

async function remove() {
  loading.value = true
  error.value = null
  const name = props.vm.name
  try {
    const r = await api<{ data_disk?: string, archive?: string | null }>(`/vms/${name}`, {
      method: 'DELETE',
      body: { confirm: name, delete_data_disk: deleteData.value },
    })
    open.value = false
    toast.add({
      title: t('vm.delete.done', { name }),
      description: r?.data_disk === 'archived' && r.archive
        ? t('vm.delete.archivedAt', { path: r.archive })
        : r?.data_disk === 'deleted' ? t('vm.delete.dataDeleted') : undefined,
      color: 'success',
      icon: 'i-lucide-check',
    })
    await navigateTo('/vms')
  }
  catch (e) {
    error.value = errorText(e)
  }
  finally {
    loading.value = false
  }
}
</script>

<template>
  <VmConfirmDialog
    v-model:open="open"
    :title="t('vm.delete.title', { name: props.vm.name })"
    :description="t('vm.delete.text')"
    :confirm-label="t('vm.delete.confirm')"
    :typed="props.vm.name"
    :loading="loading"
    :error="error"
    testid="delete-dialog"
    @confirm="remove"
  >
    <UCheckbox
      v-model="deleteData"
      color="error"
      :label="t('vm.delete.deleteData')"
      :description="deleteData ? t('vm.delete.deleteDataWarning') : t('vm.delete.archived')"
      data-testid="delete-data-disk"
    />
  </VmConfirmDialog>
</template>
