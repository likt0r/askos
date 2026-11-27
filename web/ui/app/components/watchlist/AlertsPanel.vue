<script setup lang="ts">
// Alerts of every VM (last 24 hours, harmless destinations left out by the backend), new
// ones first; mark one destination's or a whole VM's as seen. The resource `alerts`, one copy per
// VM: a VM's `alert` events, its Alerts marked seen and a change of the Harmless marks read it
// again (the resources module's rows).
import type { AlertGroup, VmAlert } from '~/utils/access'

const emit = defineEmits<{ check: [domain: string] }>()
const { t } = useI18n()
const live = useLive()
const when = useWhen()
const now = useNow(30000)

const onlyNew = ref(false)
const busy = ref<string | null>(null)
// a button's change: its failure is a toast; the list is read again on its Activity entry
const seen = useChange('markSeen', { failed: () => t('watchlist.alerts.markSeenFailed') })

const vmNames = computed(() => live.state.vms.map(v => v.name))
const each = useResourceEach('alerts', { params: () => vmNames.value.map(vm => ({ vm })) })
const alerts = computed<VmAlert[]>(() => each.value.flatMap((r) => {
  const vm = (r.params as { vm: string }).vm
  const groups = r.data.value
  return (Array.isArray(groups) ? groups : []).map((a: AlertGroup) => ({ ...a, vm }))
}))
// the VM list is known and every VM's Alerts were read (or could not be)
const loaded = computed(() => live.state.loaded && each.value.every(r => !r.loading.value))
const loading = computed(() => each.value.some(r => r.fetching.value))
const unseen = computed(() => alerts.value.filter(a => !a.seen))
const shown = computed(() => {
  const list = onlyNew.value ? unseen.value : alerts.value
  return [...list].sort((a, b) => Number(a.seen) - Number(b.seen) || b.last - a.last)
})
const vmsWithNew = computed(() => [...new Set(unseen.value.map(a => a.vm))])

/** Read every VM's Alerts again (the refresh button). */
async function load() {
  await Promise.all(each.value.map(r => r.retry()))
}

async function markSeen(vm: string, domain?: string) {
  busy.value = `${vm}/${domain ?? '*'}`
  try {
    await seen.run({ vm, domain })
  }
  finally {
    busy.value = null
  }
}

// every VM's, one after the other; one that fails says so in its own toast
async function markAllSeen() {
  for (const vm of vmsWithNew.value) await markSeen(vm)
}


</script>

<template>
  <section class="space-y-3" data-testid="alerts">
    <div class="flex flex-wrap items-center gap-2">
      <h3 class="font-medium text-highlighted">
        {{ t('watchlist.alerts.title') }}
      </h3>
      <UBadge v-if="unseen.length" color="error" variant="solid" size="sm" :label="t('watchlist.alerts.new', unseen.length)" data-testid="alerts-new" />
      <span class="flex-1" />
      <USwitch v-model="onlyNew" size="sm" :label="t('watchlist.alerts.onlyNew')" />
      <UButton
        v-if="unseen.length"
        color="neutral"
        variant="outline"
        size="sm"
        icon="i-lucide-check-check"
        :label="t('watchlist.alerts.allSeen')"
        :loading="busy !== null"
        data-testid="alerts-all-seen"
        @click="markAllSeen"
      />
      <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-refresh-cw" :loading="loading" :aria-label="t('access.common.refresh')" @click="load" />
    </div>
    <p class="text-sm text-muted">
      {{ t('watchlist.alerts.intro') }}
    </p>
    <ReadFailure v-for="r in each" :key="(r.params as { vm: string }).vm" :resource="r" data-testid="alerts-failed" />

    <p v-if="!loaded" class="text-sm text-muted">
      {{ t('access.common.loading') }}
    </p>
    <p v-else-if="!shown.length" class="rounded-lg border border-dashed border-default p-6 text-center text-sm text-muted" data-testid="alerts-empty">
      {{ alerts.length ? t('watchlist.alerts.noneNew') : t('watchlist.alerts.none') }}
    </p>
    <ul v-else class="divide-y divide-default rounded-lg border border-default bg-default">
      <li
        v-for="a in shown"
        :key="`${a.vm}/${a.domain}`"
        class="flex flex-col gap-1.5 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-4"
        :class="a.seen ? '' : 'bg-error/5'"
        data-testid="alert-row"
        :data-vm="a.vm"
        :data-domain="a.domain"
        :data-seen="a.seen"
      >
        <div class="min-w-0 flex-1 space-y-1">
          <div class="flex flex-wrap items-center gap-2">
            <UIcon v-if="!a.seen" name="i-lucide-bell" class="size-4 text-error" :aria-label="t('watchlist.alerts.unseen')" />
            <span class="break-all font-mono text-sm text-highlighted">{{ a.domain }}</span>
            <UBadge color="neutral" variant="subtle" size="sm" :label="a.vm" />
            <WatchlistSignalBadges :names="a.tags" />
          </div>
          <p class="text-xs text-muted">
            {{ t('watchlist.alerts.times', a.count) }} · {{ t('watchlist.alerts.last', { when: when.ago(a.last, now) }) }}
            <template v-if="a.count > 1">
              · {{ t('watchlist.alerts.first', { when: when.at(a.first) }) }}
            </template>
          </p>
        </div>
        <div class="flex shrink-0 flex-wrap gap-1 max-sm:-ms-2">
          <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-search" :label="t('watchlist.alerts.check')" data-testid="alert-check" @click="emit('check', a.domain)" />
          <UButton
            v-if="!a.seen"
            color="neutral"
            variant="ghost"
            size="sm"
            icon="i-lucide-check"
            :label="t('watchlist.alerts.markSeen')"
            :loading="busy === `${a.vm}/${a.domain}`"
            :disabled="busy !== null"
            data-testid="alert-seen"
            @click="markSeen(a.vm, a.domain)"
          />
        </div>
      </li>
    </ul>
  </section>
</template>
