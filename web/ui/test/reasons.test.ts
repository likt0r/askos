// Reasons in the UI language (#69), as units over the real messages: a Signal's reason, why a
// destination was blocked and why a GitHub token was refused, each worded from its code and
// parameters; agentbox's English text where the UI cannot word it.
import { describe, expect, it } from 'vitest'
import en from '../i18n/locales/en'
import de from '../i18n/locales/de'
import { blockedReason, SIGNAL_REASONS, signalReason, TOKEN_REASONS, tokenReason } from '~/utils/access'
import type { BlockedDestination, GithubTokens, SignalReport } from '~/utils/access'
import type { Translator } from '~/utils/activity'

/** A translator over a locale's messages: dotted keys, `{name}` filled in (enough for these). */
function translator(messages: object): Translator {
  const find = (key: string): unknown => key.split('.').reduce<unknown>(
    (o, k) => (o && typeof o === 'object' ? (o as Record<string, unknown>)[k] : undefined), messages)
  return {
    te: key => typeof find(key) === 'string',
    t: (key, values = {}) => String(find(key)).replace(/\{(\w+)\}/g, (_, n: string) => String(values[n])),
  }
}

const EN = translator(en)
const DE = translator(de)

const signal = (s: Partial<SignalReport>): SignalReport =>
  ({ signal: 'lookalike', strong: true, suppressed: false, reason: 'the English text', ...s })
const blocked = (b: Partial<BlockedDestination>): BlockedDestination =>
  ({ domain: 'x.example', count: 1, last: 0, reason: 'the English text', suspicious: [], ...b })
const request = (r: Partial<GithubTokens['requests'][number]>): GithubTokens['requests'][number] =>
  ({ time: 't', outcome: 'refused', repository: 'a/b', request: 'a/b', reason: 'the English text', expires_at: null, ...r })

describe('a Signal\'s reason', () => {
  it('has words for every code in both languages', () => {
    for (const tr of [EN, DE]) {
      for (const code of [...Object.keys(SIGNAL_REASONS), 'watchlistSource']) {
        expect(tr.te(`signals.reasons.${code}`), code).toBe(true)
      }
    }
  })

  it('is worded from its code and parameters', () => {
    const typo = signal({ code: 'typo', params: { target: 'github', distance: '1' } })
    expect(signalReason(typo, EN)).toBe('A typo of github (distance 1)')
    expect(signalReason(typo, DE)).toBe('Ein Tippfehler von github (Abstand 1)')
    expect(signalReason(signal({ signal: 'metadata', code: 'metadata', params: {} }), EN)).toBe('The address of a cloud metadata service')
    expect(signalReason(signal({ signal: 'exfil', code: 'watchlist', params: { domain: 'w.example' } }), EN)).toBe('Watchlist: w.example')
    expect(signalReason(signal({ signal: 'exfil', code: 'watchlist', params: { domain: 'w.example', source: 'Incident' } }), EN))
      .toBe('Watchlist: w.example (Incident)')
    expect(signalReason(signal({ signal: 'young', code: 'young-feed', params: { domain: 'new.example' } }), DE))
      .toBe('HaGeZi-Liste neu registrierter Domains: new.example')
  })

  it('keeps the English text without a code or a parameter, or for a code or Signal it does not know', () => {
    expect(signalReason(signal({}), DE)).toBe('the English text')
    expect(signalReason(signal({ code: 'typo', params: { target: 'github' } }), DE)).toBe('the English text')
    expect(signalReason(signal({ code: 'typo', params: null as unknown as Record<string, string> }), DE)).toBe('the English text')
    expect(signalReason(signal({ code: 'made-up', params: {} }), DE)).toBe('the English text')
    // a Signal from before the rename, and one added later
    expect(signalReason(signal({ signal: 'roh-ip', code: 'raw-ip', params: {} }), DE)).toBe('the English text')
    expect(signalReason(signal({ signal: 'shiny', code: 'tld', params: { tld: 'x' } }), DE)).toBe('the English text')
  })
})

describe('why a destination was blocked', () => {
  it('is worded from its code, the Signals by their names', () => {
    expect(blockedReason(blocked({ code: 'not-allowed', params: {} }), DE)).toBe('nicht auf der Allowlist')
    expect(blockedReason(blocked({ code: 'suspicious', params: { signals: 'exfil, raw-ip, roh-ip' } }), EN))
      .toBe('strong Signals: Exfiltration, Raw IP address, roh-ip')
    // also a regex Allowlist entry that matched but does not allow it (ADR 0013)
    expect(blockedReason(blocked({ code: 'watchlist-unreadable', params: {} }), EN)).toBe('the Watchlist cannot be read (Open web and Regex entries closed)')
    // a connection the proxy took no destination from
    expect(blockedReason(blocked({ code: 'not-tls', params: {} }), DE)).toBe('kein TLS-Handshake (die VM hat etwas anderes als HTTPS gesendet)')
    expect(blockedReason(blocked({ code: 'no-server-name', params: {} }), EN)).toBe('no server name in the TLS handshake')
  })

  it('keeps the proxy\'s text without a code, for one it does not know, or without the Signals', () => {
    expect(blockedReason(blocked({}), DE)).toBe('the English text')
    expect(blockedReason(blocked({ code: null }), DE)).toBe('the English text')
    expect(blockedReason(blocked({ code: 'made-up' }), DE)).toBe('the English text')
    expect(blockedReason(blocked({ code: 'suspicious', params: {} }), DE)).toBe('the English text')
  })
})

describe('why a GitHub token was refused', () => {
  it('has words for every reason in both languages', () => {
    for (const tr of [EN, DE]) {
      for (const code of Object.keys(TOKEN_REASONS)) expect(tr.te(`access.tokens.reasons.${code}`), code).toBe(true)
    }
  })

  it('is worded from its code and parameters, else the token service\'s text', () => {
    expect(tokenReason(request({ reason_code: 'not-installed' }), EN)).toBe('no GitHub App of the Host is installed on the repository')
    expect(tokenReason(request({ reason_code: 'not-installed', reason_params: { app_id: '9' } }), EN)).toBe('GitHub App 9 is no longer installed on the repository')
    expect(tokenReason(request({ reason_code: 'github-status', reason_params: { status: '502', app_id: '9' } }), DE)).toBe('GitHub antwortete GitHub App 9 mit dem Fehler 502')
    expect(tokenReason(request({ reason_code: 'github-status', reason_params: { status: '502' } }), EN)).toBe('GitHub answered with the error 502')
    expect(tokenReason(request({ reason_code: 'github-status', reason_params: { status: '502' } }), DE)).toBe('GitHub antwortete mit dem Fehler 502')
    expect(tokenReason(request({ reason_code: 'app-key', reason_params: { app_id: '9' } }), EN)).toBe('the key of GitHub App 9 cannot sign (missing or broken)')
    expect(tokenReason(request({ reason_code: 'github-status' }), DE)).toBe('the English text')
    expect(tokenReason(request({ reason_code: 'owner-not-allowed' }), DE)).toBe('ihr Owner ist kein GitHub-Owner der VM')
    expect(tokenReason(request({ reason_code: null }), DE)).toBe('the English text')
    expect(tokenReason(request({ reason_code: 'made-up' }), DE)).toBe('the English text')
    expect(tokenReason(request({ outcome: 'issued', reason: null }), DE)).toBeNull()
  })
})
