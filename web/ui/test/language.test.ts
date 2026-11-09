// English and German: a choice remembered per browser (or Automatic: none), else (once signed
// in) the default language of the Settings when it is en or de, else the browser's language,
// else English. The default comes with GET and POST /session, so no GET /settings is needed.
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { watch } from 'vue'
import App from '~/app.vue'
import LanguageSwitch from '~/components/LanguageSwitch.vue'
import { resetSessionForTests, useSession } from '~/composables/useSession'
import { forgetLanguage, LANGUAGE_KEY, pickLanguage, storedLanguage, storeLanguage, type Language } from '~/utils/language'
import { fakeApi, SIGNED_OUT } from './fakes'
import { ok, openPage, useFreshApp } from './system-helpers'

/** GET or POST /session of a signed-in browser, with the Setting as the backend reads it. */
function session(defaultLanguage?: unknown) {
  return ok({ authenticated: true, expires: '2026-11-04T00:00:00Z', ...(defaultLanguage === undefined ? {} : { default_language: defaultLanguage }) })
}

const refused = () => {
  throw new DOMException('denied', 'QuotaExceededError')
}

describe('picking the language', () => {
  it('follows the browser when nothing was chosen', () => {
    expect(pickLanguage(null, ['de-DE', 'en-US'])).toBe('de')
    expect(pickLanguage(null, ['fr-FR', 'de-AT', 'en'])).toBe('de')
    expect(pickLanguage(null, ['en-GB', 'de'])).toBe('en')
    expect(pickLanguage(null, ['fr-FR'])).toBe('en')
    expect(pickLanguage(null, [])).toBe('en')
  })

  it('prefers the choice made in this browser', () => {
    expect(pickLanguage('en', ['de-DE'])).toBe('en')
    expect(pickLanguage('de', ['en-US'])).toBe('de')
    expect(pickLanguage('fr', ['de-DE'])).toBe('de')
  })

  it('puts the default language of the Settings before the browser when it is en or de', () => {
    expect(pickLanguage(null, ['de-DE'], 'en')).toBe('en')
    expect(pickLanguage(null, ['en-US', 'en'], 'de')).toBe('de')
    expect(pickLanguage(null, ['fr-FR'], 'de')).toBe('de')
    expect(pickLanguage(null, [], 'de')).toBe('de')
    expect(pickLanguage('fr', ['en-US'], 'de')).toBe('de')       // an unknown choice is none
  })

  it('lets the choice made in this browser win over the default language', () => {
    expect(pickLanguage('en', ['de-DE'], 'de')).toBe('en')
    expect(pickLanguage('de', ['en-US'], 'en')).toBe('de')
  })

  it('leaves it to the browser when the default language is `browser` or not a language', () => {
    expect(pickLanguage(null, ['de-AT'], 'browser')).toBe('de')
    expect(pickLanguage(null, ['fr-FR', 'en-GB'], 'browser')).toBe('en')
    expect(pickLanguage(null, ['fr-FR'], 'browser')).toBe('en')
    expect(pickLanguage('en', ['de-DE'], 'browser')).toBe('en')
    for (const odd of ['fr', 'EN', 'de-DE', '', 7, null, undefined, { value: 'en' }]) {
      expect(pickLanguage(null, ['de-DE'], odd), String(odd)).toBe('de')
    }
  })

  it('keeps a choice for this page when localStorage refuses it', () => {
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: refused, removeItem: () => {} })
    try {
      storeLanguage('de')
      expect(storedLanguage()).toBe('de')
      expect(pickLanguage(storedLanguage(), ['en-US'], 'en')).toBe('de')
    }
    finally {
      vi.unstubAllGlobals()
      storeLanguage('en')       // stored for real: the one kept for this page is gone
      localStorage.removeItem(LANGUAGE_KEY)
    }
    expect(storedLanguage()).toBeNull()
  })

  it('forgets the choice, also one kept for this page only', () => {
    storeLanguage('de')
    forgetLanguage()
    expect(storedLanguage()).toBeNull()
    expect(localStorage.getItem(LANGUAGE_KEY)).toBeNull()
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: refused, removeItem: refused })
    try {
      storeLanguage('en')
      expect(storedLanguage()).toBe('en')
      forgetLanguage()
      expect(storedLanguage()).toBeNull()
    }
    finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('language switch', () => {
  beforeEach(async () => {
    resetSessionForTests()
    localStorage.clear()
    await useNuxtApp().$i18n.setLocale('en')
  })
  afterEach(() => resetSessionForTests())

  /** A language chosen earlier in this browser, as the page-load plugin shows it. */
  async function chosenBefore(lang: Language) {
    localStorage.setItem(LANGUAGE_KEY, lang)
    await useNuxtApp().$i18n.setLocale(lang)
  }

  it('switches the UI and remembers the choice', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT })
    const wrapper = await mountSuspended(LanguageSwitch, { props: { collapsed: true } })
    await flushPromises()
    const { t, locale } = useNuxtApp().$i18n
    expect(t('nav.settings')).toBe('Settings')
    await wrapper.find('[data-testid="language-toggle"]').trigger('click')
    await flushPromises()
    expect(locale.value).toBe('de')
    expect(t('nav.settings')).toBe('Einstellungen')
    expect(localStorage.getItem(LANGUAGE_KEY)).toBe('de')
    expect(pickLanguage(localStorage.getItem(LANGUAGE_KEY), ['en-US'])).toBe('de')
    await wrapper.find('[data-testid="language-toggle"]').trigger('click')
    await flushPromises()
    expect(locale.value).toBe('en')
    expect(localStorage.getItem(LANGUAGE_KEY)).toBe('en')
  })

  it('offers Automatic once a language was chosen; signed out it goes back to the browser', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT })
    const first = await mountSuspended(LanguageSwitch, { props: { collapsed: true } })
    await flushPromises()
    expect(first.find('[data-testid="language-auto"]').exists()).toBe(false)     // nothing chosen yet

    await chosenBefore('de')
    const wrapper = await mountSuspended(LanguageSwitch, { props: { collapsed: true } })
    await flushPromises()
    const auto = wrapper.find('[data-testid="language-auto"]')
    expect(auto.exists()).toBe(true)
    expect(auto.attributes('aria-label')).toBe('Automatisch')
    await auto.trigger('click')
    await flushPromises()
    expect(localStorage.getItem(LANGUAGE_KEY)).toBeNull()
    expect(useNuxtApp().$i18n.locale.value).toBe('en')       // happy-dom's browser: en-US
    expect(wrapper.find('[data-testid="language-auto"]').exists()).toBe(false)
  })

  it('Automatic goes back to the default language of the Settings when signed in', async () => {
    fakeApi({ 'GET /session': session('de') })
    await useSession().refresh()
    await chosenBefore('en')
    const wrapper = await mountSuspended(LanguageSwitch, { props: { collapsed: true } })
    await flushPromises()
    expect(useNuxtApp().$i18n.locale.value).toBe('en')       // the choice wins over the default
    await wrapper.find('[data-testid="language-auto"]').trigger('click')
    await flushPromises()
    expect(useNuxtApp().$i18n.locale.value).toBe('de')
    expect(localStorage.getItem(LANGUAGE_KEY)).toBeNull()
  })

  it('shows Automatic in the expanded switch while nothing was chosen, else the language', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT })
    const automatic = await mountSuspended(LanguageSwitch)
    await flushPromises()
    expect(automatic.find('[data-testid="language-switch"]').text()).toContain('Automatic')
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    expect(automatic.find('[data-testid="language-switch"]').text()).toContain('Automatisch')

    await chosenBefore('de')
    const chosen = await mountSuspended(LanguageSwitch)
    await flushPromises()
    expect(chosen.find('[data-testid="language-switch"]').text()).toContain('Deutsch')
    expect(chosen.find('[data-testid="language-switch"]').text()).not.toContain('Automatisch')
  })
})

describe('default language of the Settings', () => {
  useFreshApp()

  // mountSuspended leaves an app mounted: it would follow the next test's navigation, so each
  // is unmounted. Each test also navigates for real (a new query), so the auth middleware runs:
  // a replace to the route already shown would skip it and leave the session unknown.
  const mounted: { unmount(): void }[] = []
  let visit = 0
  afterEach(() => {
    while (mounted.length) mounted.pop()!.unmount()
  })

  function fresh(route: string) {
    return `${route}${route.includes('?') ? '&' : '?'}visit=${++visit}`
  }

  async function openApp(route: string, routes: Parameters<typeof openPage>[1] = {}) {
    const page = await openPage(fresh(route), routes)
    mounted.push(page.wrapper)
    return page
  }

  async function mountApp(route: string) {
    const wrapper = await mountSuspended(App, { route: fresh(route) })
    mounted.push(wrapper)
    return wrapper
  }

  const VERSION = ok({ api: 1, version: 'aaaa', ui: 'nuxt', installed: null })
  const locale = () => useNuxtApp().$i18n.locale.value
  const path = () => useRouter().currentRoute.value.path

  /** The browser's own languages (happy-dom has en-US and en); the rest of navigator stays. */
  function browserSpeaks(...languages: string[]) {
    vi.stubGlobal('navigator', new Proxy(navigator, {
      get: (target, key) => key === 'languages' ? languages : key === 'language' ? (languages[0] ?? '') : Reflect.get(target, key),
    }))
  }

  /** What the page-load plugin would have shown (it runs once per test file, not per mount). */
  async function pageLoadedIn(lang: Language) {
    await useNuxtApp().$i18n.setLocale(lang)
  }

  /** The UI never asks GET /settings for the language. */
  function settingsAsked(api: { calls: { method: string, path: string }[] }) {
    return api.calls.filter(c => c.path === '/settings').length
  }

  it('wins over the browser right after sign-in, without a flash or a request; the sign-in page follows the browser', async () => {
    browserSpeaks('de-DE', 'de')
    await pageLoadedIn('de')
    let signedIn = false
    const api = fakeApi({
      'GET /session': () => (signedIn ? session('en') : SIGNED_OUT),
      'POST /session': () => {
        signedIn = true
        return session('en')
      },
      'GET /version': VERSION,
    })
    const wrapper = await mountApp('/sign-in')
    await flushPromises()
    const { t } = useNuxtApp().$i18n
    await vi.waitFor(() => expect(path()).toBe('/sign-in'))
    expect(locale()).toBe('de')

    // English by the time the app shell's route is there: /vms never showed German
    const seen: string[] = []
    const stop = watch(() => [path(), locale()] as const, ([p, l]) => seen.push(`${p} ${l}`))
    await wrapper.find('input[type="password"]').setValue('correct horse')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    await vi.waitFor(() => expect(path()).toBe('/vms'))
    stop()
    expect(locale()).toBe('en')
    expect(seen).not.toContain('/vms de')
    expect(t('nav.settings')).toBe('Settings')
    expect(settingsAsked(api)).toBe(0)
    expect(localStorage.getItem(LANGUAGE_KEY)).toBeNull()    // a default, not a choice
  })

  it('applies when the page loads with a session, before the page shows', async () => {
    browserSpeaks('en-US', 'en')
    await pageLoadedIn('en')
    const route = fresh('/vms')
    const seen: string[] = []
    const stop = watch(() => [useRouter().currentRoute.value.fullPath, locale()] as const, ([p, l]) => seen.push(`${p} ${l}`))
    const { api, wrapper } = await openPage(route, { 'GET /session': session('de') })
    mounted.push(wrapper)
    stop()
    expect(seen).not.toContain(`${route} en`)      // the route was never shown in English
    expect(seen).toContain(`${route} de`)
    expect(locale()).toBe('de')
    expect(wrapper.find('nav').text()).toContain('Einstellungen')
    expect(settingsAsked(api)).toBe(0)
  })

  it.each<[string[], Language]>([
    [['de-DE', 'en'], 'de'],
    [['fr-FR', 'en-GB'], 'en'],
    [['fr-FR'], 'en'],
  ])('`browser` (the default) shows the browser\'s language, also after an earlier default: %j shows %s', async (languages, shown) => {
    browserSpeaks(...languages)
    await pageLoadedIn(shown === 'de' ? 'en' : 'de')     // as an earlier en or de showed this tab
    await openApp('/vms', { 'GET /session': session('browser') })
    expect(locale()).toBe(shown)
  })

  it.each([[undefined], ['fr'], [7]])('a missing or unknown value (%j) leaves the language', async (value) => {
    browserSpeaks('de-DE')
    await pageLoadedIn('de')
    await openApp('/vms', { 'GET /session': session(value) })
    expect(locale()).toBe('de')
  })

  it('does not apply over a language chosen in this browser', async () => {
    browserSpeaks('en-US')
    localStorage.setItem(LANGUAGE_KEY, 'de')
    await pageLoadedIn('de')
    await openApp('/vms', { 'GET /session': session('en') })
    expect(locale()).toBe('de')
  })

  it('no longer applies once signed out: the sign-in page follows the browser again', async () => {
    browserSpeaks('de-DE')
    await pageLoadedIn('de')
    const { wrapper } = await openApp('/vms', {
      'GET /session': session('en'),
      'DELETE /session': ok({ authenticated: false }),
    })
    expect(locale()).toBe('en')
    await wrapper.find('[data-testid="sign-out"]').trigger('click')
    await flushPromises()
    await vi.waitFor(() => expect(path()).toBe('/sign-in'))
    await vi.waitFor(() => expect(locale()).toBe('de'))
    expect(localStorage.getItem(LANGUAGE_KEY)).toBeNull()
  })

  it('keeps a language chosen in this browser on the sign-in page', async () => {
    browserSpeaks('de-DE')
    localStorage.setItem(LANGUAGE_KEY, 'en')
    await pageLoadedIn('de')      // shown before, e.g. by another tab's choice
    fakeApi({ 'GET /session': SIGNED_OUT })
    await mountApp('/sign-in')
    await flushPromises()
    await vi.waitFor(() => expect(locale()).toBe('en'))
  })
})
