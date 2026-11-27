<script setup lang="ts">
// An Image's definition (#67): its Containerfile, linked in the repository at the Release tag or
// commit its current Build came from (just the path when that is not known), and on request its
// text from the installed Release, which the next Build takes: read-only, read on the Host by
// agentbox, never fetched from GitHub. The text is read only while the panel is open; a Recipe
// that changes reads it again then (the resources module's rows).
import type { Build, Image } from '~/utils/images'

const props = defineProps<{ image: Image, build?: Build | null }>()
const { t } = useI18n()

// the installed Release has no Containerfile for an Image it no longer defines; a Recipe has its own
const readable = computed(() => props.image.in_release !== false || !!props.image.recipe)
const open = ref(false)
// shown and read while open, and while there is a text to read (a removed Recipe's Image may
// have none)
const shown = computed(() => open.value && readable.value)
const containerfile = useResource('containerfile', { params: () => ({ image: props.image.name }), enabled: shown })
const file = computed(() => containerfile.data.value ?? null)
// where the text shown came from, by its own answer: the Image may have changed since
const fromRecipe = computed(() => !!file.value?.recipe_sha256)

const link = computed(() => githubLink(props.image.containerfile_url))
const fileLink = computed(() => githubLink(file.value?.url))
const panelId = computed(() => `containerfile-${props.image.name}`)

// where the link points: what the current Build came from
const builtFrom = computed(() => {
  const b = props.build
  if (b?.release) return t('system.definition.atRelease', { release: b.release })
  if (b?.commit) return t('system.definition.atCommit', { commit: b.commit.slice(0, 12) })
  return null
})
// the text is the installed Release's (or the current Recipe's); the current Build may have come
// from other files
const differs = computed(() => !!file.value && !!props.build && (fromRecipe.value
  ? !!props.build.recipe_sha256 && props.build.recipe_sha256 !== file.value.recipe_sha256
  : !(props.build.release && props.build.release === file.value.release)))
const differsText = computed(() => {
  const b = props.build
  if (fromRecipe.value) return t('system.definition.differsRecipe')
  if (b?.release) return t('system.definition.differsRelease', { release: b.release })
  if (b?.commit) return t('system.definition.differsCommit', { commit: b.commit.slice(0, 12) })
  return t('system.definition.differsUnknown')
})

function toggle() {
  open.value = !shown.value
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
        :icon="shown ? 'i-lucide-chevron-up' : 'i-lucide-file-text'"
        :label="shown ? t('system.definition.hide') : t('system.definition.show')"
        :aria-expanded="shown"
        :aria-controls="panelId"
        data-testid="containerfile-toggle"
        @click="toggle"
      />
    </div>
    <div v-if="shown" :id="panelId" class="mt-2" data-testid="containerfile">
      <ReadFailure :resource="containerfile" class="mb-2" data-testid="containerfile-error" />
      <p v-if="containerfile.loading.value" class="text-sm text-muted">
        {{ t('system.definition.loading') }}
      </p>
      <template v-else-if="file">
        <p class="mb-1 flex flex-wrap items-center gap-x-2 text-xs text-muted" data-testid="containerfile-source">
          <span>{{ fromRecipe ? t('system.definition.fromRecipe') : file.release ? t('system.definition.fromRelease', { release: file.release }) : t('system.definition.fromInstalled') }}</span>
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
