// The rules of the VM detail views, the same as the CLI's (agentbox/registry.py,
// vms.py, credentials.py, guest.py). The UI checks them before sending so mistakes
// show at the field; agentbox checks them again and has the last word.

import type { Vm } from './live'

// --- creating a VM --------------------------------------------------------------

export const VM_NAME = /^[a-z][a-z0-9-]{1,15}$/
/** Words that stand where a VM name may stand (registry.RESERVED, plus v1, v2, ...). */
export const RESERVED_NAMES = ['off', 'all', 'new', 'status', 'login', 'logout', 'activity', 'allowlist',
  'watchlist', 'images', 'settings', 'system', 'host', 'events', 'version', 'vms', 'github-app']
export const GITHUB_OWNER = /^[A-Za-z0-9-]{1,39}$/
export const MIN_MEMORY_MIB = 1024
export const MAX_DATA_DISK_GIB = 16384

export type NameProblem = 'required' | 'invalid' | 'reserved' | 'exists'

export function nameProblem(name: string, existing: readonly string[]): NameProblem | null {
  if (!name) return 'required'
  if (!VM_NAME.test(name)) return 'invalid'
  if (RESERVED_NAMES.includes(name) || /^v[0-9]+$/.test(name)) return 'reserved'
  if (existing.includes(name)) return 'exists'
  return null
}

/** GitHub owners from free text (commas, spaces or new lines between them). */
export function parseOwners(text: string): { owners: string[], invalid: string[] } {
  const words = text.split(/[\s,]+/).filter(Boolean)
  const owners = [...new Set(words.filter(w => GITHUB_OWNER.test(w)))]
  return { owners, invalid: words.filter(w => !GITHUB_OWNER.test(w)) }
}

/** A whole number in a range, or null (empty means "the default"). */
export function wholeNumber(value: unknown, low: number, high: number): number | null | 'invalid' {
  if (value === null || value === undefined || value === '') return null
  const text = String(value).trim()
  if (!/^[0-9]{1,9}$/.test(text)) return 'invalid'
  const n = Number(text)
  return n >= low && n <= high ? n : 'invalid'
}

// --- the VM's journal -----------------------------------------------------------

export const PRIORITIES = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug'] as const
export const LOG_TIME = /^(today|yesterday|now|-\d{1,6}(s|min|h|d|w)|\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?)$/
export const LOG_UNIT = /^[A-Za-z0-9][A-Za-z0-9:_.@-]{0,127}$/
export const MAX_LOG_LINES = 5000
export const DEFAULT_LOG_LINES = 200
export const MAX_GREP = 200

export interface LogFilters {
  unit: string
  /** '' for every priority, else a syslog level name (that one and more severe) */
  priority: string
  since: string
  until: string
  boot: 'current' | 'all'
  grep: string
  lines: number | string
}

export function defaultLogFilters(): LogFilters {
  return { unit: '', priority: '', since: '', until: '', boot: 'current', grep: '', lines: DEFAULT_LOG_LINES }
}

export type LogFilterProblem = 'unit' | 'priority' | 'since' | 'until' | 'grep' | 'lines'

/** The filters agentbox would refuse (field names; empty when all are fine). */
export function logFilterProblems(f: LogFilters): LogFilterProblem[] {
  const problems: LogFilterProblem[] = []
  const unit = f.unit.trim()
  if (unit && !LOG_UNIT.test(unit)) problems.push('unit')
  if (f.priority && !(PRIORITIES as readonly string[]).includes(f.priority)) problems.push('priority')
  if (f.since.trim() && !LOG_TIME.test(f.since.trim())) problems.push('since')
  if (f.until.trim() && !LOG_TIME.test(f.until.trim())) problems.push('until')
  // eslint-disable-next-line no-control-regex
  if (f.grep.length > MAX_GREP || /[\u0000-\u001f\u007f-\u009f]/.test(f.grep)) problems.push('grep')
  const lines = wholeNumber(f.lines, 1, MAX_LOG_LINES)
  if (lines === 'invalid' || lines === null) problems.push('lines')
  return problems
}

/** The query of GET /vms/<vm>/logs; empty filters are left out. */
export function logQuery(f: LogFilters, afterCursor?: string | null): string {
  const q = new URLSearchParams()
  const set = (key: string, value: string) => {
    if (value) q.set(key, value)
  }
  set('unit', f.unit.trim())
  set('priority', f.priority)
  set('since', f.since.trim())
  set('until', f.until.trim())
  set('boot', f.boot)
  if (f.grep.trim()) q.set('grep', f.grep)       // the text as typed (spaces may be meant)
  set('lines', String(f.lines).trim())
  if (afterCursor) q.set('after_cursor', afterCursor)
  return q.toString()
}

export interface LogEntry {
  time: string | null
  unit: string | null
  identifier: string | null
  priority: number | null
  message: string | null
  boot: string | null
  cursor: string | null
}

export interface LogResult {
  vm: string
  entries: LogEntry[]
  cursor: string | null
  truncated: boolean
}

// agentbox already hides Pairing codes in logs; this hides anything that still looks
// like one (and agentbox's own marker, orca://pair?[hidden]) once more, in the UI's language.
const PAIRING = /orca(?::\/\/|%3A%2F%2F)pair\S*/gi

export function hidePairingCodes(text: string, placeholder: string): string {
  return text.replace(PAIRING, placeholder)
}

export function priorityName(n: number | null | undefined): string | null {
  return typeof n === 'number' && n >= 0 && n <= 7 ? PRIORITIES[n]! : null
}

// --- showing a VM ---------------------------------------------------------------

/** Memory the VM's QEMU process uses (host readings), in bytes, or null. */
export function memoryInUse(host: Record<string, unknown> | null, name: string): number | null {
  const vms = host?.vms as Record<string, { memory_bytes?: unknown }> | undefined
  const bytes = vms?.[name]?.memory_bytes
  return typeof bytes === 'number' && Number.isFinite(bytes) ? bytes : null
}

export function gib(bytes: number): string {
  const v = bytes / 1024 ** 3
  return v >= 100 ? v.toFixed(0) : v.toFixed(1)
}

export function mibAsGib(mib: number): string {
  const v = mib / 1024
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

// --- VM variables (agentbox/vmenv.py) --------------------------------------------------

export const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
export const MAX_ENV_VARIABLES = 64
export const MAX_ENV_VALUE_BYTES = 4096

export interface EnvRow { name: string, value: string }
export type EnvRowProblem = 'name' | 'twice' | 'value' | 'long'

/** The rows of the VM variables dialog as the list PUT /vms/<vm>/env takes, with what is
 * wrong per row (by index). A row without name and value is left out. The names a Safeguard
 * or Agent Box reads are agentbox's to refuse (env_name_reserved), so the UI never drifts
 * from safeguards.py. */
export function envFromRows(rows: readonly EnvRow[]): { env: Record<string, string>, problems: Record<number, EnvRowProblem>, tooMany: boolean } {
  // no prototype: names such as constructor or __proto__ are names like any other
  const env: Record<string, string> = Object.create(null)
  const problems: Record<number, EnvRowProblem> = {}
  rows.forEach((row, i) => {
    const name = row.name.trim()
    if (!name && !row.value) return
    if (!ENV_NAME.test(name)) problems[i] = 'name'
    else if (Object.hasOwn(env, name)) problems[i] = 'twice'
    else if (/[\u0000-\u0008\u000a-\u001f\u007f-\u009f]/.test(row.value)) problems[i] = 'value'
    else if (new TextEncoder().encode(row.value).length > MAX_ENV_VALUE_BYTES) problems[i] = 'long'
    else env[name] = row.value
  })
  return { env, problems, tooMany: Object.keys(env).length > MAX_ENV_VARIABLES }
}

/** What the VM's buttons may do in its state. */
export function lifecycleActions(vm: Pick<Vm, 'state'>) {
  const s = vm.state
  return {
    // a Pause (paused) and a VM paused by libvirt (suspended) resume with start; starting
    // and not_responding run already (libvirt), so Start would do nothing (#54)
    start: ['stopped', 'crashed', 'paused', 'suspended', 'unknown'].includes(s),
    wake: s === 'paused' || s === 'suspended',
    pause: s === 'running',
    restart: ['running', 'starting', 'not_responding', 'crashed', 'suspended'].includes(s),
    reset: s !== 'shutting_down',
  }
}

/** Builds a VM can be pinned to: finished and checked ones with a disk file (images.USABLE).
 * Takes an Image of GET /images (utils/images.ts) or anything with its `builds`. */
export function pinnableBuilds<B extends { status: string, file?: string | null }>(image: { builds: B[] } | undefined): B[] {
  return (image?.builds ?? []).filter(b => (b.status === 'ok' || b.status === 'migrated') && b.file !== null)
}

/** A time from the API in the UI's language, or null. */
export function formatTime(iso: string | null | undefined, locale: string, withSeconds = false): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
  }).format(d)
}

/** A yes/no remembered in this browser (private mode: the default). */
export function storedFlag(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    return v === null ? fallback : v === '1'
  }
  catch {
    return fallback
  }
}

export function storeFlag(key: string, value: boolean) {
  try {
    localStorage.setItem(key, value ? '1' : '0')
  }
  catch {
    // private mode: for this page only
  }
}
