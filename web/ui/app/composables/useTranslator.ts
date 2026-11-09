// vue-i18n's t and te as the Translator that the text helpers in utils/ take (#69): Signal
// reasons, blocked destinations and refused GitHub tokens (utils/access.ts).
import type { Translator } from '~/utils/activity'

export function useTranslator(): Translator {
  const { t, te } = useI18n()
  return { t: (key, values) => t(key, values ?? {}), te: key => te(key) }
}
