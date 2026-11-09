<script setup lang="ts">
const props = defineProps<{ collapsed?: boolean }>()
const { t } = useI18n()
const session = useSession()
const busy = ref(false)

async function signOut() {
  busy.value = true
  try {
    await session.signOut()
  }
  catch {
    // signed out locally either way
  }
  finally {
    busy.value = false
  }
  await navigateTo('/sign-in', { replace: true })
}
</script>

<template>
  <UButton
    color="neutral"
    variant="ghost"
    icon="i-lucide-log-out"
    :label="props.collapsed ? undefined : t('signOut')"
    :aria-label="t('signOut')"
    :loading="busy"
    data-testid="sign-out"
    @click="signOut"
  />
</template>
