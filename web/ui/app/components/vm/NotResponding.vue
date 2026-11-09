<script setup lang="ts">
// A VM libvirt reports as running whose Orca has not answered for longer than the wake
// time limit (#54): what does not answer, since when, the connections that gave up, and
// what to do. Nothing in any other state.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const when = useWhen()

const info = computed(() => notResponding(props.vm))
</script>

<template>
  <UAlert
    v-if="info"
    color="error"
    variant="subtle"
    icon="i-lucide-circle-alert"
    :title="t(`vm.notResponding.what.${info.what}`)"
    data-testid="vm-not-responding"
    :data-what="info.what"
  >
    <template #description>
      <div class="space-y-1">
        <p v-if="info.since" data-testid="not-responding-since">
          {{ t('vm.notResponding.since', { time: when.at(info.since) }) }}
        </p>
        <p v-if="info.wakes" data-testid="not-responding-wakes">
          {{ t('vm.notResponding.wakes', { n: info.wakes.count, time: when.at(info.wakes.last) }, info.wakes.count) }}
        </p>
        <p data-testid="not-responding-hint">
          {{ t('vm.notResponding.hint') }}
        </p>
      </div>
    </template>
  </UAlert>
</template>
