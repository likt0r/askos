<script setup lang="ts">
// A VM's journal, read through its guest agent (GET /vms/<vm>/logs, #3): filters for
// unit, priority, since/until, boot, text search and number of lines; "Follow" asks
// for entries after the last cursor every few seconds. Pairing codes are hidden by
// agentbox and hidden here once more.
const props = defineProps<{ name: string, followMs?: number }>()
const { t, locale } = useI18n()
const errorText = useErrorText()

const ANY = '__any__'
const MAX_SHOWN = MAX_LOG_LINES

const filters = reactive<LogFilters>(defaultLogFilters())
const priority = computed({
  get: () => filters.priority || ANY,
  set: (v: string) => (filters.priority = v === ANY ? '' : v),
})
let applied: LogFilters = { ...filters }

const entries = ref<LogEntry[]>([])
const cursor = ref<string | null>(null)
const truncated = ref(false)
const dropped = ref(false)          // older entries left the view while following
const loading = ref(false)
const loaded = ref(false)
const error = ref<string | null>(null)
const follow = ref(false)
const list = ref<HTMLElement | null>(null)

const problems = computed(() => logFilterProblems(filters))
const problem = (field: LogFilterProblem) => problems.value.includes(field)
  ? t(`vm.log.invalid.${field}`, { max: field === 'lines' ? MAX_LOG_LINES : MAX_GREP })
  : undefined

const priorityItems = computed(() => [
  { value: ANY, label: t('vm.log.anyPriority') },
  ...PRIORITIES.map(p => ({ value: p, label: t(`vm.log.priorities.${p}`) })),
])
const bootItems = computed(() => [
  { value: 'current', label: t('vm.log.bootCurrent') },
  { value: 'all', label: t('vm.log.bootAll') },
])

const placeholder = computed(() => t('vm.log.pairingHidden'))
const shown = computed(() => entries.value.map(e => ({
  ...e,
  key: e.cursor ?? `${e.time}-${e.message}`,
  when: formatTime(e.time, locale.value, true) ?? '—',
  level: priorityName(e.priority),
  text: e.message == null ? '' : hidePairingCodes(e.message, placeholder.value),
  source: e.identifier || e.unit || '—',
})))

function levelColor(n: number | null) {
  if (n === null) return 'neutral'
  if (n <= 3) return 'error'
  if (n === 4) return 'warning'
  if (n === 5) return 'info'
  return 'neutral'
}

function nearBottom() {
  const el = list.value
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

async function toBottom() {
  await nextTick()
  const el = list.value
  if (el) el.scrollTop = el.scrollHeight
}

let generation = 0

async function fetchLogs(after: string | null) {
  const gen = generation
  const r = await api<LogResult>(`/vms/${props.name}/logs?${logQuery(applied, after)}`)
  if (gen !== generation) return null       // filters changed or the view left meanwhile
  return r
}

/** Show: the newest entries matching the filters. */
async function show() {
  if (problems.value.length) return
  generation += 1
  applied = { ...filters }
  loading.value = true
  error.value = null
  try {
    const r = await fetchLogs(null)
    if (!r) return
    entries.value = Array.isArray(r.entries) ? r.entries : []
    cursor.value = r.cursor ?? entries.value.at(-1)?.cursor ?? null
    truncated.value = r.truncated === true
    dropped.value = false
    loaded.value = true
    await toBottom()
  }
  catch (e) {
    error.value = errorText(e)
    follow.value = false
  }
  finally {
    loading.value = false
  }
}

let polling = false
async function poll() {
  if (polling || loading.value || !follow.value) return
  polling = true
  try {
    if (!cursor.value) {
      await show()
      return
    }
    const stick = nearBottom()
    const r = await fetchLogs(cursor.value)
    if (!r || !follow.value) return
    const fresh = Array.isArray(r.entries) ? r.entries : []
    if (fresh.length) {
      const all = [...entries.value, ...fresh]
      if (all.length > MAX_SHOWN) {
        all.splice(0, all.length - MAX_SHOWN)
        dropped.value = true
      }
      entries.value = all
      cursor.value = r.cursor ?? fresh.at(-1)?.cursor ?? cursor.value
      if (stick) await toBottom()
    }
    // more new entries than one answer holds: the next poll fetches the rest
    error.value = null
  }
  catch (e) {
    error.value = errorText(e)
    follow.value = false
  }
  finally {
    polling = false
  }
}

let timer: ReturnType<typeof setInterval> | null = null
watch(follow, (on) => {
  if (timer) clearInterval(timer)
  timer = null
  if (on) {
    timer = setInterval(poll, props.followMs ?? 3000)
    void poll()
  }
})

function clearFilters() {
  Object.assign(filters, defaultLogFilters())
}

onMounted(show)
watch(() => props.name, () => {
  follow.value = false
  entries.value = []
  void show()
})
onBeforeUnmount(() => {
  generation += 1
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="space-y-4" data-testid="log-view">
    <form class="grid gap-3 rounded-lg border border-default bg-default p-4 sm:grid-cols-2 lg:grid-cols-4" novalidate data-testid="log-filters" @submit.prevent="show">
      <UFormField :label="t('vm.log.unit')" :error="problem('unit')">
        <UInput v-model="filters.unit" :placeholder="t('vm.log.unitPlaceholder')" autocomplete="off" autocapitalize="off" spellcheck="false" class="w-full font-mono" data-testid="log-unit" />
      </UFormField>
      <UFormField :label="t('vm.log.priority')" :help="t('vm.log.priorityHelp')">
        <USelect v-model="priority" :items="priorityItems" class="w-full" data-testid="log-priority" />
      </UFormField>
      <UFormField :label="t('vm.log.since')" :error="problem('since')">
        <UInput v-model="filters.since" :placeholder="t('vm.log.timePlaceholder')" autocomplete="off" class="w-full font-mono" data-testid="log-since" />
      </UFormField>
      <UFormField :label="t('vm.log.until')" :error="problem('until')">
        <UInput v-model="filters.until" :placeholder="t('vm.log.timePlaceholder')" autocomplete="off" class="w-full font-mono" data-testid="log-until" />
      </UFormField>
      <UFormField :label="t('vm.log.boot')">
        <USelect v-model="filters.boot" :items="bootItems" class="w-full" data-testid="log-boot" />
      </UFormField>
      <UFormField :label="t('vm.log.grep')" :error="problem('grep')" class="sm:col-span-1 lg:col-span-2">
        <UInput v-model="filters.grep" icon="i-lucide-search" :maxlength="MAX_GREP" autocomplete="off" spellcheck="false" class="w-full font-mono" data-testid="log-grep" />
      </UFormField>
      <UFormField :label="t('vm.log.lines')" :error="problem('lines')">
        <UInput v-model="filters.lines" inputmode="numeric" class="w-full" data-testid="log-lines" />
      </UFormField>
      <p class="text-xs text-muted sm:col-span-2 lg:col-span-4">
        {{ t('vm.log.timeHelp') }}
      </p>
      <div class="flex flex-wrap items-center gap-3 sm:col-span-2 lg:col-span-4">
        <UButton type="submit" icon="i-lucide-refresh-cw" :label="t('vm.log.show')" :loading="loading" data-testid="log-show" />
        <UButton color="neutral" variant="ghost" :label="t('vm.log.clear')" data-testid="log-clear" @click="clearFilters" />
        <span class="flex-1" />
        <USwitch v-model="follow" :label="t('vm.log.follow')" :disabled="!loaded && !error" data-testid="log-follow" />
      </div>
    </form>

    <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="error" data-testid="log-error" />
    <UAlert v-if="truncated" color="warning" variant="subtle" icon="i-lucide-scissors" :title="t('vm.log.truncated')" data-testid="log-truncated" />

    <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
      <span v-if="loaded" data-testid="log-count">{{ t('vm.log.count', entries.length) }}</span>
      <span v-if="dropped">{{ t('vm.log.dropped', { max: MAX_SHOWN }) }}</span>
      <span v-if="follow" class="inline-flex items-center gap-1 text-success" data-testid="log-following">
        <UIcon name="i-lucide-radio" class="size-3.5" />{{ t('vm.log.following') }}
      </span>
      <span class="inline-flex items-center gap-1">
        <UIcon name="i-lucide-eye-off" class="size-3.5" />{{ t('vm.log.codesHidden') }}
      </span>
    </div>

    <p v-if="loaded && !entries.length && !error" class="text-sm text-muted" data-testid="log-empty">
      {{ t('vm.log.empty') }}
    </p>
    <div
      v-if="entries.length"
      ref="list"
      class="max-h-[70svh] overflow-y-auto rounded-lg border border-default bg-default font-mono text-xs"
      data-testid="log-entries"
    >
      <ol class="divide-y divide-default">
        <li
          v-for="e in shown"
          :key="e.key"
          class="grid gap-x-3 gap-y-0.5 px-3 py-1.5 sm:grid-cols-[11rem_5rem_minmax(6rem,12rem)_1fr]"
          data-testid="log-entry"
        >
          <span class="text-muted whitespace-nowrap">{{ e.when }}</span>
          <span>
            <UBadge v-if="e.level" :color="levelColor(e.priority)" variant="soft" size="sm" :label="e.level" />
          </span>
          <span class="truncate text-muted" :title="e.unit ?? undefined">{{ e.source }}</span>
          <span class="whitespace-pre-wrap break-words text-default" data-testid="log-message">{{ e.text }}</span>
        </li>
      </ol>
    </div>
  </div>
</template>
