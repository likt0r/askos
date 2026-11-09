<script setup lang="ts">
// Harmless marks: destinations the owner judged false positives. They raise no more Alerts
// (except for Signals that do not depend on the name); blocking stays as it is.
const props = defineProps<{ marks: string[], loaded: boolean, error?: unknown }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const errorText = useErrorText()
const toast = useToast()

const input = ref('')
const busy = ref(false)
const error = ref<unknown>(null)
const removing = ref<string | null>(null)
const removeOpen = computed({
  get: () => removing.value !== null,
  set: (v: boolean) => { if (!v && !busy.value) removing.value = null },
})

const domain = computed(() => normalizeDomain(input.value))
const valid = computed(() => looksLikeDomain(domain.value))

async function add() {
  if (!valid.value) return
  busy.value = true
  error.value = null
  try {
    await api(`/watchlist/harmless/${encodeURIComponent(domain.value)}`, { method: 'PUT' })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('watchlist.harmless.marked', { domain: domain.value }) })
    input.value = ''
    emit('changed')
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}

async function remove() {
  const d = removing.value
  if (!d) return
  busy.value = true
  error.value = null
  try {
    await api(`/watchlist/harmless/${encodeURIComponent(d)}`, { method: 'DELETE' })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('watchlist.harmless.unmarked', { domain: d }) })
    busy.value = false
    removing.value = null
    emit('changed')
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
  <section class="space-y-3 rounded-lg border border-default p-4" data-testid="harmless">
    <div>
      <h3 class="font-medium text-highlighted">
        {{ t('watchlist.harmless.title') }}
      </h3>
      <p class="text-sm text-muted">
        {{ t('watchlist.harmless.intro') }}
      </p>
    </div>
    <form class="flex flex-wrap gap-2" novalidate @submit.prevent="add">
      <UInput
        v-model="input"
        placeholder="false-positive.example"
        autocomplete="off"
        spellcheck="false"
        class="min-w-0 flex-1 font-mono"
        :aria-label="t('watchlist.harmless.domain')"
        data-testid="harmless-input"
      />
      <UButton type="submit" icon="i-lucide-bell-off" :label="t('watchlist.harmless.mark')" :loading="busy && !removing" :disabled="!valid" data-testid="harmless-add" />
    </form>
    <UAlert v-if="props.error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(props.error)" />
    <UAlert v-if="error && !removing" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
    <p v-if="props.loaded && !props.marks.length" class="text-sm text-muted" data-testid="harmless-empty">
      {{ t('watchlist.harmless.empty') }}
    </p>
    <ul v-else-if="props.marks.length" class="divide-y divide-default rounded-md border border-default">
      <li v-for="m in props.marks" :key="m" class="flex items-center gap-2 px-3 py-1.5" data-testid="harmless-row" :data-domain="m">
        <span class="min-w-0 flex-1 break-all font-mono text-sm">{{ m }}</span>
        <UButton
          color="error"
          variant="ghost"
          size="sm"
          icon="i-lucide-trash-2"
          :aria-label="t('watchlist.harmless.unmarkOne', { domain: m })"
          data-testid="harmless-remove"
          @click="removing = m"
        />
      </li>
    </ul>

    <AccessConfirmDialog
      v-model:open="removeOpen"
      :title="t('watchlist.harmless.unmarkTitle', { domain: removing })"
      :description="t('watchlist.harmless.unmarkText')"
      :confirm-label="t('watchlist.harmless.unmark')"
      :busy="busy"
      :error="error && removing ? errorText(error) : null"
      @confirm="remove"
    />
  </section>
</template>
