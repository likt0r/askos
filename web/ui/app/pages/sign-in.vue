<script setup lang="ts">
// Sign-in with the Control Room password (POST /api/v1/session), or with a sign-in link from
// the Host (`agentbox sign-in-link`, /sign-in#link=<token>): the page takes the token out of
// the address first, then sends it once; a refused link leaves the password form. After five
// failures in 15 minutes (passwords and links) the backend refuses for a while (429 +
// Retry-After): the form waits that long.
definePageMeta({ layout: 'bare', titleKey: 'signIn.title' })

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const session = useSession()
const errorText = useErrorText()
const now = useNow(250)

const password = ref('')
const link = linkFromHash(route.hash)
const withLink = ref(link !== null)
const busy = ref(false)
const error = ref<unknown>(null)
const lockedUntil = ref(0)

const remaining = computed(() => Math.max(0, Math.ceil((lockedUntil.value - now.value) / 1000)))
const locked = computed(() => remaining.value > 0)
const message = computed(() => {
  if (!error.value) return null
  if (error.value instanceof ApiError && error.value.code === 'too_many_attempts') {
    return locked.value ? t('signIn.brake', { seconds: remaining.value }) : t('signIn.brakeOver')
  }
  return errorText(error.value)
})

async function attempt(signIn: () => Promise<void>) {
  busy.value = true
  error.value = null
  try {
    await signIn()
    password.value = ''
    await navigateTo(safeRedirect(route.query.redirect), { replace: true })
  }
  catch (e) {
    error.value = e
    if (e instanceof ApiError && e.code === 'too_many_attempts') {
      lockedUntil.value = Date.now() + (e.retryAfter ?? 60) * 1000
      now.value = Date.now()
    }
  }
  finally {
    busy.value = false
  }
}

async function submit() {
  if (busy.value || locked.value || !password.value) return
  await attempt(() => session.signIn(password.value))
}

onMounted(async () => {
  if (link === null) return
  busy.value = true
  try {
    // out of the address bar and this history entry before it is sent
    await router.replace({ path: route.path, query: route.query, hash: '' })
    await attempt(() => session.signInWithLink(link))
  }
  finally {
    withLink.value = false
    busy.value = false
  }
})
</script>

<template>
  <UCard class="w-full max-w-sm">
    <template #header>
      <div class="flex items-center gap-3">
        <UIcon name="i-lucide-box" class="size-7 text-primary" />
        <div class="min-w-0 flex-1">
          <h1 class="text-lg font-semibold text-highlighted">
            {{ t('signIn.title') }}
          </h1>
          <p class="text-sm text-muted">
            {{ t('signIn.intro') }}
          </p>
        </div>
        <LanguageSwitch collapsed />
      </div>
    </template>

    <form class="space-y-4" novalidate data-testid="sign-in-form" @submit.prevent="submit">
      <UAlert
        v-if="withLink"
        color="info"
        variant="subtle"
        icon="i-lucide-link"
        :title="t('signIn.withLink')"
        data-testid="sign-in-link"
      />
      <UAlert
        v-if="route.query.expired && !error"
        color="info"
        variant="subtle"
        icon="i-lucide-info"
        :title="t('signIn.expired')"
      />
      <UFormField :label="t('signIn.password')" name="password">
        <UInput
          v-model="password"
          type="password"
          autocomplete="current-password"
          autofocus
          class="w-full"
          data-testid="password"
        />
      </UFormField>
      <UAlert
        v-if="message"
        :color="locked ? 'warning' : 'error'"
        variant="subtle"
        icon="i-lucide-circle-alert"
        :title="message"
        role="alert"
        data-testid="sign-in-error"
      />
      <UButton
        type="submit"
        block
        :label="t('signIn.submit')"
        :loading="busy"
        :disabled="locked || !password"
        data-testid="sign-in-submit"
      />
    </form>
  </UCard>
</template>
