/** The text for an error: the translation of its API code, else the backend's English
 * message, else a generic one. agentbox's own `detail` (English and untranslated, for
 * diagnosis) is never the main text. */
export function useErrorText() {
  const { t, te } = useI18n()
  return (error: unknown, params: Record<string, unknown> = {}): string => {
    if (error instanceof ApiError) {
      const key = `errors.${error.code}`
      // a pattern the rules refuse: which rule, or which unrelated host it matches
      const reason = error.params?.reason
      if (error.code === 'invalid_pattern' && reason && te(`regex.reasons.${reason}`))
        return t('regex.invalid', { reason: t(`regex.reasons.${reason}`) })
      if (error.code === 'pattern_too_broad' && error.params?.host) return t('regex.tooBroad', { host: error.params.host })
      if (te(key)) return t(key, { ...params, field: error.field ?? '' })
      if (error.message) return error.message
    }
    return t('errors.unknown')
  }
}
