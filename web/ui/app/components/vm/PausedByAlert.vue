<script setup lang="ts">
// A VM an Alert with a strong Signal paused (#11, agentbox alert-watch): the destination, its
// Signals, when, and what to do. Nothing in any other state, and nothing once it runs again.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const tr = useTranslator()
const when = useWhen()

const info = computed(() => pausedByAlert(props.vm))
const signals = computed(() => (info.value?.signals ?? []).map(s => signalName(s, tr)).join(', '))
</script>

<template>
  <UAlert
    v-if="info"
    color="error"
    variant="subtle"
    icon="i-lucide-shield-alert"
    :title="t('vm.pausedByAlert.title')"
    data-testid="vm-paused-by-alert"
    :data-domain="info.domain"
  >
    <template #description>
      <div class="space-y-1">
        <p data-testid="paused-by-alert-what">
          {{ t('vm.pausedByAlert.what', { domain: info.domain, signals, time: when.at(info.time) }) }}
        </p>
        <p data-testid="paused-by-alert-hint">
          {{ t('vm.pausedByAlert.hint') }}
        </p>
      </div>
    </template>
  </UAlert>
</template>
