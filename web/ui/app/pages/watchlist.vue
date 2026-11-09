<script setup lang="ts">
// The Watchlist view: Alerts across VMs (mark as seen), checking a domain, Harmless marks,
// the hand-maintained Watchlist and the Legitimate domains (both edited here), and the
// state of the Feeds.
import type { HarmlessMark, LegitimateDomain, Watchlist } from '~/utils/access'

definePageMeta({ titleKey: 'nav.watchlist' })

const harmless = ref<HarmlessMark[]>([])
const harmlessLoaded = ref(false)
const harmlessError = ref<unknown>(null)
const watchlist = ref<Watchlist | null>(null)
const watchlistError = ref<unknown>(null)
const legitimate = ref<LegitimateDomain[]>([])
const legitimateLoaded = ref(false)
const legitimateError = ref<unknown>(null)
const checker = ref<{ check: (d?: string) => Promise<void> } | null>(null)
const alerts = ref<{ load: () => Promise<void> } | null>(null)

async function loadHarmless() {
  try {
    const list = await api<HarmlessMark[]>('/watchlist/harmless')
    harmless.value = Array.isArray(list) ? list.filter(m => m && typeof m === 'object') : []
    harmlessError.value = null
    harmlessLoaded.value = true
  }
  catch (e) {
    harmlessError.value = e
  }
}

async function loadWatchlist() {
  try {
    watchlist.value = await api<Watchlist>('/watchlist')
    watchlistError.value = null
  }
  catch (e) {
    watchlistError.value = e
  }
}

async function loadLegitimate() {
  try {
    const list = await api<LegitimateDomain[]>('/watchlist/legitimate')
    legitimate.value = Array.isArray(list) ? list : []
    legitimateError.value = null
    legitimateLoaded.value = true
  }
  catch (e) {
    legitimateError.value = e
  }
}

/** a Watchlist entry or a Legitimate domain changed: the domain check may say something else now */
function listsChanged(load: () => Promise<void>) {
  void load()
  void checker.value?.check()
}

/** a Harmless mark changed: Alerts of that destination appear or disappear */
function harmlessChanged() {
  void loadHarmless()
  void alerts.value?.load()
}

function check(domain: string) {
  void checker.value?.check(domain)
  if (typeof document !== 'undefined') document.querySelector('[data-testid="domain-check"]')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}

onMounted(() => {
  void loadHarmless()
  void loadWatchlist()
  void loadLegitimate()
})
</script>

<template>
  <div class="space-y-8">
    <WatchlistAlertsPanel ref="alerts" @check="check" />
    <div class="grid items-start gap-4 lg:grid-cols-2">
      <WatchlistDomainCheck ref="checker" :harmless="harmless" @harmless-changed="harmlessChanged" />
      <WatchlistHarmlessList :marks="harmless" :loaded="harmlessLoaded" :error="harmlessError" @changed="harmlessChanged" />
    </div>
    <WatchlistHandList :watchlist="watchlist" :error="watchlistError" @changed="listsChanged(loadWatchlist)" />
    <WatchlistLegitimateList :domains="legitimate" :loaded="legitimateLoaded" :error="legitimateError" @changed="listsChanged(loadLegitimate)" />
    <WatchlistFeeds :watchlist="watchlist" :error="watchlistError" />
  </div>
</template>
