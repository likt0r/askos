<script setup lang="ts">
// What a VM tried to reach in the last 24 hours and was refused, newest first, with the
// Signals of its last Alert; "Allow…" adds an Allowlist entry for a chosen time, "Forbid…" a
// Forbid for the VM (refused at once from then on, no hold), except on a row that names the VM's
// own address (a connection with no destination). Of an entry and a Forbid the more specific wins
// (ADR 0024): marks destinations an active entry allows by now, those a Forbid wins for, and those
// the proxy holds for an entry (#39), which appear the moment it holds them.
import type { AllowlistEntry, BlockedDestination, ForbidEntry } from '~/utils/access'

const props = withDefaults(defineProps<{ vm: string, entries: AllowlistEntry[], forbids?: ForbidEntry[] }>(), { forbids: () => [] })
const emit = defineEmits<{ allowed: [], forbidden: [] }>()
const { t } = useI18n()
const tr = useTranslator()
const errorText = useErrorText()
const when = useWhen()
const now = useNow(1000)

const rows = ref<BlockedDestination[]>([])
const loaded = ref(false)
const loading = ref(false)
const error = ref<unknown>(null)
const allowing = ref<string | null>(null)
const allowOpen = computed({
  get: () => allowing.value !== null,
  set: (v: boolean) => { if (!v) allowing.value = null },
})
const forbidding = ref<string | null>(null)
const forbidOpen = computed({
  get: () => forbidding.value !== null,
  set: (v: boolean) => { if (!v) forbidding.value = null },
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

// the more specific of an entry and a Forbid wins (ADR 0024)
function forbiddenNow(domain: string) {
  return !!forbidWins(domain, props.entries, props.forbids, now.value)
}

function allowedBy(domain: string) {
  return !forbiddenNow(domain) && coveredBy(domain, props.entries, now.value)
}

watch(() => props.vm, load)
onMounted(load)
// one reload a second at most, for this VM's events only: a burst gathered across VMs would
// keep only its last event and miss this VM's
let reloading: ReturnType<typeof setTimeout> | null = null
function reloadSoon() {
  if (reloading) clearTimeout(reloading)
  reloading = setTimeout(() => {
    reloading = null
    void load()
  }, 1000)
}
onBeforeUnmount(() => {
  if (reloading) clearTimeout(reloading)
})
useLiveEvent('alert', (a) => {
  if (a?.vm === props.vm) reloadSoon()
}, 0)
useLiveEvent('held', (h) => {
  if (h?.vm === props.vm) reloadSoon()
}, 0)
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
        :key="(allowable(r) ? 'to ' : 'from ') + r.domain"
        class="flex flex-col gap-1.5 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-4"
        data-testid="blocked-row"
        :data-domain="r.domain"
      >
        <div class="min-w-0 flex-1 space-y-1">
          <div class="flex flex-wrap items-center gap-2">
            <span class="break-all font-mono text-sm text-highlighted">{{ r.domain }}</span>
            <WatchlistSignalBadges v-if="r.suspicious.length" :names="r.suspicious" />
            <UBadge v-if="allowedBy(r.domain)" color="success" variant="soft" size="sm" icon="i-lucide-check" :label="t('webTab.blocked.allowedNow')" data-testid="blocked-allowed" />
            <UBadge v-else-if="forbiddenNow(r.domain)" color="error" variant="soft" size="sm" icon="i-lucide-ban" :label="t('webTab.blocked.forbiddenNow')" data-testid="blocked-forbidden" />
            <UBadge v-else-if="heldFor(r, now)" color="warning" variant="soft" size="sm" icon="i-lucide-hourglass" :label="t('webTab.blocked.held', { seconds: heldFor(r, now) })" :title="t('webTab.blocked.heldHint')" data-testid="blocked-held" />
          </div>
          <p class="text-xs text-muted">
            <template v-if="r.count">
              {{ t('webTab.blocked.times', r.count) }} ·
            </template>
            {{ t('webTab.blocked.last', { when: when.ago(r.last, now) }) }} · <span data-testid="blocked-reason" :title="reason(r) !== r.reason ? r.reason : undefined">{{ reason(r) }}</span>
          </p>
        </div>
        <div v-if="allowable(r)" class="flex shrink-0 flex-wrap gap-2 self-start sm:self-center">
          <UButton
            v-if="!allowedBy(r.domain)"
            color="neutral"
            variant="outline"
            size="sm"
            icon="i-lucide-plus"
            :label="t('webTab.blocked.allow')"
            data-testid="blocked-allow"
            @click="allowing = r.domain"
          />
          <UButton
            v-if="!forbiddenNow(r.domain)"
            color="error"
            variant="outline"
            size="sm"
            icon="i-lucide-ban"
            :label="t('webTab.blocked.forbid')"
            data-testid="blocked-forbid"
            @click="forbidding = r.domain"
          />
        </div>
      </li>
    </ul>

    <AllowlistAddDialog
      v-model:open="allowOpen"
      :domain="allowing ?? ''"
      :scope="props.vm"
      :vms="[props.vm]"
      @added="emit('allowed')"
    />
    <AllowlistForbidDialog
      v-model:open="forbidOpen"
      :domain="forbidding ?? ''"
      :vm="props.vm"
      @forbidden="emit('forbidden')"
    />
  </section>
</template>
