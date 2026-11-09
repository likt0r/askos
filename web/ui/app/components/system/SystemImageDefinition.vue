<script setup lang="ts">
// An Image's definition (#67): its Containerfile, linked in the repository at the Release tag or
// commit its current Build came from (just the path when that is not known), and on request its
// text from the installed Release, which the next Build takes: read-only, read on the Host by
// agentbox, never fetched from GitHub.
import type { Build, ContainerfileText, Image } from '~/utils/images'

const props = defineProps<{ image: Image, build?: Build | null }>()
const { t } = useI18n()
const errorText = useErrorText()

const open = ref(false)
const loading = ref(false)
const error = ref<unknown>(null)
const file = ref<ContainerfileText | null>(null)

const link = computed(() => githubLink(props.image.containerfile_url))
const fileLink = computed(() => githubLink(file.value?.url))
// the installed Release has no Containerfile for an Image it no longer defines
const readable = computed(() => props.image.in_release !== false)
const panelId = computed(() => `containerfile-${props.image.name}`)

// where the link points: what the current Build came from
const builtFrom = computed(() => {
  const b = props.build
  if (b?.release) return t('system.definition.atRelease', { release: b.release })
  if (b?.commit) return t('system.definition.atCommit', { commit: b.commit.slice(0, 12) })
  return null
})
// the text is the installed Release's; the current Build may have come from other files
const differs = computed(() => !!file.value && !!props.build
  && !(props.build.release && props.build.release === file.value.release))
const differsText = computed(() => {
  const b = props.build
  if (b?.release) return t('system.definition.differsRelease', { release: b.release })
  if (b?.commit) return t('system.definition.differsCommit', { commit: b.commit.slice(0, 12) })
  return t('system.definition.differsUnknown')
})

async function load() {
  loading.value = true
  try {
    file.value = await api<ContainerfileText>(`/images/${encodeURIComponent(props.image.name)}/containerfile`)
    error.value = null
  }
  catch (e) {
    error.value = e
  }
  finally {
    loading.value = false
  }
}

function toggle() {
  open.value = !open.value
  // read once; after a failure again at the next opening
  if (open.value && !file.value && !loading.value) void load()
}
</script>

<template>
  <div class="px-4 pt-3 text-sm" data-testid="image-definition">
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span class="text-muted">{{ t('system.definition.label') }}:</span>
      <a
        v-if="link"
        :href="link"
        target="_blank"
        rel="noopener noreferrer"
        class="inline-flex items-center gap-1 font-mono text-highlighted hover:underline"
        data-testid="containerfile-link"
      >{{ image.containerfile }}<UIcon name="i-lucide-external-link" class="size-3.5 shrink-0" /></a>
      <span v-else class="font-mono text-highlighted" data-testid="containerfile-path">{{ image.containerfile }}</span>
      <span v-if="link && builtFrom" class="text-xs text-muted" data-testid="containerfile-ref">{{ builtFrom }}</span>
      <span class="flex-1" />
      <UButton
        v-if="readable"
        size="xs"
        color="neutral"
        variant="ghost"
        :icon="open ? 'i-lucide-chevron-up' : 'i-lucide-file-text'"
        :label="open ? t('system.definition.hide') : t('system.definition.show')"
        :aria-expanded="open"
        :aria-controls="panelId"
        data-testid="containerfile-toggle"
        @click="toggle"
      />
    </div>
    <div v-if="open" :id="panelId" class="mt-2" data-testid="containerfile">
      <p v-if="error" class="text-sm text-error" data-testid="containerfile-error">
        {{ errorText(error) }}
      </p>
      <p v-else-if="!file" class="text-sm text-muted">
        {{ t('system.definition.loading') }}
      </p>
      <template v-else>
        <p class="mb-1 flex flex-wrap items-center gap-x-2 text-xs text-muted" data-testid="containerfile-source">
          <span>{{ file.release ? t('system.definition.fromRelease', { release: file.release }) : t('system.definition.fromInstalled') }}</span>
          <a
            v-if="fileLink"
            :href="fileLink"
            target="_blank"
            rel="noopener noreferrer"
            class="inline-flex items-center gap-1 text-highlighted hover:underline"
            data-testid="containerfile-file-link"
          >{{ t('system.definition.onGithub') }}<UIcon name="i-lucide-external-link" class="size-3 shrink-0" /></a>
        </p>
        <p v-if="differs" class="mb-1 text-xs text-warning" data-testid="containerfile-differs">
          {{ differsText }}
        </p>
        <pre
          class="max-h-96 overflow-auto rounded-md bg-elevated p-3 font-mono text-xs leading-relaxed text-default"
          tabindex="0"
          :aria-label="t('system.definition.textLabel', { path: file.path })"
          data-testid="containerfile-text"
        >{{ file.text }}</pre>
        <p v-if="file.truncated" class="mt-1 text-xs text-muted" data-testid="containerfile-truncated">
          {{ t('system.definition.truncated', { size: file.size }) }}
        </p>
      </template>
    </div>
  </div>
</template>
