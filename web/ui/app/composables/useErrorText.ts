/** The text for an error: the translation of its API code, else the backend's English
 * message, else a generic one. agentbox's own `detail` (English and untranslated, for
 * diagnosis) is never the main text. */
export function useErrorText() {
  const { t, te } = useI18n()
  return (error: unknown, params: Record<string, unknown> = {}): string => {
    if (error instanceof ApiError) {
      const key = `errors.${error.code}`
      if (te(key)) return t(key, { ...params, field: error.field ?? '' })
      if (error.message) return error.message
    }
    return t('errors.unknown')
  }
}
