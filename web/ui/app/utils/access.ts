// Types and helpers of the Allowlist, Watchlist and credential views (docs/control-room-api.md:
// Allowlist, Watchlist, GitHub App; the VM's blocked list, Alerts and GitHub tokens).
import type { Translator } from './activity'

/** An Allowlist entry as `GET /allowlist` returns it (times in epoch seconds). */
export interface AllowlistEntry {
  domain: string
  /** `all` or a VM's name */
  scope: string
  expires: number | null
  note: string
  added_by: string
  added: number
  expired: boolean
  remaining: number | null
  uses_24h: number | null
}

/** Durations of a new entry; `permanent` has no expiry. */
export const ENTRY_DURATIONS = ['15min', '1h', '8h', '7d', 'permanent'] as const
export type EntryDuration = typeof ENTRY_DURATIONS[number]
/** What a temporary entry can be extended by. */
export const EXTEND_DURATIONS = ['15min', '1h', '8h', '7d'] as const

/** The API path of an entry (`/allowlist/<scope>/<domain>`). */
export function allowlistEntryPath(e: { scope: string, domain: string }): string {
  return `/allowlist/${encodeURIComponent(e.scope)}/${encodeURIComponent(e.domain)}`
}

/** Seconds an entry still runs at `nowMs`, null when permanent, 0 when expired. */
export function entrySecondsLeft(e: Pick<AllowlistEntry, 'expires'>, nowMs: number): number | null {
  if (e.expires == null) return null
  return Math.max(0, Math.floor(e.expires - nowMs / 1000))
}

/** The domain is covered by an active entry (the entry's domain or a subdomain of it). */
export function coveredBy(domain: string, entries: AllowlistEntry[], nowMs: number): AllowlistEntry | undefined {
  return entries.find(e => (domain === e.domain || domain.endsWith('.' + e.domain))
    && (e.expires == null || e.expires * 1000 > nowMs))
}

/** A destination a VM tried to reach and was refused (last 24 hours). */
export interface BlockedDestination {
  domain: string
  count: number
  last: number
  /** the proxy's reason, English */
  reason: string
  /** the reason as a stable code (#69), null for one agentbox does not know */
  code?: string | null
  /** its parameters: `signals` for `suspicious` */
  params?: Record<string, string>
  /** Signals of its last Alert, empty when unremarkable */
  suspicious: string[]
}

/** A VM's Alerts for one destination (last 24 hours). */
export interface AlertGroup {
  domain: string
  tags: string[]
  count: number
  first: number
  last: number
  seen: boolean
}

/** An Alert group with the VM it belongs to (the Watchlist view lists every VM's). */
export interface VmAlert extends AlertGroup { vm: string }

export interface SignalReport {
  signal: string
  strong: boolean
  /** why it applies, the classifier's English text */
  reason: string
  /** the kind of reason as a stable code, and its parameters (#69) */
  code?: string
  params?: Record<string, string>
  suppressed: boolean
}

export interface FeedState {
  present: boolean
  entries: number | null
  updated: number | null
  attempted: number | null
  error: string | null
  days: number | null
}

/** `GET /watchlist/check?domain=` */
export interface DomainCheck {
  domain: string
  port: number
  signals: SignalReport[]
  alert: boolean
  strong: boolean
  /** the harmless mark that covers it, or null */
  harmless: string | null
  /** the known legitimate domain that covers it, or null */
  legitimate: string | null
  feeds: Record<string, FeedState>
}

/** An entry of the hand-maintained Watchlist (`added` in epoch seconds). */
export interface WatchlistEntry {
  domain: string
  category: string
  strong: boolean
  source: string
  note: string
  added: number
  added_by: string
}

/** Categories of the hand-maintained Watchlist; each becomes the Signal of a hit. All but payload are strong. */
export const WATCHLIST_CATEGORIES = ['exfil', 'c2', 'tunnel', 'paste', 'phishing', 'blockchain', 'payload'] as const

/** A Legitimate domain as `GET /watchlist/legitimate` returns it; built-in ones have no `added`. */
export interface LegitimateDomain {
  domain: string
  note: string
  added: number | null
  added_by: string | null
  builtin: boolean
}

/** `GET /watchlist`; while the hand-maintained Watchlist cannot be read, `unreadable` says why and `entries` is empty */
export interface Watchlist {
  entries: WatchlistEntry[]
  unreadable: string | null
  feeds: Record<string, FeedState>
  feed_interval_hours: number
  young_domain_days: number
}

/** `GET /vms/<vm>/github-tokens` */
export interface GithubTokens {
  vm: string
  repositories: { repository: string, tokens: number, last_token: string | null, refused: number, last_refused: string | null }[]
  /** `reason`: why it was refused, English; `reason_code`: the same as a stable code, `reason_params` its parameters (#69) */
  requests: { time: string, outcome: 'issued' | 'refused' | 'error', repository: string | null, request: string | null, reason: string | null, reason_code?: string | null, reason_params?: Record<string, string>, expires_at: string | null }[]
  truncated: boolean
  retention_days: number
}

export interface GithubApp {
  id: number
  slug: string | null
  name: string | null
  owner: string | null
  html_url: string | null
  install_url: string | null
}

/** `GET /github-app` */
export interface GithubAppOverview {
  app: GithubApp
  installations: { id: number | null, account: string | null, type: string | null, repository_selection: string | null, suspended: boolean, html_url: string | null, vms: string[] }[]
  missing: { owner: string, vms: string[] }[]
}

// --- reasons in the UI language (#69) ----------------------------------------------------------
// agentbox gives a reason a stable code and its parameters next to its English text; the UI words
// the codes it knows and shows the English text for the others, and when a parameter is missing.

type Params = Record<string, string>

const filled = (p: Params, names: string[]) => names.every(n => typeof p[n] === 'string' && p[n] !== '')
const paramsOf = (p: unknown): Params => (p && typeof p === 'object' ? p as Params : {})

/** The parameters each code of a Signal's reason needs (proxy/agentbox-suspicious explain). */
export const SIGNAL_REASONS: Record<string, string[]> = {
  'port': ['port'],
  'metadata': [],
  'raw-ip': [],
  'watchlist': ['domain'],
  'threat-feed': ['domain'],
  'young-feed': ['domain'],
  'brand': ['brand', 'domain'],
  'typo': ['target', 'distance'],
  'tld': ['tld'],
  'random-label': ['label'],
  'many-labels': [],
  'blockchain-pattern': [],
}

/** A Signal's name in the UI language, or the name itself for one the UI does not know. */
export function signalName(signal: string, tr: Translator): string {
  return tr.te(`signals.names.${signal}`) ? tr.t(`signals.names.${signal}`) : signal
}

/** Why a Signal applies, in the UI language; the classifier's English text for an unknown Signal
 * or code, or without the parameters the code needs. */
export function signalReason(s: SignalReport, tr: Translator): string {
  const p = paramsOf(s.params)
  const needs = s.code ? SIGNAL_REASONS[s.code] : undefined
  if (!needs || !tr.te(`signals.names.${s.signal}`) || !filled(p, needs)) return s.reason
  const key = s.code === 'watchlist' && filled(p, ['source']) ? 'watchlistSource' : s.code!
  return tr.t(`signals.reasons.${key}`, p)
}

/** Why the proxy blocked a destination, in the UI language; else its English reason. */
export function blockedReason(r: BlockedDestination, tr: Translator): string {
  const key = `webTab.blocked.reasons.${r.code}`
  if (!r.code || !tr.te(key)) return r.reason
  if (r.code !== 'suspicious') return tr.t(key)
  const p = paramsOf(r.params)
  if (!filled(p, ['signals'])) return r.reason
  return tr.t(key, { signals: p.signals!.split(', ').map(s => signalName(s, tr)).join(', ') })
}

/** The parameters each reason of the token service needs (github-app/agentbox-gh-tokend). */
export const TOKEN_REASONS: Record<string, string[]> = {
  'invalid-request': [],
  'vm-not-registered': [],
  'owner-not-allowed': [],
  'not-installed': [],
  'github-status': ['status'],
}

/** Why the token service refused, in the UI language from its code and parameters; null for a
 * code it does not know or a missing parameter. */
export function refusalText(code: string | null | undefined, params: unknown, tr: Translator): string | null {
  const p = paramsOf(params)
  const needs = code ? TOKEN_REASONS[code] : undefined
  const key = `access.tokens.reasons.${code}`
  return needs && filled(p, needs) && tr.te(key) ? tr.t(key, p) : null
}

/** Why the token service refused a request, in the UI language; else its English text. */
export function tokenReason(r: GithubTokens['requests'][number], tr: Translator): string | null {
  return refusalText(r.reason_code, r.reason_params, tr) ?? r.reason
}

/** A domain as the API takes it: trimmed, lowercase, without a scheme, path or trailing dot. */
export function normalizeDomain(input: string): string {
  let d = input.trim().toLowerCase()
  d = d.replace(/^[a-z][a-z0-9+.-]*:\/\//, '')
  d = d.split(/[/?#]/)[0] ?? ''
  d = d.replace(/:\d+$/, '').replace(/\.$/, '')
  return d
}

/** Rough check before asking the backend (which checks for real). */
export function looksLikeDomain(d: string): boolean {
  return /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(d)
}

/** GitHub owner names (account or organisation). */
export function looksLikeGithubOwner(name: string): boolean {
  return /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(name)
}
