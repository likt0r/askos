// Settings (#22): what the form knows about each key. The backend (agentbox/settings.py)
// validates and is the authority; this mirrors its rules only to show the right input, say
// what is allowed in the UI's language, and catch a typo before the request. The API's own
// `allowed` text is English, so it is not shown unless a key is unknown to the UI.

export type SettingValue = string | number | boolean

export interface SettingInfo {
  value: SettingValue
  default: SettingValue
  is_default: boolean
  applies_later: string | null
  allowed: string
}

export type Spec =
  | { type: 'int', min: number, max: number, unit?: string }
  | { type: 'time' }
  /** numeric: the options are whole numbers; boolean: they are true and false (sent as JSON booleans) */
  | { type: 'choice', options: readonly string[], numeric?: boolean, boolean?: boolean }
  | { type: 'name' }
  /** a version X.Y.Z, or empty for automatic */
  | { type: 'version' }

const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const

/** Every key the Control Room may change (docs/control-room-api.md). The password, the home
 * network and the ports are not among them and never appear here. */
export const SETTING_SPECS: Record<string, Spec> = {
  maintenance_time: { type: 'time' },
  reset_age_days: { type: 'int', min: 1, max: 365 },
  auto_update: { type: 'choice', options: ['true', 'false'], boolean: true },
  rebuild_day: { type: 'choice', options: DAYS },
  builds_kept: { type: 'int', min: 1, max: 20 },
  default_memory_mib: { type: 'int', min: 1024, max: 1048576, unit: 'MiB' },
  default_data_disk_gib: { type: 'int', min: 1, max: 16384, unit: 'GiB' },
  default_image: { type: 'name' },
  open_web_max_minutes: { type: 'int', min: 1, max: 1440, unit: 'min' },
  temporary_entry_default: { type: 'choice', options: ['15min', '1h', '8h', '7d'] },
  young_domain_days: { type: 'choice', options: ['7', '14', '21', '28', '35'], numeric: true },
  feed_interval_hours: { type: 'int', min: 1, max: 168 },
  package_min_age_days: { type: 'int', min: 1, max: 30 },
  orca_version: { type: 'version' },
  activity_retention_days: { type: 'int', min: 1, max: 3650 },
  session_hours: { type: 'int', min: 1, max: 168 },
  default_language: { type: 'choice', options: ['browser', 'en', 'de'] },
}

export const SETTING_GROUPS: { id: string, keys: string[] }[] = [
  { id: 'schedule', keys: ['maintenance_time', 'reset_age_days', 'auto_update', 'rebuild_day', 'builds_kept', 'package_min_age_days', 'orca_version'] },
  { id: 'newVms', keys: ['default_memory_mib', 'default_data_disk_gib', 'default_image'] },
  { id: 'access', keys: ['open_web_max_minutes', 'temporary_entry_default', 'young_domain_days', 'feed_interval_hours'] },
  { id: 'controlRoom', keys: ['activity_retention_days', 'session_hours', 'default_language'] },
]

const TIME = /^([01][0-9]|2[0-3]):[0-5][0-9]$/
const NAME = /^[a-z0-9][a-z0-9-]{0,31}$/
// as agentbox/settings.py ORCA_VERSION
const VERSION = /^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$/

export type Problem = { code: 'empty' } | { code: 'int', min: number, max: number } | { code: 'time' } | { code: 'choice', options: string } | { code: 'name' } | { code: 'version' }

/** What is wrong with the text of a field, or null. Unknown keys are not judged. */
export function settingProblem(key: string, text: string): Problem | null {
  const spec = SETTING_SPECS[key]
  const value = text.trim()
  if (!spec) return null
  if (spec.type === 'version') return !value || VERSION.test(value) ? null : { code: 'version' }
  if (!value) return { code: 'empty' }
  switch (spec.type) {
    case 'int':
      return /^[0-9]{1,9}$/.test(value) && Number(value) >= spec.min && Number(value) <= spec.max
        ? null
        : { code: 'int', min: spec.min, max: spec.max }
    case 'time':
      return TIME.test(value) ? null : { code: 'time' }
    case 'choice':
      return spec.options.includes(value) ? null : { code: 'choice', options: spec.options.join(', ') }
    case 'name':
      return NAME.test(value) ? null : { code: 'name' }
  }
}

/** The value as the API takes it: whole numbers as numbers, true and false as booleans, the
 * rest as text. */
export function settingValue(key: string, text: string): SettingValue {
  const spec = SETTING_SPECS[key]
  const value = text.trim()
  if (spec?.type === 'choice' && spec.boolean) return value === 'true'
  return spec && (spec.type === 'int' || (spec.type === 'choice' && spec.numeric)) ? Number(value) : value
}

/** What to send for a field: `default` when it equals the default (the setting is then not
 * stored, so a later change of the default applies), else the typed value. */
export function toSend(key: string, text: string, info: SettingInfo): SettingValue {
  const value = settingValue(key, text)
  return String(value) === String(info.default) ? 'default' : value
}

/** Whether the text differs from the stored value (a number written "007" is the same as 7). */
export function settingChanged(key: string, text: string, info: SettingInfo): boolean {
  const spec = SETTING_SPECS[key]
  const value = text.trim()
  const same = spec && (spec.type === 'int' || (spec.type === 'choice' && spec.numeric)) && /^[0-9]{1,9}$/.test(value)
    ? Number(value) === Number(info.value)
    : value === String(info.value)
  return !same
}
