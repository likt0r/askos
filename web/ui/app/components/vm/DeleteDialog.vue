<script setup lang="ts">
// Deleting a VM, with the CLI's rules: only after typing its name; its Data disk is
// archived on the Host unless deleting it is chosen as well.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Pick<Vm, 'name' | 'data_disk_gib'> }>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const toast = useToast()

const deleteData = ref(false)
const del = useChange('deleteVm', { form: true })

watch(open, (value) => {
  if (value) {
    deleteData.value = false
    del.reset()
  }
})

async function remove() {
  const name = props.vm.name
  const r = await del.run({ vm: name, deleteDataDisk: deleteData.value })
  if (!r.ok) return
  const done = r.data as { data_disk?: string, archive?: string | null } | null
  open.value = false
  toast.add({
    title: t('vm.delete.done', { name }),
    description: done?.data_disk === 'archived' && done.archive
      ? t('vm.delete.archivedAt', { path: done.archive })
      : done?.data_disk === 'deleted' ? t('vm.delete.dataDeleted') : undefined,
    color: 'success',
    icon: 'i-lucide-check',
  })
  await navigateTo('/vms')
}
</script>

<template>
  <VmConfirmDialog
    v-model:open="open"
    :title="t('vm.delete.title', { name: props.vm.name })"
    :description="t('vm.delete.text')"
    :confirm-label="t('vm.delete.confirm')"
    :typed="props.vm.name"
    :loading="del.busy.value"
    :failure="del.shown.value"
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
