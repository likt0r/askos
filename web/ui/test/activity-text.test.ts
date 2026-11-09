// Activity entries in the UI language (#69): the sentence of an entry's code (its action) and
// parameters, in English and German; a failure with its translated error code; agentbox's stored
// text for an action this UI does not know, for entries from before (no code), for an error code
// it does not know and for a sentence missing a parameter.
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import ActivityList from '~/components/activity/ActivityList.vue'
import { activityText, SENTENCES } from '~/utils/activity'
import type { ActivityEntry, Translator } from '~/utils/activity'

const at = (n: number) => `2026-11-09T12:${String(59 - n).padStart(2, '0')}:00.000000Z`

function coded(n: number, action: string, params: Record<string, string>, description: string,
  over: Partial<ActivityEntry> = {}): ActivityEntry {
  return { time: at(n), vm: 'work', actor: 'timer', kind: 'action', action, outcome: 'ok', description, code: action, params, ...over }
}

const ENTRIES: ActivityEntry[] = [
  coded(0, 'allow', { domain: 'pypi.org', scope: 'all', duration: '1h' }, 'Allow pypi.org for all VMs for 1 hour'),
  coded(1, 'allow', { domain: 'evil.example', scope: 'work', temporary: 'true' },
    'Allow evil.example temporarily: evil.example is suspicious (exfil). Allowing it only with --force',
    { outcome: 'failed', error: 'domain_suspicious', error_params: { domain: 'evil.example' } }),
  coded(2, 'extend', { domain: 'a.example', scope: 'all', duration: '8h', force: 'true' },
    'Extend the Allowlist entry a.example for all VMs by 8 hours with --force'),
  coded(3, 'reset.postponed', { reason: 'gui-connected', image: 'base', build: '20261101' },
    'Automatic Reset onto Build base-20261101 postponed (a device is connected)'),
  coded(4, 'build.ok', { image: 'base', build: '20261109-120000', release: '0.3.0', removed: '2', reason: 'update' },
    'Update: Build base-20261109-120000 done (Release 0.3.0), 2 old Builds removed', { vm: null }),
  coded(5, 'signin.failed', {}, 'Sign-in with a wrong password', { vm: null, outcome: 'failed' }),
  coded(6, 'alert', { domain: 'paste.example', signals: 'paste, young' }, 'Alert: paste.example (paste,young)'),
  coded(7, 'reset', { no_start: 'true' }, 'Reset without start: interrupted',
    { outcome: 'failed', error: 'interrupted', error_params: {} }),
  coded(8, 'reset.update', { image: 'base', skipped: 'true', failed_builds: 'base' },
    'No Reset after the Update: the Build of base failed', { outcome: 'failed' }),
  coded(9, 'wake.not-responding', { reason: 'orca-not-answering' },
    'The VM is not responding: Orca does not answer (the guest agent answers)', { outcome: 'failed' }),
  // a failure without an error code: what failed, as agentbox said it
  coded(10, 'build.failed', { image: 'base', build: '20261109-120000', message: 'podman build failed (exit 1)' },
    'Build base-20261109-120000 failed: podman build failed (exit 1)', { vm: null, outcome: 'failed' }),
  // no Release known: no part for it
  coded(11, 'build.ok', { image: 'base', build: '20261109-120000' }, 'Build base-20261109-120000 done (Release None)',
    { vm: null }),
  // agentbox's stored text, unchanged
  coded(12, 'frobnicate', { thing: 'x' }, 'Frobnicate x'),
  { time: at(13), vm: 'work', actor: 'x', kind: 'allowlist', action: 'allow', outcome: 'ok', description: 'Freigabe example.org' },
  coded(14, 'pause', {}, 'Pause: something new', { outcome: 'failed', error: 'made_up_code' }),
  coded(15, 'allow', {}, 'Allow : No such VM: x', { outcome: 'failed', error: 'vm_not_found' }),
  // the generic code: agentbox's own message says more
  coded(16, 'reset', {}, 'Reset: error: failed to get domain', { outcome: 'failed', error: 'action_failed' }),
]

const STORED = ['Frobnicate x', 'Freigabe example.org', 'Pause: something new', 'Allow : No such VM: x',
  'Reset: error: failed to get domain']

const EN = [
  'Allow pypi.org for all VMs for 1 hour',
  'Allow evil.example temporarily: This destination has strong Signals and can only be allowed on the Host.',
  'Extend the Allowlist entry a.example for all VMs by 8 hours with --force',
  'Automatic Reset onto Build base-20261101 postponed (a device is connected)',
  'Update: Build base-20261109-120000 done (Release 0.3.0), old Builds removed: 2',
  'Sign-in with a wrong password',
  'Alert: paste.example (Paste service, Young domain)',
  'Reset without start: Interrupted.',
  'No Reset after the Update of base: the Build of base failed',
  'The VM is not responding: Orca does not answer (the guest agent answers)',
  'Build base-20261109-120000 failed: podman build failed (exit 1)',
  'Build base-20261109-120000 done',
  ...STORED,
]

const DE = [
  'Freigabe pypi.org für alle VMs für 1 Stunde',
  'Freigabe evil.example vorübergehend: Dieses Ziel hat starke Signale und kann nur auf dem Host erlaubt werden.',
  'Freigabe a.example für alle VMs verlängern um 8 Stunden mit --force',
  'Automatischer Reset auf Build base-20261101 verschoben (ein Gerät ist verbunden)',
  'Update: Build base-20261109-120000 fertig (Release 0.3.0), alte Builds entfernt: 2',
  'Anmeldung mit falschem Passwort',
  'Verdacht: paste.example (Paste-Dienst, Junge Domain)',
  'Reset ohne Start: Abgebrochen.',
  'Kein Reset nach dem Update von base: der Build von base ist gescheitert',
  'VM antwortet nicht: Orca antwortet nicht (der Gast-Agent antwortet)',
  'Build base-20261109-120000 gescheitert: podman build failed (exit 1)',
  'Build base-20261109-120000 fertig',
  ...STORED,
]

async function list(entries: ActivityEntry[]) {
  const wrapper = await mountSuspended(ActivityList, { props: { entries } })
  const texts = wrapper.findAll('[data-testid="activity-text"]')
  return {
    // without the words for screen readers that say it failed
    shown: texts.map(el => el.text().replace(el.find('.sr-only').text(), '').trim()),
    titles: texts.map(el => el.attributes('title')),
  }
}

describe('Activity in the UI language', () => {
  beforeEach(async () => {
    await useNuxtApp().$i18n.setLocale('en')
  })
  afterEach(async () => {
    await useNuxtApp().$i18n.setLocale('en')
  })

  it('words each entry from its code and parameters, a failure with its error', async () => {
    const { shown, titles } = await list(ENTRIES)
    expect(shown).toEqual(EN)
    // agentbox's text on hover where it says something else
    expect(titles[0]).toBeUndefined()
    expect(titles[1]).toBe(ENTRIES[1]!.description)
    expect(titles[10]).toBeUndefined()
    expect(titles[13]).toBeUndefined()
  })

  it('in German, and what it cannot word stays as stored', async () => {
    await useNuxtApp().$i18n.setLocale('de')
    const { shown, titles } = await list(ENTRIES)
    expect(shown).toEqual(DE)
    expect(titles[0]).toBe('Allow pypi.org for all VMs for 1 hour')
    expect(titles.slice(12)).toEqual([undefined, undefined, undefined, undefined, undefined])
  })

  it('words the token service\'s entries, a refusal with its reason, an error with its message', async () => {
    const github = (n: number, action: string, params: Record<string, string>, description: string, outcome = 'failed') =>
      coded(n, action, params, description, { kind: 'github', actor: 'VM work', outcome })
    const entries = [
      github(0, 'github.token', { repository: 'likt0r/askos', expires: '2026-11-09T13:00:00Z' },
        'GitHub token for likt0r/askos until 2026-11-09T13:00:00Z', 'ok'),
      github(1, 'github.refused', { request: 'evil/repo', repository: 'evil/repo', reason: 'owner-not-allowed' },
        'GitHub token for evil/repo refused: evil is not a GitHub owner of the VM work'),
      github(2, 'github.error', { request: 'a/b', repository: 'a/b', message: 'TimeoutError(\'timed out\')' },
        'GitHub token for a/b failed: TimeoutError(\'timed out\')'),
      github(3, 'github.refused', { request: 'a/b', repository: 'a/b', reason: 'github-status', status: '502' },
        'GitHub token for a/b refused: GitHub answered 502'),
      // a reason this UI does not know, and one without the parameter it needs
      github(4, 'github.refused', { request: 'a/b', reason: 'made-up' }, 'GitHub token for a/b refused: something new'),
      github(5, 'github.refused', { request: 'a/b', reason: 'github-status' }, 'GitHub token for a/b refused: GitHub answered 500'),
    ]
    expect((await list(entries)).shown).toEqual([
      'GitHub token for likt0r/askos',
      'GitHub token for evil/repo refused: its owner is not a GitHub owner of the VM',
      'GitHub token for a/b failed: TimeoutError(\'timed out\')',
      'GitHub token for a/b refused: GitHub answered with the error 502',
      'GitHub token for a/b refused: something new',
      'GitHub token for a/b refused: GitHub answered 500',
    ])
    await useNuxtApp().$i18n.setLocale('de')
    expect((await list(entries)).shown).toEqual([
      'GitHub-Token für likt0r/askos',
      'GitHub-Token für evil/repo abgelehnt: ihr Owner ist kein GitHub-Owner der VM',
      'GitHub-Token für a/b gescheitert: TimeoutError(\'timed out\')',
      'GitHub-Token für a/b abgelehnt: GitHub antwortete mit dem Fehler 502',
      'GitHub token for a/b refused: something new',
      'GitHub token for a/b refused: GitHub answered 500',
    ])
  })

  it('has the words of every sentence in both languages', async () => {
    const params: Record<string, string> = {
      domain: 'd.example', scope: 'all', duration: '1h', target: 'priv', image: 'base', build: '20261101', mib: '8192',
      minutes: '30', category: 'paste', list: 'watchlist', legacy: 'old-file', migrated: '2', skipped: '0', owners: 'a',
      app_id: '1', settings: 'reset_time', reason: 'idle', signals: 'young', images: 'base', release: '0.3.0',
      repository: 'a/b', request: 'a/b',
    }
    // a reason the sentence knows, where it is a code of its own
    const reasons: Record<string, string> = { 'wake.not-responding': 'system-not-booted', 'github.refused': 'not-installed' }
    const entries = Object.keys(SENTENCES).map((action, n) => coded(n % 60, action,
      reasons[action] ? { ...params, reason: reasons[action] } : params, 'STORED'))
    for (const language of ['en', 'de']) {
      await useNuxtApp().$i18n.setLocale(language)
      const { shown } = await list(entries)
      shown.forEach((text, i) => {
        expect(text, `${language} ${entries[i]!.action}`).not.toBe('STORED')
        expect(text, `${language} ${entries[i]!.action}`).not.toMatch(/activity\.|\{|\}/)
      })
    }
  })
})

describe('Activity text (unit)', () => {
  // a stand-in for vue-i18n: every key known except errors.made_up_code, values shown
  const tr: Translator = {
    t: (key, values) => (values && Object.keys(values).length ? `${key} ${JSON.stringify(values)}` : key),
    te: key => key !== 'errors.made_up_code' && !key.startsWith('activity.lists.nope'),
  }

  it('keeps the stored text without a code, or for an action it does not know', () => {
    expect(activityText(ENTRIES[11]!, tr)).toBe('Freigabe example.org')
    expect(activityText(ENTRIES[10]!, tr)).toBe('Frobnicate x')
    expect(activityText({ ...ENTRIES[0]!, code: undefined }, tr)).toBe(ENTRIES[0]!.description)
  })

  it('keeps it when a parameter the sentence needs is missing or not text', () => {
    expect(activityText(coded(0, 'open-web', {}, 'Open web for x minutes'), tr)).toBe('Open web for x minutes')
    expect(activityText(coded(0, 'migrate-lists', { list: 'nope', legacy: 'f' }, 'stored'), tr)).toBe('stored')
    const odd = { ...coded(0, 'focus', {}, 'Focus on 5'), params: { target: 5 } as unknown as Record<string, string> }
    expect(activityText(odd, tr)).toBe('Focus on 5')
    expect(activityText({ ...coded(0, 'start', {}, 'Start'), params: undefined }, tr)).toBe('activity.actions.start')
  })

  it('adds a failure\'s translated error, or keeps the stored text for an error it does not know', () => {
    const failed = coded(0, 'pause', {}, 'Pause: x', { outcome: 'failed', error: 'vm_paused', error_params: { vm: 'work' } })
    expect(activityText(failed, tr)).toBe('activity.actions.pause: errors.vm_paused {"field":"","vm":"work"}')
    expect(activityText({ ...failed, error: 'made_up_code' }, tr)).toBe('Pause: x')
    // the generic code: agentbox's own message
    expect(activityText({ ...failed, error: 'action_failed' }, tr)).toBe('Pause: x')
    // a failure without an error code is the sentence, with what failed when agentbox said it;
    // an error only counts on a failure
    expect(activityText({ ...failed, error: undefined }, tr)).toBe('activity.actions.pause')
    expect(activityText({ ...failed, error: undefined, params: { message: 'disk full' } }, tr))
      .toBe('activity.actions.pause: disk full')
    expect(activityText({ ...failed, outcome: 'ok' }, tr)).toBe('activity.actions.pause')
  })
})
