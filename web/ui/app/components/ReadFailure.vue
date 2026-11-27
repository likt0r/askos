<script setup lang="ts">
// Where a failed read of a resource shows (ADR 0026), the same in every view: nothing read yet,
// in place of the section, with Retry; data shown already, a small notice that it could not be
// refreshed, with Retry, and the data stays. Nothing of its own while the live connection is
// down, and for a read that could not reach the Control Room before the stream is live: the
// connection notice says it, and the next `hello` reads again. Nothing for a read the view
// declared an extra.
import type { Resource } from '~/utils/resources'

const props = defineProps<{ resource: Resource<unknown> }>()
const { t } = useI18n()
const errorText = useErrorText()
const live = useLive()

const shown = computed(() => {
  const r = props.resource
  if (r.extra || !r.error.value) return null
  if (live.state.connection === 'reconnecting') return null
  // not before the stream is live, nor while the read again after a `hello` is on the way
  if (unreachable(r.error.value) && (live.state.connection !== 'live' || r.fetching.value)) return null
  return r.data.value === undefined ? 'failed' : 'stale'
})
const retrying = ref(false)

async function retry() {
  retrying.value = true
  try {
    await props.resource.retry()
  }
  finally {
    retrying.value = false
  }
}
</script>

<template>
  <UAlert
    v-if="shown === 'failed'"
    color="error"
    variant="subtle"
    icon="i-lucide-circle-alert"
    :title="t('read.failed', { what: t(`read.resources.${props.resource.name}`, (props.resource.params ?? {}) as Record<string, unknown>) })"
    :description="errorText(props.resource.error.value)"
    :actions="[{ label: t('read.retry'), color: 'error', variant: 'outline', loading: retrying, onClick: retry }]"
    data-read="failed"
  />
  <p v-else-if="shown === 'stale'" class="flex flex-wrap items-center gap-2 text-xs text-warning" data-read="stale">
    <UIcon name="i-lucide-triangle-alert" class="size-4" />
    {{ props.resource.params === undefined ? t('read.stale') : t('read.staleOf', { what: t(`read.resources.${props.resource.name}`, props.resource.params as Record<string, unknown>) }) }}
    <UButton color="warning" variant="link" size="xs" class="p-0" :label="t('read.retry')" :loading="retrying" @click="retry" />
  </p>
</template>
