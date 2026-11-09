/** Times for people in the UI's language: a moment (`at`), how long ago (`ago`) and a span
 * (`span`, e.g. "2 h 5 min"). Moments are epoch seconds or ISO 8601 strings. */
export function useWhen() {
  const { t, locale } = useI18n()

  function ms(v: number | string | null | undefined): number | null {
    if (v == null || v === '') return null
    const n = typeof v === 'number' ? v * 1000 : Date.parse(v)
    return Number.isFinite(n) ? n : null
  }

  function at(v: number | string | null | undefined): string {
    const m = ms(v)
    if (m == null) return '—'
    const d = new Date(m)
    const today = new Date()
    const sameDay = d.toDateString() === today.toDateString()
    return new Intl.DateTimeFormat(locale.value, sameDay
      ? { timeStyle: 'short' }
      : { dateStyle: 'medium', timeStyle: 'short' }).format(d)
  }

  function span(seconds: number): string {
    const s = Math.max(0, Math.round(seconds))
    if (s < 60) return t('when.lessThanMinute')
    const days = Math.floor(s / 86400)
    const hours = Math.floor((s % 86400) / 3600)
    const minutes = Math.floor((s % 3600) / 60)
    const parts: string[] = []
    if (days) parts.push(t('when.days', { n: days }))
    if (hours) parts.push(t('when.hours', { n: hours }))
    if (minutes && !days) parts.push(t('when.minutes', { n: minutes }))
    return parts.join(' ')
  }

  function ago(v: number | string | null | undefined, nowMs = Date.now()): string {
    const m = ms(v)
    if (m == null) return '—'
    const s = Math.round((nowMs - m) / 1000)
    if (s < 60) return t('when.justNow')
    return t('when.ago', { span: span(s) })
  }

  return { at, ago, span }
}
