<script setup lang="ts">
// Alerts of every VM (last 24 hours, harmless destinations left out by the backend), new
// ones first; mark one destination's or a whole VM's as seen. New Alerts arrive as `alert`
// events and reload the list.
import type { AlertGroup, VmAlert } from '~/utils/access'

const emit = defineEmits<{ check: [domain: string] }>()
const { t } = useI18n()
const live = useLive()
const errorText = useErrorText()
const when = useWhen()
const now = useNow(30000)

const alerts = ref<VmAlert[]>([])
const failed = ref<{ vm: string, error: unknown }[]>([])
const loaded = ref(false)
const loading = ref(false)
const onlyNew = ref(false)
const busy = ref<string | null>(null)
const actionError = ref<unknown>(null)
let request = 0

const vmNames = computed(() => live.state.vms.map(v => v.name))
const unseen = computed(() => alerts.value.filter(a => !a.seen))
const shown = computed(() => {
  const list = onlyNew.value ? unseen.value : alerts.value
  return [...list].sort((a, b) => Number(a.seen) - Number(b.seen) || b.last - a.last)
})
const vmsWithNew = computed(() => [...new Set(unseen.value.map(a => a.vm))])

async function load() {
  const names = vmNames.value
  const mine = ++request
  loading.value = true
  const results = await Promise.allSettled(names.map(vm => api<AlertGroup[]>(`/vms/${vm}/alerts`)))
  if (mine !== request) return
  const list: VmAlert[] = []
  const errors: { vm: string, error: unknown }[] = []
  results.forEach((r, i) => {
    const vm = names[i]!
    if (r.status === 'fulfilled') list.push(...(Array.isArray(r.value) ? r.value : []).map(a => ({ ...a, vm })))
    else errors.push({ vm, error: r.reason })
  })
  alerts.value = list
  failed.value = errors
  loaded.value = live.state.loaded
  loading.value = false
}

async function markSeen(vm: string, domain?: string) {
  busy.value = `${vm}/${domain ?? '*'}`
  actionError.value = null
  try {
    await api(`/vms/${vm}/alerts/seen`, { method: 'POST', body: domain ? { domain } : {} })
    await load()
  }
  catch (e) {
    actionError.value = e
  }
  finally {
    busy.value = null
  }
}

async function markAllSeen() {
  for (const vm of vmsWithNew.value) await markSeen(vm)
}

watch(() => `${live.state.loaded}:${vmNames.value.join()}`, () => void load(), { immediate: true })
useLiveEvent('alert', () => void load(), 1000)
defineExpose({ load })
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
    <UAlert v-for="f in failed" :key="f.vm" color="warning" variant="subtle" icon="i-lucide-triangle-alert" :title="t('watchlist.alerts.failed', { vm: f.vm, error: errorText(f.error) })" />
    <UAlert v-if="actionError" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(actionError)" />

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
            data-testid="alert-seen"
            @click="markSeen(a.vm, a.domain)"
          />
        </div>
      </li>
    </ul>
  </section>
</template>
