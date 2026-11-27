<script setup lang="ts">
// The Watchlist view: Alerts across VMs (mark as seen), checking a domain, Harmless marks,
// the hand-maintained Watchlist and the Legitimate domains (both edited here), and the
// state of the Feeds. Each section asks for its resources by name; the resources module reads
// them again when they change (and the domain check with them).
definePageMeta({ titleKey: 'nav.watchlist' })

const checker = ref<{ check: (d?: string) => Promise<void> } | null>(null)

function check(domain: string) {
  void checker.value?.check(domain)
  if (typeof document !== 'undefined') document.querySelector('[data-testid="domain-check"]')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}
</script>

<template>
  <div class="space-y-8">
    <WatchlistAlertsPanel @check="check" />
    <div class="grid items-start gap-4 lg:grid-cols-2">
      <WatchlistDomainCheck ref="checker" />
      <WatchlistHarmlessList />
    </div>
    <WatchlistHandList />
    <WatchlistLegitimateList />
    <WatchlistFeeds />
  </div>
</template>
