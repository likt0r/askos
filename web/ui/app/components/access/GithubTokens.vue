<script setup lang="ts">
// The GitHub tokens a VM got from the token service, per repository, and the requests that
// were refused (the token itself is never shown or stored here). New ones arrive as
// `github-token` events.
import type { GithubTokens } from '~/utils/access'

const props = defineProps<{ vm: string }>()
const { t } = useI18n()
// why a request was refused in the UI language (#69); the token service's English text on hover
const tr = useTranslator()
const reason = (r: GithubTokens['requests'][number]) => tokenReason(r, tr)
const errorText = useErrorText()
const when = useWhen()
const now = useNow(60000)

const PERIODS = { '-24h': 'day', '-7d': 'week', '-30d': 'month' } as const
const period = ref<string>('-7d')
const data = ref<GithubTokens | null>(null)
const loading = ref(false)
const error = ref<unknown>(null)
let request = 0

const periods = computed(() => Object.entries(PERIODS).map(([value, key]) => ({ value, label: t(`access.tokens.periods.${key}`) })))
const refused = computed(() => data.value?.requests.filter(r => r.outcome !== 'issued') ?? [])
const issued = computed(() => data.value?.repositories.filter(r => r.tokens > 0) ?? [])

async function load() {
  const mine = ++request
  loading.value = true
  try {
    const r = await api<GithubTokens>(`/vms/${props.vm}/github-tokens?since=${encodeURIComponent(period.value)}`)
    if (mine !== request) return
    data.value = r
    error.value = null
  }
  catch (e) {
    if (mine === request) error.value = e
  }
  finally {
    if (mine === request) loading.value = false
  }
}

watch([() => props.vm, period], load)
onMounted(load)
useLiveEvent('github-token', (e) => {
  if (e?.vm === props.vm) void load()
}, 1000)
</script>

<template>
  <section class="space-y-4 rounded-lg border border-default p-4" data-testid="github-tokens">
    <div class="flex flex-wrap items-start gap-2">
      <div class="min-w-0 flex-1">
        <h3 class="font-medium text-highlighted">
          {{ t('access.tokens.title') }}
        </h3>
        <p class="text-sm text-muted">
          {{ t('access.tokens.intro') }}
        </p>
      </div>
      <USelect v-model="period" :items="periods" size="sm" class="w-40" :aria-label="t('access.tokens.period')" data-testid="tokens-period" />
      <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-refresh-cw" :loading="loading" :aria-label="t('access.common.refresh')" @click="load" />
    </div>
    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" />
    <p v-if="!data && !error" class="text-sm text-muted">
      {{ t('access.common.loading') }}
    </p>

    <template v-if="data">
      <div class="space-y-2">
        <h4 class="text-sm font-medium text-highlighted">
          {{ t('access.tokens.repositories') }}
        </h4>
        <p v-if="!issued.length" class="text-sm text-muted" data-testid="tokens-none">
          {{ t('access.tokens.none') }}
        </p>
        <ul v-else class="divide-y divide-default rounded-md border border-default">
          <li v-for="r in issued" :key="r.repository" class="flex flex-col gap-0.5 px-3 py-2 sm:flex-row sm:items-center sm:gap-3" data-testid="token-repo" :data-repository="r.repository">
            <span class="flex min-w-0 flex-1 items-center gap-2">
              <UIcon name="i-lucide-folder-git-2" class="size-4 shrink-0 text-muted" />
              <span class="break-all font-mono text-sm text-highlighted">{{ r.repository }}</span>
            </span>
            <span class="text-xs text-muted">
              {{ t('access.tokens.count', r.tokens) }} · {{ t('access.tokens.last', { when: when.ago(r.last_token, now) }) }}
              <template v-if="r.refused"> · <span class="text-warning">{{ t('access.tokens.refusedCount', r.refused) }}</span></template>
            </span>
          </li>
        </ul>
      </div>

      <div class="space-y-2">
        <h4 class="text-sm font-medium text-highlighted">
          {{ t('access.tokens.refused') }}
        </h4>
        <p v-if="!refused.length" class="text-sm text-muted" data-testid="refused-none">
          {{ t('access.tokens.noneRefused') }}
        </p>
        <ul v-else class="divide-y divide-default rounded-md border border-warning/40">
          <li v-for="(r, i) in refused" :key="`${r.time}-${i}`" class="space-y-0.5 px-3 py-2 text-sm" data-testid="token-refused" :data-outcome="r.outcome">
            <div class="flex flex-wrap items-center gap-2">
              <UBadge :color="r.outcome === 'error' ? 'error' : 'warning'" variant="subtle" size="sm" :label="t(`access.tokens.outcome.${r.outcome}`)" />
              <span class="break-all font-mono text-highlighted">{{ r.request ?? r.repository ?? '?' }}</span>
              <span class="text-xs text-muted">{{ when.at(r.time) }}</span>
            </div>
            <p v-if="reason(r)" class="break-words text-xs text-muted" data-testid="token-reason" :title="reason(r) !== r.reason ? r.reason ?? undefined : undefined">
              {{ reason(r) }}
            </p>
          </li>
        </ul>
      </div>
      <p v-if="data.truncated" class="text-xs text-muted">
        {{ t('access.tokens.truncated') }}
      </p>
    </template>
  </section>
</template>
