<script setup lang="ts">
// The backend refused to allow a destination with strong Signals (409 domain_suspicious):
// why (its Signals from check-domain) and that only the CLI on the Host can force it.
import type { DomainCheck } from '~/utils/access'

const props = defineProps<{ domain: string, scope: string, command?: 'allow' | 'extend' | 'make-permanent' }>()
const { t } = useI18n()
const check = ref<DomainCheck | null>(null)

const cli = computed(() => {
  const cmd = props.command ?? 'allow'
  return `sudo agentbox ${cmd} ${props.scope} ${props.domain}${cmd === 'extend' ? ' --for 1h' : ''} --force`
})
const strong = computed(() => check.value?.signals.filter(s => s.strong) ?? [])

watch(() => props.domain, async (domain) => {
  check.value = null
  try {
    check.value = await api<DomainCheck>(`/watchlist/check?domain=${encodeURIComponent(domain)}`)
  }
  catch {
    // the refusal stands on its own; the Signals are extra
  }
}, { immediate: true })
</script>

<template>
  <UAlert color="error" variant="subtle" icon="i-lucide-shield-alert" :title="t('allowlist.refusal.title', { domain: props.domain })" data-testid="refusal">
    <template #description>
      <div class="space-y-2">
        <p>{{ t('allowlist.refusal.text') }}</p>
        <WatchlistSignalList v-if="strong.length" :signals="strong" />
        <p>{{ t('allowlist.refusal.cli') }}</p>
        <code class="block overflow-x-auto whitespace-nowrap rounded bg-elevated px-2 py-1 font-mono text-xs text-highlighted" data-testid="refusal-cli">{{ cli }}</code>
      </div>
    </template>
  </UAlert>
</template>
