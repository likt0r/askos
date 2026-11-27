<script setup lang="ts">
// The journal lines of a Build, newest at the bottom; follows the end while one reads there. The
// resource `buildLog`: `build-log` events append to it, the log endpoint fills what they could not
// carry (lines skipped, a reconnect that could not resume, the end of the Build).
const props = defineProps<{ image: string, build: string, running?: boolean }>()
const { t, locale } = useI18n()
const log = useResource('buildLog', { params: () => ({ image: props.image, build: props.build }) })
const lines = computed(() => log.data.value?.lines ?? [])
const box = ref<HTMLElement | null>(null)
let follow = true

function onScroll() {
  const el = box.value
  if (el) follow = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

watch(() => lines.value.length, async () => {
  await nextTick()
  const el = box.value
  if (el && follow) el.scrollTop = el.scrollHeight
}, { flush: 'post' })
</script>

<template>
  <div data-testid="build-log">
    <ReadFailure :resource="log" class="mb-1" data-testid="build-log-error" />
    <p v-if="!lines.length && !log.error.value" class="text-sm text-muted">
      {{ log.loading.value ? t('system.log.loading') : running ? t('system.log.waiting') : t('system.log.empty') }}
    </p>
    <div
      v-else-if="lines.length"
      ref="box"
      class="max-h-72 overflow-auto rounded-md bg-elevated p-3 font-mono text-xs leading-relaxed text-default"
      tabindex="0"
      role="log"
      :aria-label="t('system.log.label')"
      data-testid="build-log-lines"
      @scroll.passive="onScroll"
    >
      <div v-for="l in lines" :key="l.cursor" class="whitespace-pre-wrap break-words" data-testid="build-log-line">
        <span class="select-none text-dimmed">{{ formatClock(l.time, locale) }}</span> {{ l.message }}
      </div>
    </div>
  </div>
</template>
