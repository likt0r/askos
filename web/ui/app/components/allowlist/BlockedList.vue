<script setup lang="ts">
// What a VM tried to reach in the last 24 hours and was refused, newest first, with the
// Signals of its last Alert; "Allow…" adds an Allowlist entry for a chosen time. Marks
// destinations an active entry covers by now.
import type { AllowlistEntry, BlockedDestination } from '~/utils/access'

const props = defineProps<{ vm: string, entries: AllowlistEntry[] }>()
const emit = defineEmits<{ allowed: [] }>()
const { t } = useI18n()
const tr = useTranslator()
const errorText = useErrorText()
const when = useWhen()
const now = useNow(30000)

const rows = ref<BlockedDestination[]>([])
const loaded = ref(false)
const loading = ref(false)
const error = ref<unknown>(null)
const allowing = ref<string | null>(null)
const allowOpen = computed({
  get: () => allowing.value !== null,
  set: (v: boolean) => { if (!v) allowing.value = null },
})

async function load() {
  loading.value = true
  try {
    const list = await api<BlockedDestination[]>(`/vms/${props.vm}/blocked`)
    rows.value = Array.isArray(list) ? list : []
    error.value = null
    loaded.value = true
  }
  catch (e) {
    error.value = e
  }
  finally {
    loading.value = false
  }
}

// why it was blocked in the UI language (#69); the proxy's English reason on hover
function reason(r: BlockedDestination) {
  return blockedReason(r, tr)
}

function allowedBy(domain: string) {
  return coveredBy(domain, props.entries, now.value)
}

watch(() => props.vm, load)
onMounted(load)
useLiveEvent('alert', (a) => {
  if (a?.vm === props.vm) void load()
}, 1000)
</script>

<template>
  <section class="space-y-2" data-testid="blocked">
    <div class="flex items-center gap-2">
      <h3 class="font-medium text-highlighted">
        {{ t('webTab.blocked.title') }}
      </h3>
      <span v-if="loaded" class="text-sm text-muted">{{ t('webTab.blocked.count', rows.length) }}</span>
      <span class="flex-1" />
      <UButton color="neutral" variant="ghost" size="sm" icon="i-lucide-refresh-cw" :loading="loading" :aria-label="t('access.common.refresh')" @click="load" />
    </div>
    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" />
    <p v-if="!loaded && !error" class="text-sm text-muted">
      {{ t('access.common.loading') }}
    </p>
    <p v-else-if="loaded && !rows.length" class="text-sm text-muted" data-testid="blocked-empty">
      {{ t('webTab.blocked.empty') }}
    </p>
    <ul v-else-if="rows.length" class="divide-y divide-default rounded-lg border border-default bg-default">
      <li
        v-for="r in rows"
        :key="r.domain"
        class="flex flex-col gap-1.5 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-4"
        data-testid="blocked-row"
        :data-domain="r.domain"
      >
        <div class="min-w-0 flex-1 space-y-1">
          <div class="flex flex-wrap items-center gap-2">
            <span class="break-all font-mono text-sm text-highlighted">{{ r.domain }}</span>
            <WatchlistSignalBadges v-if="r.suspicious.length" :names="r.suspicious" />
            <UBadge v-if="allowedBy(r.domain)" color="success" variant="soft" size="sm" icon="i-lucide-check" :label="t('webTab.blocked.allowedNow')" data-testid="blocked-allowed" />
          </div>
          <p class="text-xs text-muted">
            {{ t('webTab.blocked.times', r.count) }} · {{ t('webTab.blocked.last', { when: when.ago(r.last, now) }) }} · <span data-testid="blocked-reason" :title="reason(r) !== r.reason ? r.reason : undefined">{{ reason(r) }}</span>
          </p>
        </div>
        <UButton
          v-if="!allowedBy(r.domain)"
          class="self-start sm:self-center"
          color="neutral"
          variant="outline"
          size="sm"
          icon="i-lucide-plus"
          :label="t('webTab.blocked.allow')"
          data-testid="blocked-allow"
          @click="allowing = r.domain"
        />
      </li>
    </ul>

    <AllowlistAddDialog
      v-model:open="allowOpen"
      :domain="allowing ?? ''"
      :scope="props.vm"
      :vms="[props.vm]"
      @added="emit('allowed')"
    />
  </section>
</template>
