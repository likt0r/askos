<script setup lang="ts">
// The state of the Feeds (when each list was last fetched, entries, the last error); the
// hand-maintained Watchlist is HandList.
import type { FeedState, Watchlist } from '~/utils/access'

const props = defineProps<{ watchlist: Watchlist | null, error?: unknown }>()
const { t, te, locale } = useI18n()
const errorText = useErrorText()
const when = useWhen()
const now = useNow(60000)

const FEED_ORDER = ['threat-intelligence', 'newly-registered']
const feeds = computed(() => {
  const f = props.watchlist?.feeds ?? {}
  return Object.keys(f).sort((a, b) => (FEED_ORDER.indexOf(a) + 1 || 99) - (FEED_ORDER.indexOf(b) + 1 || 99))
    .map(name => ({ name, ...f[name]! }))
})

function feedName(name: string) {
  return te(`watchlist.feeds.names.${name}`) ? t(`watchlist.feeds.names.${name}`) : name
}

function feedState(f: FeedState & { name: string }) {
  if (!f.present) return { color: 'error' as const, label: t('watchlist.feeds.missing') }
  if (f.error) return { color: 'warning' as const, label: t('watchlist.feeds.lastFailed') }
  const interval = (props.watchlist?.feed_interval_hours ?? 12) * 3600
  if (f.updated && now.value / 1000 - f.updated > 2 * interval + 3600) return { color: 'warning' as const, label: t('watchlist.feeds.stale') }
  return { color: 'success' as const, label: t('watchlist.feeds.ok') }
}
</script>

<template>
  <section class="space-y-4" data-testid="watchlist-feeds">
    <div>
      <h3 class="font-medium text-highlighted">
        {{ t('watchlist.feeds.title') }}
      </h3>
      <p class="text-sm text-muted">
        {{ t('watchlist.feeds.intro') }}
        <template v-if="props.watchlist">
          {{ t('watchlist.feeds.interval', props.watchlist.feed_interval_hours) }}
        </template>
      </p>
    </div>
    <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(props.error)" data-testid="feeds-error" />

    <div v-if="feeds.length" class="grid gap-3 sm:grid-cols-2">
      <div v-for="f in feeds" :key="f.name" class="space-y-1 rounded-lg border border-default p-3 text-sm" data-testid="feed" :data-feed="f.name">
        <div class="flex flex-wrap items-center gap-2">
          <UIcon name="i-lucide-rss" class="size-4 text-muted" />
          <span class="font-medium text-highlighted">{{ feedName(f.name) }}</span>
          <span class="flex-1" />
          <UBadge :color="feedState(f).color" variant="subtle" size="sm" :label="feedState(f).label" data-testid="feed-state" />
        </div>
        <p v-if="te(`watchlist.feeds.about.${f.name}`)" class="text-muted">
          {{ t(`watchlist.feeds.about.${f.name}`, { days: f.days ?? props.watchlist?.young_domain_days ?? '?' }) }}
        </p>
        <p v-if="f.present && f.entries != null">
          {{ t('watchlist.feeds.entries', { n: f.entries.toLocaleString(locale) }, f.entries) }}
        </p>
        <p class="text-muted">
          {{ f.updated ? t('watchlist.feeds.updated', { when: when.ago(f.updated, now) }) : t('watchlist.feeds.never') }}
        </p>
        <p v-if="f.error" class="break-words text-warning" data-testid="feed-error">
          {{ t('watchlist.feeds.error', { when: when.at(f.attempted) }) }}
          <span class="font-mono text-xs">{{ f.error }}</span>
        </p>
      </div>
    </div>

  </section>
</template>
