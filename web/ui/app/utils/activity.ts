// The Activity timeline (GET /activity and `activity` events): one list of sign-ins, actions,
// Allowlist changes, Alerts, GitHub tokens, pairings, Builds, Resets, Settings and credentials.
import { refusalText, signalName } from './access'
import { parseApiTime } from './clock'
import { formatHostBytes } from './host'

export const ACTIVITY_KINDS = [
  'signin', 'action', 'allowlist', 'alert', 'github', 'pairing', 'build', 'reset', 'settings', 'credentials',
] as const

export interface ActivityEntry {
  time: string
  vm: string | null
  actor: string | null
  kind: string
  action: string
  outcome: 'ok' | 'failed' | string
  /** agentbox's English text (older entries: German), shown for an action this UI does not know */
  description: string
  /** the action, on entries recorded with a code (#69); older entries have none */
  code?: string
  /** what the UI words the entry from: text values, a flag is "true" when set */
  params?: Record<string, string>
  /** a failure's error code (errors.*) and that error's parameters */
  error?: string
  error_params?: Record<string, string>
  reason?: string
}

export interface ActivityFilter {
  kinds: string[]
  vm: string
  search: string
}

export interface ActivityResult {
  entries: ActivityEntry[]
  truncated: boolean
  retention_days: number
}

export const ACTIVITY_PAGE = 200
export const ACTIVITY_MAX_LINES = 5000

export function activityQuery(filter: ActivityFilter, lines: number): string {
  const params = new URLSearchParams()
  if (filter.kinds.length) params.set('kind', filter.kinds.join(','))
  if (filter.vm) params.set('vm', filter.vm)
  const search = filter.search.trim()
  if (search) params.set('search', search)
  params.set('lines', String(Math.min(lines, ACTIVITY_MAX_LINES)))
  return params.toString()
}

/** Whether an entry passes the filter: the same fields as the backend's search, and `shown`,
 * what the UI shows for it in its language (activityText), so a German search finds the
 * German words too. */
export function matchesFilter(entry: ActivityEntry, filter: ActivityFilter, shown = ''): boolean {
  if (filter.kinds.length && !filter.kinds.includes(entry.kind)) return false
  if (filter.vm && entry.vm !== filter.vm) return false
  const search = filter.search.trim().toLowerCase()
  if (search) {
    const text = [entry.vm, entry.actor, entry.kind, entry.action, entry.description, shown].map(v => v ?? '').join(' ').toLowerCase()
    if (!text.includes(search)) return false
  }
  return true
}

export function entryKey(e: ActivityEntry): string {
  return `${e.time}|${e.vm ?? ''}|${e.kind}|${e.action}|${e.description}`
}

function millis(e: ActivityEntry): number {
  return parseApiTime(e.time)?.getTime() ?? 0
}

/** Adds an entry from the stream (newest first, no duplicates: a resumed stream may repeat). */
export function addEntry(list: ActivityEntry[], entry: ActivityEntry, limit: number): ActivityEntry[] {
  const key = entryKey(entry)
  if (list.some(e => entryKey(e) === key)) return list
  const at = list.findIndex(e => millis(e) < millis(entry))
  const next = at < 0 ? [...list, entry] : [...list.slice(0, at), entry, ...list.slice(at)]
  return next.slice(0, limit)
}

export function isEntry(data: unknown): data is ActivityEntry {
  const e = data as ActivityEntry | null
  return !!e && typeof e.time === 'string' && typeof e.kind === 'string' && typeof e.description === 'string'
}

// --- the words of an entry (#69) -------------------------------------------------------------
// agentbox records each entry with its action as a stable code and the action's parameters;
// the UI words it in its language from those. An entry without a code (recorded before) or
// with an action this table does not know shows agentbox's stored text, and so does one
// missing a parameter its sentence needs. A failure adds the translation of its error code.

/** The translation functions of vue-i18n, as useI18n() gives them. */
export interface Translator {
  t: (key: string, values?: Record<string, unknown>) => string
  te: (key: string) => boolean
}

type Params = Record<string, string>
type Sentence = (p: Params, tr: Translator, e: ActivityEntry) => string | null

const has = (p: Params, ...names: string[]) => names.every(n => typeof p[n] === 'string' && p[n] !== '')
const flag = (p: Params, name: string) => p[name] === 'true'
const say = (tr: Translator, key: string, values: Record<string, unknown> = {}) => tr.t(`activity.actions.${key}`, values)
const part = (tr: Translator, key: string, values: Record<string, unknown> = {}) => tr.t(`activity.parts.${key}`, values)
const joined = (...parts: (string | null)[]) => parts.filter(Boolean).join(' ')

/** The entry a list command names: its domain, or the pattern of a Regex entry (`regex`). */
function target(p: Params, tr: Translator): string {
  return has(p, 'regex') ? part(tr, 'pattern', { pattern: p.regex }) : p.domain!
}
const named = (p: Params) => has(p, 'domain') || has(p, 'regex')

/** The domain or pattern of an Allowlist entry, with "for all VMs" when that is its scope. */
function entryOf(p: Params, tr: Translator): string {
  return p.scope === 'all' ? part(tr, 'allVms', { domain: target(p, tr) }) : target(p, tr)
}

/** The duration of --for after "for" or "by" (Allowlist durations), or "temporarily". */
function duration(p: Params, tr: Translator, word: 'for' | 'by'): string | null {
  const key = `allowlist.durations.${p.duration}`
  if (has(p, 'duration') && tr.te(key)) return part(tr, word, { duration: tr.t(key) })
  return word === 'for' && flag(p, 'temporary') ? part(tr, 'temporary') : null
}

const forced = (p: Params, tr: Translator) => (flag(p, 'force') ? part(tr, 'force') : null)
const thenReset = (p: Params, tr: Translator, text: string) => (has(p, 'reset') ? `${text}, ${part(tr, 'thenReset', { vms: p.reset })}` : text)
const update = (p: Params, tr: Translator, text: string) => (p.reason === 'update' ? part(tr, 'update', { text }) : text)

function reason(p: Params, tr: Translator): string | null {
  const key = `activity.reasons.${p.reason}`
  return has(p, 'reason') && tr.te(key) ? tr.t(key) : null
}

const plain = (key: string): Sentence => (_p, tr) => say(tr, key)
/** An Allowlist command or event: the entry, then the duration (allow, extend) and --force. */
const listed = (key: string, word?: 'for' | 'by'): Sentence => (p, tr) =>
  named(p) ? joined(say(tr, key, { entry: entryOf(p, tr) }), word ? duration(p, tr, word) : null, forced(p, tr)) : null
/** A command on one domain or pattern (Watchlist, Legitimate domains, Harmless marks), and --force. */
const domain = (key: string): Sentence => (p, tr) =>
  named(p) ? joined(say(tr, key, { domain: target(p, tr) }), forced(p, tr)) : null
/** The variant `key` with the parameter `name`, else `other` without it. */
const either = (name: string, key: string, other: string, value = name): Sentence => (p, tr) =>
  has(p, name) ? say(tr, key, { [value]: p[name] }) : say(tr, other)
const flagged = (name: string, key: string, other: string): Sentence => (p, tr) => say(tr, flag(p, name) ? key : other)

/** A step of the Maintenance window: done, or failed (the failure's own words follow). */
const step = (key: string): Sentence => (_p, tr, e) => say(tr, e.outcome === 'failed' ? `${key}Failed` : key)

const NOT_RESPONDING: Record<string, string> = { 'system-not-booted': 'notRespondingSystem', 'orca-not-answering': 'notRespondingOrca' }

export const SENTENCES: Record<string, Sentence> = {
  // commands (agentbox/activity.py DESCRIBE)
  'start': plain('start'),
  'wake': plain('wake'),
  'pause': plain('pause'),
  'restart': plain('restart'),
  'reset': flagged('no_start', 'resetNoStart', 'reset'),
  'focus': (p, tr) => (flag(p, 'off') ? say(tr, 'focusOff') : has(p, 'target') ? say(tr, 'focus', { vm: p.target }) : null),
  'create': either('image', 'createImage', 'create'),
  'delete': flagged('delete_data_disk', 'deleteWithDisk', 'delete'),
  'set-memory': either('mib', 'setMemory', 'setMemoryAny'),
  'set-container-image-cleanup': (p, tr) => {
    const on = flag(p, 'enabled')
    if (has(p, 'enabled', 'days')) return say(tr, on ? 'cleanupOnDays' : 'cleanupOffDays', { days: p.days })
    if (has(p, 'enabled')) return say(tr, on ? 'cleanupOn' : 'cleanupOff')
    return has(p, 'days') ? say(tr, 'cleanupDays', { days: p.days }) : say(tr, 'cleanupAny')
  },
  // the variables as NAME=value (not secret), all of them with replace (none: all removed)
  'set-env': (p, tr) => (flag(p, 'replace') ? (has(p, 'env') ? say(tr, 'replaceEnv', { env: p.env }) : say(tr, 'clearEnv'))
    : has(p, 'env') ? say(tr, 'setEnv', { env: p.env }) : null),
  'unset-env': (p, tr) => (has(p, 'names') ? say(tr, 'unsetEnv', { names: p.names }) : null),
  'allow': listed('allow', 'for'),
  'disallow': listed('disallow'),
  'extend': listed('extend', 'by'),
  'make-permanent': listed('makePermanent'),
  'set-note': listed('setNote'),
  'migrate-allowlist': plain('migrateAllowlist'),
  'open-web': (p, tr) => (has(p, 'minutes') ? say(tr, 'openWeb', { minutes: p.minutes }) : null),
  'close-web': plain('closeWeb'),
  'mark-seen': either('domain', 'markSeenDomain', 'markSeen'),
  'mark-harmless': domain('markHarmless'),
  'unmark-harmless': domain('unmarkHarmless'),
  'watch-add': (p, tr) => (named(p)
    ? joined(has(p, 'category') ? say(tr, 'watchAddCategory', { domain: target(p, tr), category: p.category }) : say(tr, 'watchAdd', { domain: target(p, tr) }), forced(p, tr))
    : null),
  'watch-remove': domain('watchRemove'),
  'watch-set-note': domain('watchSetNote'),
  'legitimate-add': domain('legitimateAdd'),
  'legitimate-remove': domain('legitimateRemove'),
  'legitimate-set-note': domain('legitimateSetNote'),
  // the command, or one list taken over (lists.py: list, legacy, migrated, skipped or created)
  'migrate-lists': (p, tr) => {
    if (!has(p, 'list')) return say(tr, 'migrateLists')
    const key = `activity.lists.${p.list}`
    if (!tr.te(key) || !has(p, 'legacy')) return null
    if (flag(p, 'created')) return say(tr, 'listCreated', { list: tr.t(key), legacy: p.legacy })
    return has(p, 'migrated', 'skipped') ? say(tr, 'listMigrated', { list: tr.t(key), legacy: p.legacy, migrated: p.migrated, skipped: p.skipped }) : null
  },
  'set-owners': (p, tr) => (has(p, 'owners') ? say(tr, 'setOwners', { owners: p.owners }) : null),
  'set-claude-token': plain('setClaudeToken'),
  'set-opencode-key': plain('setOpencodeKey'),
  'set-github-app': either('app_id', 'setGithubAppId', 'setGithubApp', 'id'),
  'remove-github-app': either('app_id', 'removeGithubAppId', 'removeGithubApp', 'id'),
  'pair': plain('pair'),
  'observe': plain('observe'),
  'unpair': plain('unpair'),
  'revoke-pairings': plain('revokePairings'),
  'migrate': plain('migrate'),
  'regenerate': plain('regenerate'),
  'setup': plain('setup'),
  'set-settings': either('settings', 'setSettings', 'setSettingsAny'),
  'build': (p, tr) => (flag(p, 'all') ? thenReset(p, tr, say(tr, 'buildStartAll'))
    : has(p, 'image') ? thenReset(p, tr, say(tr, 'buildStart', { image: p.image })) : null),
  'update-now': (p, tr) => thenReset(p, tr, say(tr, 'updateNow')),
  'set-image': (p, tr) => (!has(p, 'image') ? null
    : has(p, 'build') ? say(tr, 'setImagePinned', { image: p.image, build: p.build }) : say(tr, 'setImageCurrent', { image: p.image })),
  'import': (p, tr) => (!has(p, 'folder', 'target') ? say(tr, 'importAny')
    : has(p, 'files', 'bytes') ? say(tr, 'importDone', { folder: p.folder, target: p.target, files: p.files, bytes: p.bytes })
      : say(tr, 'importFolder', { folder: p.folder, target: p.target })),
  // the Control Room's sign-ins
  'signin': plain('signin'),
  'signin.failed': plain('signinFailed'),
  'signin.locked': plain('signinLocked'),
  'signout': plain('signout'),
  // events
  'alert': (p, tr) => (!has(p, 'domain') ? null
    : has(p, 'signals') ? say(tr, 'alertSignals', { domain: p.domain, signals: p.signals!.split(', ').map(s => signalName(s, tr)).join(', ') })
      : say(tr, 'alert', { domain: p.domain })),
  // the GitHub token service's lines (agentbox/activity.py github_entry); an error adds its message
  'github.token': (p, tr) => (has(p, 'repository') ? say(tr, 'githubToken', { repository: p.repository }) : null),
  'github.refused': (p, tr) => {
    const reason = has(p, 'request') ? refusalText(p.reason, p, tr) : null
    return reason ? say(tr, 'githubRefused', { request: p.request, reason }) : null
  },
  'github.error': (p, tr) => (has(p, 'request') ? say(tr, 'githubError', { request: p.request }) : null),
  'allowlist.expired': listed('expired'),
  'container-image-cleanup.removed': (p, tr) => (has(p, 'removed', 'freed_bytes')
    ? say(tr, Number(p.removed) === 1 ? 'cleanupRemovedOne' : 'cleanupRemoved',
      { removed: p.removed, freed: formatHostBytes(Number(p.freed_bytes)) }) : null),
  'container-image-cleanup.failed': plain('cleanupFailed'),
  'wake.not-responding': (p, tr) => (NOT_RESPONDING[p.reason ?? ''] ? say(tr, NOT_RESPONDING[p.reason!]!) : null),
  'reset.automatic': (p, tr) => {
    if (p.reason === 'update') return has(p, 'image', 'build') ? say(tr, 'resetAutomaticUpdate', { image: p.image, build: p.build }) : null
    const why = reason(p, tr)
    return why ? say(tr, 'resetAutomatic', { reason: why }) : null
  },
  'reset.postponed': (p, tr) => {
    const why = reason(p, tr)
    if (!why) return null
    return has(p, 'image', 'build') ? say(tr, 'resetPostponedUpdate', { image: p.image, build: p.build, reason: why }) : say(tr, 'resetPostponed', { reason: why })
  },
  'reset.failed': (p, tr) => (has(p, 'image', 'build') ? say(tr, 'resetFailedUpdate', { image: p.image, build: p.build }) : say(tr, 'resetFailed')),
  'reset.update': (p, tr, e) => {
    if (!has(p, 'image')) return null
    if (e.outcome !== 'failed') return has(p, 'build') ? say(tr, flag(p, 'started') ? 'resetUpdateStarted' : 'resetUpdate', { image: p.image, build: p.build }) : null
    if (flag(p, 'skipped')) return has(p, 'failed_builds') ? say(tr, 'resetUpdateBuildFailed', { image: p.image, builds: p.failed_builds }) : say(tr, 'resetUpdateSkipped', { image: p.image })
    return say(tr, flag(p, 'running') ? 'resetUpdateRunning' : 'resetUpdateOff', { image: p.image })
  },
  'build.ok': (p, tr) => {
    if (!has(p, 'image', 'build')) return null
    const done = has(p, 'release') ? say(tr, 'buildOkRelease', { image: p.image, build: p.build, release: p.release })
      : say(tr, 'buildOk', { image: p.image, build: p.build })
    return update(p, tr, has(p, 'removed') ? `${done}, ${part(tr, 'removed', { n: p.removed })}` : done)
  },
  'build.failed': (p, tr) => (flag(p, 'all') ? update(p, tr, say(tr, 'buildAllFailed'))
    : has(p, 'image', 'build') ? update(p, tr, say(tr, 'buildFailed', { image: p.image, build: p.build })) : null),
  'build.rejected': (p, tr) => (has(p, 'image', 'build') ? update(p, tr, say(tr, 'buildRejected', { image: p.image, build: p.build })) : null),
  'build.unneeded': plain('buildUnneeded'),
  'build.skipped': (p, tr) => (has(p, 'images') ? say(tr, 'buildSkipped', { images: p.images }) : null),
  'build.automatic': (p, tr, e) => (has(p, 'images') ? say(tr, e.outcome === 'failed' ? 'buildAutomaticFailed' : 'buildAutomatic', { images: p.images }) : null),
  // the steps of the Maintenance window, in their order (agentbox/maintenance.py)
  'maintenance.feeds': step('maintenanceFeeds'),
  'maintenance.versions': step('maintenanceVersions'),
  // left to the old rebuild timer while it is still there (reason old-timer)
  'maintenance.rebuild': (p, tr, e) => (p.reason === 'old-timer' && e.outcome !== 'failed'
    ? say(tr, 'maintenanceRebuildOldTimer') : step('maintenanceRebuild')(p, tr, e)),
  'maintenance.reset-check': step('maintenanceResetCheck'),
  // the timer's window of this turn of maintenance_time ran already (reason already-ran)
  'maintenance.skipped': (p, tr) => (p.reason === 'already-ran' && has(p, 'time') ? say(tr, 'maintenanceSkipped', { time: p.time }) : null),
}

/** The generic error code: agentbox's own message says more than its translation would. */
const GENERIC_ERROR = 'action_failed'

/** What the timeline shows for an entry in the UI's language: the sentence of its action; for a
 * failure followed by the translation of its error code, or, without one, by what failed as
 * agentbox said it (`message`, English); else agentbox's stored text. */
export function activityText(e: ActivityEntry, tr: Translator): string {
  const sentence = e.code ? SENTENCES[e.code] : undefined
  const p: Params = e.params && typeof e.params === 'object' ? e.params : {}
  const text = sentence ? sentence(p, tr, e) : null
  if (!text) return e.description
  if (e.outcome !== 'failed') return text
  if (!e.error) return has(p, 'message') ? `${text}: ${p.message}` : text
  const key = `errors.${e.error}`
  if (e.error === GENERIC_ERROR || !tr.te(key)) return e.description
  return `${text}: ${tr.t(key, { field: '', ...(e.error_params ?? {}) })}`
}
