<script setup lang="ts">
// The app shell: navigation, the host bar, the live connection's state, the page.
import type { NavigationMenuItem } from '@nuxt/ui'

const { t } = useI18n()
const route = useRoute()
const live = useLive()
const release = useRelease()
useNotificationListener()       // browser notifications for `notification` events (#21)

const items = computed<NavigationMenuItem[]>(() => [
  { label: t('nav.vms'), 'data-testid': 'nav-vms', icon: 'i-lucide-server', to: '/vms' },
  { label: t('nav.allowlist'), 'data-testid': 'nav-allowlist', icon: 'i-lucide-list-checks', to: '/allowlist' },
  { label: t('nav.watchlist'), 'data-testid': 'nav-watchlist', icon: 'i-lucide-radar', to: '/watchlist' },
  { label: t('nav.activity'), 'data-testid': 'nav-activity', icon: 'i-lucide-history', to: '/activity' },
  { label: t('nav.system'), 'data-testid': 'nav-system', icon: 'i-lucide-cpu', to: '/system' },
  { label: t('nav.settings'), 'data-testid': 'nav-settings', icon: 'i-lucide-settings', to: '/settings' },
])

const title = computed(() => {
  const vm = route.params.vm
  if (typeof vm === 'string') return vm
  const key = route.meta.titleKey
  return typeof key === 'string' ? t(key) : t('app.title')
})

function online() {
  live.reconnectNow()
}

onMounted(() => {
  live.start()
  void loadOwnVersion()
  window.addEventListener('online', online)
})
onBeforeUnmount(() => {
  window.removeEventListener('online', online)
})
</script>

<template>
  <UDashboardGroup storage="local" storage-key="agentbox-dashboard" unit="rem">
    <UDashboardSidebar
      collapsible
      :default-size="15"
      :min-size="12"
      :max-size="20"
      :collapsed-size="4"
      :ui="{ footer: 'flex-col items-stretch gap-1 lg:border-t lg:border-default' }"
    >
      <template #header="{ collapsed }">
        <NuxtLink to="/vms" class="flex items-center gap-2 font-semibold text-highlighted min-w-0">
          <UIcon name="i-lucide-box" class="size-5 shrink-0 text-primary" />
          <span v-if="!collapsed" class="truncate">{{ t('app.host') }}</span>
        </NuxtLink>
      </template>

      <template #default="{ collapsed }">
        <UNavigationMenu
          :items="items"
          orientation="vertical"
          :collapsed="collapsed"
          tooltip
          :aria-label="t('nav.label')"
        />
      </template>

      <template #footer="{ collapsed }">
        <ConnectionStatus :collapsed="collapsed" />
        <LanguageSwitch :collapsed="collapsed" />
        <SignOutButton :collapsed="collapsed" />
        <p v-if="release && !collapsed" class="px-2 text-xs tabular-nums text-dimmed" data-testid="release">
          Agent Box v{{ release }}
        </p>
      </template>
    </UDashboardSidebar>

    <div class="flex min-w-0 flex-1 flex-col h-svh">
      <UDashboardNavbar :title="title">
        <template #leading>
          <UDashboardSidebarCollapse class="max-lg:hidden" />
        </template>
        <template #right>
          <ConnectionStatus compact class="lg:hidden" />
        </template>
      </UDashboardNavbar>
      <NotificationsReloadPrompt />
      <HostBar />
      <ConnectionNotice />
      <main class="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        <slot />
      </main>
    </div>
  </UDashboardGroup>
</template>
