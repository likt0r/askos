<script setup lang="ts">
// The VM list: compact rows, one per VM, fed live from the stream.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vms: Vm[] }>()
const { t, te } = useI18n()

function agentsText(vm: Vm) {
  const counts = agentCounts(vm)
  if (counts === null) return t('vms.agents.unknown')
  if (!counts.length) return t('vms.agents.none')
  return counts.map(c => te(`vms.agents.${c.key}`) ? t(`vms.agents.${c.key}`, { n: c.n }) : `${c.n} ${c.key}`).join(' · ')
}

function devicesText(vm: Vm) {
  const n = deviceCount(vm)
  return n === null ? t('vms.unknown') : t('vms.devices', n)
}

function diskText(vm: Vm) {
  const days = vm.system_disk?.age_days
  return typeof days === 'number' ? t('vms.diskAge', days) : '—'
}
</script>

<template>
  <ul class="divide-y divide-default rounded-lg border border-default bg-default" data-testid="vm-list">
    <li
      class="hidden md:grid grid-cols-[minmax(8rem,1.2fr)_8.5rem_minmax(7rem,1fr)_minmax(8rem,1.2fr)_7rem_8rem] gap-3 px-4 py-2 text-xs font-medium uppercase tracking-wide text-dimmed"
      aria-hidden="true"
    >
      <span>{{ t('vms.columns.name') }}</span>
      <span>{{ t('vms.columns.state') }}</span>
      <span>{{ t('vms.columns.image') }}</span>
      <span>{{ t('vms.columns.agents') }}</span>
      <span>{{ t('vms.columns.devices') }}</span>
      <span>{{ t('vms.columns.disk') }}</span>
    </li>
    <li v-for="vm in props.vms" :key="vm.name" data-testid="vm-row" :data-vm="vm.name">
      <NuxtLink
        :to="`/vms/${vm.name}`"
        class="grid grid-cols-[1fr_auto] md:grid-cols-[minmax(8rem,1.2fr)_8.5rem_minmax(7rem,1fr)_minmax(8rem,1.2fr)_7rem_8rem] items-center gap-x-3 gap-y-1 px-4 py-2.5 text-sm hover:bg-elevated/60 focus-visible:bg-elevated/60 focus-visible:outline-none"
      >
        <span class="font-medium text-highlighted truncate">{{ vm.name }}</span>
        <span class="justify-self-end md:justify-self-start"><VmStateBadge :state="vm.state" /></span>
        <span class="text-muted truncate font-mono text-xs">
          {{ vm.image ?? '—' }}<template v-if="vm.build_pin"> @ {{ vm.build_pin }}</template>
        </span>
        <span class="text-muted truncate max-md:text-right">{{ agentsText(vm) }}</span>
        <span class="text-muted truncate max-md:hidden">{{ devicesText(vm) }}</span>
        <span class="text-muted truncate max-md:hidden">
          {{ diskText(vm) }}
          <UBadge v-if="vm.reset_due" color="warning" variant="soft" size="sm" :label="t('vms.resetDue')" class="ms-1" />
        </span>
      </NuxtLink>
    </li>
  </ul>
</template>
