// The words of an Activity entry in the UI language (#69): from its code and parameters, a
// failure with its translated error; else agentbox's stored text (utils/activity.ts).
import type { ActivityEntry, Translator } from '~/utils/activity'

export function useActivityText() {
  const { t, te } = useI18n()
  const tr: Translator = { t: (key, values) => t(key, values ?? {}), te: key => te(key) }
  return (e: ActivityEntry): string => activityText(e, tr)
}
