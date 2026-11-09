// Times of the API: ISO 8601 strings in UTC, or epoch seconds (Allowlist, Host readings).

export function parseApiTime(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

/** "3 Nov 2026, 12:00:05" in the browser's time zone and the UI language. */
export function formatWhen(value: string | number | null | undefined, locale: string, seconds = true): string {
  const d = parseApiTime(value)
  if (!d) return '—'
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium', timeStyle: seconds ? 'medium' : 'short',
  }).format(d)
}

export function formatClock(value: string | number | null | undefined, locale: string): string {
  const d = parseApiTime(value)
  if (!d) return '—'
  return new Intl.DateTimeFormat(locale, { timeStyle: 'medium' }).format(d)
}

/** The local calendar day of a time ("2026-11-03"), for grouping. */
export function localDayKey(value: string | number | null | undefined): string {
  const d = parseApiTime(value)
  if (!d) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function formatDayHeading(value: string | number | null | undefined, locale: string): string {
  const d = parseApiTime(value)
  if (!d) return '—'
  return new Intl.DateTimeFormat(locale, { dateStyle: 'full' }).format(d)
}
