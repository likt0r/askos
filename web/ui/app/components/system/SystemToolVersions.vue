<script setup lang="ts">
// The versions of the tools in a Build next to what is available (#104): one row per tool,
// marked where a new Build would change it, Orca marked when the Settings pin its version
// (#113); and the Fedora package updates when they were counted for this Build.
import type { Available, ToolRow } from '~/utils/images'

const props = defineProps<{
  rows: ToolRow[]
  available: Available | null | undefined
  /** heading of the column of the Build's versions */
  installedLabel: string
  /** Fedora package updates of the Build; undefined: no such row */
  packages?: number | null
}>()
const { t } = useI18n()

const orcaPinned = computed(() => props.available?.orca_version ?? null)
</script>

<template>
  <div class="space-y-1" data-testid="tool-versions">
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-xs text-dimmed">
            <th class="py-1 pe-4 font-medium">
              {{ t('system.versions.tool') }}
            </th>
            <th class="py-1 pe-4 font-medium">
              {{ installedLabel }}
            </th>
            <th class="py-1 font-medium">
              {{ t('system.versions.available') }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="r in props.rows"
            :key="r.tool"
            data-testid="tool-row"
            :data-tool="r.tool"
            :data-update="r.update ? 'true' : 'false'"
          >
            <td class="py-0.5 pe-4">
              {{ r.name }}
            </td>
            <td class="py-0.5 pe-4 font-mono text-xs" data-testid="tool-installed">
              {{ r.installed ?? t('system.versions.unknown') }}
            </td>
            <td class="py-0.5 text-xs" data-testid="tool-available">
              <span class="inline-flex flex-wrap items-center gap-x-2">
                <span v-if="r.tool === 'fedora'" class="text-dimmed">—</span>
                <span v-else class="font-mono">{{ r.available ?? t('system.versions.unknown') }}</span>
                <span v-if="r.tool === 'orca' && orcaPinned" class="text-muted" data-testid="orca-pinned">{{ t('system.versions.pinned') }}</span>
                <UBadge v-if="r.update" color="warning" variant="subtle" size="sm" :label="t('system.versions.update')" />
              </span>
            </td>
          </tr>
          <tr v-if="props.packages !== undefined" data-testid="tool-packages">
            <td class="py-0.5 pe-4">
              {{ t('system.versions.packages') }}
            </td>
            <td colspan="2" class="py-0.5 text-xs">
              <span v-if="props.packages === null" class="text-muted">{{ t('system.versions.unknown') }}</span>
              <template v-else>
                {{ t('system.versions.packageUpdates', props.packages) }}
              </template>
              <UBadge v-if="props.packages" color="warning" variant="subtle" size="sm" class="ms-2" :label="t('system.versions.update')" />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
