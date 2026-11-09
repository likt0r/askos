<script setup lang="ts">
// Open web for one VM: any public domain for a limited time, suspicious destinations
// still blocked; open it for a chosen time (at most the Settings' longest Open web) or
// close it early.
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const live = useLive()
const errorText = useErrorText()
const toast = useToast()
const when = useWhen()
const now = useNow(5000)

const CHOICES = [15, 30, 60, 120, 240]
const longest = ref<number | null>(null)
const minutes = ref(30)
const busy = ref(false)
const error = ref<unknown>(null)
/** ms since epoch from our own action, until the VM object catches up; 0: closed */
const override = ref<number | null>(null)

const choices = computed(() => {
  const max = longest.value
  if (!max) return CHOICES
  const list = CHOICES.filter(m => m <= max)
  return list.length ? list : [max]
})
const until = computed(() => {
  if (override.value !== null) return override.value
  const v = props.vm.open_web_until
  const ms = v ? Date.parse(v) : NaN
  return Number.isFinite(ms) ? ms : 0
})
const isOpen = computed(() => until.value > now.value)

watch(() => props.vm.open_web_until, () => { override.value = null })
watch(choices, (list) => {
  if (!list.includes(minutes.value)) minutes.value = list.includes(30) ? 30 : list[0]!
})

onMounted(async () => {
  try {
    const s = await api<{ settings: Record<string, { value: unknown }> }>('/settings')
    const v = s?.settings?.open_web_max_minutes?.value
    if (typeof v === 'number' && v > 0) longest.value = v
  }
  catch {
    // the backend checks the limit anyway
  }
})

async function openWeb() {
  busy.value = true
  error.value = null
  try {
    const r = await api<{ open_until?: number }>(`/vms/${props.vm.name}/open-web`, { method: 'POST', body: { minutes: minutes.value } })
    override.value = typeof r?.open_until === 'number' ? r.open_until * 1000 : Date.now() + minutes.value * 60000
    toast.add({ color: 'success', icon: 'i-lucide-globe', title: t('webTab.openWeb.opened', { vm: props.vm.name, until: when.at(override.value / 1000) }) })
    void live.refetchVms()
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}

async function closeWeb() {
  busy.value = true
  error.value = null
  try {
    await api(`/vms/${props.vm.name}/open-web`, { method: 'DELETE' })
    override.value = 0
    toast.add({ color: 'success', icon: 'i-lucide-globe-lock', title: t('webTab.openWeb.closed', { vm: props.vm.name }) })
    void live.refetchVms()
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="rounded-lg border border-default p-4" :class="isOpen ? 'border-warning/60 bg-warning/5' : ''" data-testid="open-web" :data-open="isOpen">
    <div class="flex flex-wrap items-start gap-3">
      <UIcon :name="isOpen ? 'i-lucide-globe' : 'i-lucide-globe-lock'" class="mt-0.5 size-5 shrink-0" :class="isOpen ? 'text-warning' : 'text-muted'" />
      <div class="min-w-0 flex-1 space-y-1">
        <h3 class="font-medium text-highlighted">
          {{ t('webTab.openWeb.title') }}
        </h3>
        <p v-if="isOpen" class="text-sm" data-testid="open-web-state">
          {{ t('webTab.openWeb.openUntil', { until: when.at(until / 1000), span: when.span((until - now) / 1000) }) }}
        </p>
        <p class="text-sm text-muted">
          {{ t('webTab.openWeb.explain') }}
        </p>
      </div>
      <UButton
        v-if="isOpen"
        color="warning"
        variant="solid"
        icon="i-lucide-globe-lock"
        :label="t('webTab.openWeb.close')"
        :loading="busy"
        data-testid="open-web-close"
        @click="closeWeb"
      />
    </div>
    <div v-if="!isOpen" class="mt-3 flex flex-wrap items-center gap-2">
      <span class="text-sm text-muted">{{ t('webTab.openWeb.for') }}</span>
      <div class="flex flex-wrap gap-1" role="radiogroup" :aria-label="t('webTab.openWeb.for')">
        <UButton
          v-for="m in choices"
          :key="m"
          role="radio"
          :aria-checked="minutes === m"
          size="sm"
          :color="minutes === m ? 'primary' : 'neutral'"
          :variant="minutes === m ? 'solid' : 'outline'"
          :label="when.span(m * 60)"
          :data-minutes="m"
          @click="minutes = m"
        />
      </div>
      <UButton icon="i-lucide-globe" :label="t('webTab.openWeb.open')" :loading="busy" data-testid="open-web-open" @click="openWeb" />
    </div>
    <UAlert v-if="error" class="mt-3" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
  </section>
</template>
