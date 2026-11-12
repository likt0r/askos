<script setup lang="ts">
// The VM's Container image cleanup: on/off and days are separate values, so turning it off
// keeps the days for the next time it is turned on. The next hourly run uses the new values.
import type { Vm } from '~/utils/live'

const props = defineProps<{
  vm: Pick<Vm, 'name' | 'container_image_cleanup' | 'container_image_cleanup_days'>
}>()
const open = defineModel<boolean>('open', { default: false })
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const enabled = ref(true)
const days = ref<string | number>('7')
const saving = ref(false)
const error = ref<string | null>(null)
const current = computed(() => containerImageCleanup(props.vm))

watch(open, (v) => {
  if (!v) return
  enabled.value = current.value.enabled
  days.value = current.value.days
  error.value = null
})

const wholeDays = computed(() => wholeNumber(days.value, 1, 365))
const inputError = computed(() => typeof wholeDays.value === 'number' ? null : t('vm.containerImageCleanup.invalid'))
const changes = computed(() => {
  const body: { enabled?: boolean, days?: number } = {}
  if (enabled.value !== current.value.enabled) body.enabled = enabled.value
  if (typeof wholeDays.value === 'number' && wholeDays.value !== current.value.days) body.days = wholeDays.value
  return body
})
const ready = computed(() => !saving.value && typeof wholeDays.value === 'number' && Object.keys(changes.value).length > 0)

async function save() {
  if (!ready.value) return
  const name = props.vm.name
  saving.value = true
  error.value = null
  try {
    await api(`/vms/${name}/container-image-cleanup`, { method: 'PUT', body: changes.value })
    open.value = false
    toast.add({ title: t('vm.containerImageCleanup.saved', { name }), color: 'success', icon: 'i-lucide-check' })
  }
  catch (e) {
    error.value = errorText(e)
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <UModal
    v-model:open="open"
    :title="t('vm.containerImageCleanup.title', { name: props.vm.name })"
    :description="t('vm.containerImageCleanup.text')"
    :dismissible="!saving"
    :close="!saving"
  >
    <template #body>
      <form class="space-y-4" data-testid="container-image-cleanup-dialog" novalidate @submit.prevent="save">
        <USwitch v-model="enabled" :label="t('vm.containerImageCleanup.enabled')" :disabled="saving" data-testid="container-image-cleanup-enabled" />
        <UFormField
          :label="t('vm.containerImageCleanup.days')"
          :help="inputError ? undefined : t('vm.containerImageCleanup.daysHelp')"
          :error="inputError ?? undefined"
        >
          <UInput v-model="days" inputmode="numeric" class="w-full" :disabled="saving" data-testid="container-image-cleanup-days" />
        </UFormField>
        <UAlert
          color="info"
          variant="subtle"
          icon="i-lucide-info"
          :title="t('vm.containerImageCleanup.nextRun')"
          data-testid="container-image-cleanup-next-run"
        />
        <UAlert
          v-if="error"
          color="error"
          variant="subtle"
          icon="i-lucide-circle-alert"
          :title="error"
          data-testid="container-image-cleanup-error"
        />
        <div class="flex flex-wrap justify-end gap-2">
          <UButton color="neutral" variant="ghost" :label="t('vm.confirm.cancel')" :disabled="saving" @click="open = false" />
          <UButton
            type="submit"
            :label="t('vm.containerImageCleanup.save')"
            :loading="saving"
            :disabled="!ready"
            data-testid="container-image-cleanup-save"
          />
        </div>
      </form>
    </template>
  </UModal>
</template>
