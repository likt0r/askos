import en from './locales/en'
import de from './locales/de'

// Messages are bundled (no lazy loading): two small languages, no extra requests.
export default defineI18nConfig(() => ({
  legacy: false,
  fallbackLocale: 'en',
  messages: { en, de },
}))
