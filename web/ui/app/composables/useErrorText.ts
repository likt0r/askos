// The codes of a refused VM variable or secret that name it (`params.name`).
const ENV_NAMED = ['env_name_reserved', 'env_name_is_secret', 'invalid_env_value', 'env_too_large']
const SECRET_NAMED = ['secret_name_reserved', 'secret_name_is_env', 'invalid_secret_value', 'secret_too_large']

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
      // a VM variable or secret refused by its name (never its value)
      const name = error.params?.name
      if (name && ENV_NAMED.includes(error.code)) return t(`vm.env.named.${error.code}`, { name, max: MAX_ENV_VALUE_BYTES })
      if (name && SECRET_NAMED.includes(error.code)) return t(`vm.secrets.named.${error.code}`, { name, max: MAX_ENV_VALUE_BYTES })
      // a change of SSH access saved but not applied: the command that finishes it on the Host
      if (error.code === 'ssh_not_applied')
        return t(key, { finish: error.params?.finish ?? 'agentbox regenerate' })
      if (te(key)) return t(key, { ...params, field: error.field ?? '' })
      if (error.message) return error.message
    }
    return t('errors.unknown')
  }
}
