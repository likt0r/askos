export const LANGUAGES = ['en', 'de'] as const
export type Language = typeof LANGUAGES[number]
export const LANGUAGE_KEY = 'agentbox-language'
/** The switch's entry for "no choice in this browser": the order decides again. */
export const AUTO_LANGUAGE = 'auto'
/** `default_language` of the Settings that leaves it to the browser (its default) */
export const BROWSER_LANGUAGE = 'browser'

export function isLanguage(code: unknown): code is Language {
  return typeof code === 'string' && (LANGUAGES as readonly string[]).includes(code)
}

function known(code: unknown): Language | null {
  if (typeof code !== 'string') return null
  const primary = code.toLowerCase().split(/[-_]/)[0]
  return isLanguage(primary) ? primary : null
}

/** The language to show: the one chosen in this browser, else `default_language` of the
 * Settings when it is en or de (known once signed in; its default `browser` names none), else
 * the first of the browser's languages we have, else English. */
export function pickLanguage(stored: string | null, browser: readonly string[], defaultLanguage: unknown = null): Language {
  if (isLanguage(stored)) return stored
  if (isLanguage(defaultLanguage)) return defaultLanguage
  for (const code of browser) {
    const lang = known(code)
    if (lang) return lang
  }
  return 'en'
}

// the last choice when localStorage refused it (private mode): it lasts for this page only, but
// there it still wins over default_language and the sign-in page's order
let chosenOnThisPage: Language | null = null

export function storedLanguage(): string | null {
  try {
    return chosenOnThisPage ?? localStorage.getItem(LANGUAGE_KEY)
  }
  catch {
    return chosenOnThisPage
  }
}

/** The language chosen in this browser, or null. */
export function chosenLanguage(): Language | null {
  const stored = storedLanguage()
  return isLanguage(stored) ? stored : null
}

export function storeLanguage(lang: Language) {
  try {
    localStorage.setItem(LANGUAGE_KEY, lang)
    chosenOnThisPage = null
  }
  catch {
    chosenOnThisPage = lang
  }
}

/** Automatic: no language is chosen in this browser any more. */
export function forgetLanguage() {
  chosenOnThisPage = null
  try {
    localStorage.removeItem(LANGUAGE_KEY)
  }
  catch {
    // private mode: nothing was stored
  }
}

export function browserLanguages(): string[] {
  if (typeof navigator === 'undefined') return []
  return navigator.languages?.length ? [...navigator.languages] : navigator.language ? [navigator.language] : []
}
