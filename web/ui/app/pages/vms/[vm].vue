<script setup lang="ts">
// A VM's detail: its live state, what does not answer when it does not respond (#54),
// lifecycle actions and overview (#13), and the tabs Web, Access, Log (#3), Pair (#19).
import type { NavigationMenuItem } from '@nuxt/ui'

const { t } = useI18n()
const route = useRoute()
const live = useLive()

const name = computed(() => String(route.params.vm))
const vm = computed(() => live.state.vms.find(v => v.name === name.value))
// the overview can be folded away (phones); remembered in this browser
const overviewOpen = ref(storedFlag('agentbox-vm-overview', true))
watch(overviewOpen, v => storeFlag('agentbox-vm-overview', v))
const tabs = computed<NavigationMenuItem[]>(() => [
  { label: t('tabs.web'), icon: 'i-lucide-globe', to: `/vms/${name.value}/web` },
  { label: t('tabs.access'), icon: 'i-lucide-key-round', to: `/vms/${name.value}/access` },
  { label: t('tabs.log'), icon: 'i-lucide-scroll-text', to: `/vms/${name.value}/log` },
  { label: t('tabs.pair'), icon: 'i-lucide-smartphone', to: `/vms/${name.value}/pair` },
])
</script>

<template>
  <div class="space-y-4">
    <UButton to="/vms" color="neutral" variant="link" icon="i-lucide-arrow-left" :label="t('vms.back')" class="px-0" />

    <p v-if="!live.state.loaded" class="text-sm text-muted">
      {{ t('vms.loading') }}
    </p>
    <UAlert
      v-else-if="!vm"
      color="neutral"
      variant="subtle"
      icon="i-lucide-search-x"
      :title="t('vms.notFound', { name })"
      data-testid="vm-not-found"
    />
    <template v-else>
      <div class="flex flex-wrap items-center gap-3" data-testid="vm-detail">
        <h2 class="text-xl font-semibold text-highlighted">
          {{ vm.name }}
        </h2>
        <VmStateBadge :state="vm.state" />
        <span class="font-mono text-xs text-muted">{{ vm.image }}<template v-if="vm.build_pin"> @ {{ vm.build_pin }}</template></span>
        <span class="flex-1" />
        <UButton
          color="neutral"
          variant="ghost"
          size="xs"
          :icon="overviewOpen ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
          :label="overviewOpen ? t('vm.overview.hide') : t('vm.overview.show')"
          data-testid="overview-toggle"
          @click="overviewOpen = !overviewOpen"
        />
      </div>
      <VmNotResponding :vm="vm" />
      <VmActions :vm="vm" />
      <VmOverview v-if="overviewOpen" :vm="vm" />
      <UNavigationMenu
        :items="tabs"
        orientation="horizontal"
        highlight
        :aria-label="t('tabs.label')"
        class="border-b border-default -mx-1"
        data-testid="vm-tabs"
      />
      <NuxtPage />
    </template>
  </div>
</template>
