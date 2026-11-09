/** The language chosen in this browser (null: Automatic), shared by every switch on the page
 * (the sidebar's and the narrow screen's menu); read again from storage when a switch sets up. */
const chosen = ref<Language | null>(null)

/** The language without a choice in this browser: once signed in, `default_language` of the
 * Settings when it is en or de; else the browser's language, else English. */
function automaticLanguage(): Language {
  const session = useSession().state
  return pickLanguage(null, browserLanguages(), session.authenticated ? session.defaultLanguage : null)
}

/** The UI language and the switch: English, German, or Automatic (no choice in this browser);
 * a choice is remembered in this browser. */
export function useLanguage() {
  const { locale, locales, setLocale } = useI18n()
  const options = computed(() => locales.value.map(l => ({ value: l.code, label: l.name ?? l.code })))
  chosen.value = chosenLanguage()

  async function choose(code: string) {
    if (code === AUTO_LANGUAGE) {
      forgetLanguage()
      chosen.value = null
      await setLocale(automaticLanguage())
      return
    }
    if (!isLanguage(code)) return
    storeLanguage(code)
    chosen.value = code
    await setLocale(code)
  }

  /** The Settings page saved a new `default_language`: this browser follows it at once, unless a
   * language was chosen here; other browsers at their next sign-in or page load. Returns whether
   * it applies here. */
  async function followDefaultLanguage(value: unknown): Promise<boolean> {
    useSession().setDefaultLanguage(value)
    if (chosenLanguage()) return false
    const lang = pickLanguage(null, browserLanguages(), value)
    if (lang !== locale.value) await setLocale(lang)
    return true
  }

  return { locale, options, chosen: readonly(chosen), choose, followDefaultLanguage }
}

/** Without a session (the auth middleware calls it before the sign-in page shows, also after
 * signing out): the Settings are not known, so the choice made in this browser, else the
 * browser's language, else English. */
export async function applySignedOutLanguage() {
  const i18n = useNuxtApp().$i18n
  const lang = pickLanguage(storedLanguage(), browserLanguages())
  if (lang !== i18n.locale.value) await i18n.setLocale(lang)
}

/** Signed in (the auth middleware calls it before a page shows, so neither a page load nor a
 * sign-in shows the browser's language first): unless a language was chosen in this browser,
 * the order with `default_language` as GET or POST /session gave it: en or de wins over the
 * browser's language, `browser` shows the browser's (also after an earlier en or de in this
 * tab). Without a known value the language stays. The default is not stored as a choice. */
export async function applyDefaultLanguage() {
  const value = useSession().state.defaultLanguage
  if (!(isLanguage(value) || value === BROWSER_LANGUAGE) || chosenLanguage()) return
  const i18n = useNuxtApp().$i18n
  const lang = pickLanguage(null, browserLanguages(), value)
  if (lang !== i18n.locale.value) await i18n.setLocale(lang)
}
