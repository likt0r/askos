<script setup lang="ts">
// Forbids a destination for one VM, from its blocked list: the domain and the VM are fixed, the
// duration is chosen as for an Allowlist entry (the same durations and permanent), a note is
// optional. The proxy then refuses it at once, without a hold, also in Open web. Of a Forbid and an
// Allowlist entry the more specific wins (ADR 0024): it beats an entry of a parent domain or one
// for all VMs, an entry of a subdomain beats it, and the VM's own entry of the same domain gives way
// to it.
import type { EntryDuration } from '~/utils/access'

const props = defineProps<{ domain: string, vm: string }>()
const open = defineModel<boolean>('open', { default: false })

const { t } = useI18n()
const toast = useToast()

const duration = ref<EntryDuration>('1h')
const note = ref('')
const forbid = useChange('forbid', { form: true })
const busy = forbid.busy

const durations = computed(() => ENTRY_DURATIONS.map(d => ({ value: d, label: t(`allowlist.durations.${d}`) })))
const canSubmit = computed(() => !busy.value && note.value.length <= 200 && !!props.domain)

watch(open, (isOpen) => {
  if (!isOpen) return
  duration.value = '1h'
  note.value = ''
  forbid.reset()
}, { immediate: true })

async function submit() {
  if (!canSubmit.value) return
  const body: Record<string, string> = { domain: props.domain, duration: duration.value }
  if (note.value.trim()) body.note = note.value.trim()
  if (!(await forbid.run({ vm: props.vm, body })).ok) return
  toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('forbid.add.done', { domain: props.domain, vm: props.vm }) })
  open.value = false
}
</script>

<template>
  <UModal v-model:open="open" :title="t('forbid.add.title', { domain: props.domain })" :description="t('forbid.add.description', { vm: props.vm })">
    <template #body>
      <form class="space-y-4" novalidate data-testid="forbid-add" @submit.prevent="submit">
        <p class="break-all font-mono text-sm text-highlighted" data-testid="forbid-domain">
          {{ props.domain }}
        </p>
        <p class="text-sm text-muted">
          {{ t('allowlist.scope.vm', { vm: props.vm }) }}
        </p>
        <UFormField :label="t('allowlist.add.duration')" name="duration">
          <div class="flex flex-wrap gap-2" role="radiogroup" :aria-label="t('allowlist.add.duration')" data-testid="forbid-duration">
            <UButton
              v-for="d in durations"
              :key="d.value"
              role="radio"
              :aria-checked="duration === d.value"
              :color="duration === d.value ? 'primary' : 'neutral'"
              :variant="duration === d.value ? 'solid' : 'outline'"
              size="sm"
              :label="d.label"
              :data-duration="d.value"
              @click="duration = d.value"
            />
          </div>
        </UFormField>
        <UFormField :label="t('allowlist.add.note')" name="note" :hint="t('allowlist.add.optional')" :error="note.length > 200 ? t('allowlist.add.noteTooLong') : undefined">
          <UInput v-model="note" :placeholder="t('forbid.add.notePlaceholder')" class="w-full" data-testid="forbid-note" />
        </UFormField>
        <ChangeFailure :error="forbid.shown.value" data-testid="forbid-error" />
        <button type="submit" class="hidden" />
      </form>
    </template>
    <template #footer>
      <div class="flex w-full flex-wrap justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" @click="open = false" />
        <UButton color="error" icon="i-lucide-ban" :label="t('forbid.add.submit')" :loading="busy" :disabled="!canSubmit" data-testid="forbid-submit" @click="submit" />
      </div>
    </template>
  </UModal>
</template>
