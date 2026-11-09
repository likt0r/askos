// Settings (#22) and this browser's notifications (#21): all keys with label, value and
// default; what only the Host may change is not a field; checks before the request; one
// PATCH with the changed keys; the backend's problems at their field; notification
// preferences per browser.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { SETTING_SPECS, settingProblem, settingValue, toSend, settingChanged } from '~/utils/settings'
import { resetNotificationsForTests } from '~/composables/useNotifications'
import { LANGUAGE_KEY } from '~/utils/language'
import { useSession } from '~/composables/useSession'
import { ok, openPage, useFreshApp } from './system-helpers'
import en from '../i18n/locales/en'

// as agentbox/settings.py describes them (German `allowed` and `applies_later` texts included)
const LATER = {
  builds_kept: 'ab dem naechsten Build',
  package_min_age_days: 'ab dem naechsten Build',
  orca_version: 'ab dem naechsten Build',
  default_memory_mib: 'fuer neue VMs',
  default_data_disk_gib: 'fuer neue VMs',
  default_image: 'fuer neue VMs',
  temporary_entry_default: 'fuer neue befristete Freigaben',
  young_domain_days: 'beim naechsten Abruf der Listen',
  session_hours: 'bei der naechsten Anmeldung',
  default_language: 'ab der naechsten Anmeldung oder dem naechsten Laden des Leitstands',
} as Record<string, string>

const DEFAULTS: Record<string, string | number | boolean> = {
  reset_age_days: 7, reset_time: '04:00', auto_update: true, rebuild_day: 'sun', rebuild_time: '03:00', builds_kept: 3,
  default_memory_mib: 26624, default_data_disk_gib: 300, default_image: 'base', open_web_max_minutes: 240,
  temporary_entry_default: '1h', young_domain_days: 14, feed_interval_hours: 12, package_min_age_days: 2,
  orca_version: '', activity_retention_days: 90, session_hours: 12, default_language: 'browser',
}

function settingsBody(values: Record<string, string | number | boolean> = {}) {
  return {
    settings: Object.fromEntries(Object.entries(DEFAULTS).map(([key, def]) => {
      const value = values[key] ?? def
      return [key, { value, default: def, is_default: value === def, applies_later: LATER[key] ?? null, allowed: 'ganze Zahl von 1 bis 99' }]
    })),
  }
}

type Page = Awaited<ReturnType<typeof openPage>>['wrapper']
const field = (w: Page, key: string) => w.find(`[data-testid="setting"][data-key="${key}"]`)
const input = (w: Page, key: string) => w.find(`#setting-${key}`)
const save = (w: Page) => w.find('[data-testid="settings-save"]')
const patches = (calls: { method: string }[]) => calls.filter(c => c.method === 'PATCH') as { method: string, path: string, headers: Record<string, string>, body: unknown }[]
/** GET /settings so far (only the form asks; the language comes with GET /session). */
const reads = (calls: { method: string, path: string }[]) => calls.filter(c => c.method === 'GET' && c.path === '/settings').length

/** Types into a field and waits for the page. */
async function type(w: Page, key: string, text: string) {
  await input(w, key).setValue(text)
  await flushPromises()
}

const changedBody = { changed: {}, applies_later: {}, regenerated: null }

describe('settings (units)', () => {
  const doc = readFileSync(resolve(process.cwd(), '../../docs/control-room-api.md'), 'utf8')
  const section = doc.slice(doc.indexOf('Settings keys the Control Room may change'), doc.indexOf('## Live events'))
  const listed = [...section.slice(0, section.indexOf('The password')).matchAll(/`([a-z_]+)`/g)].map(m => m[1]!)

  it('knows the keys of docs/control-room-api.md, no more and no fewer', () => {
    expect(listed.length).toBe(18)
    expect([...listed].sort()).toEqual(Object.keys(SETTING_SPECS).sort())
    expect(Object.keys(SETTING_SPECS).sort()).toEqual(Object.keys(DEFAULTS).sort())
  })

  it('has a label and help for each key, and a note for those that apply later', () => {
    for (const key of Object.keys(SETTING_SPECS)) {
      expect(en.settings.fields[key as keyof typeof en.settings.fields], key).toBeTruthy()
    }
    expect(Object.keys(en.settings.later).sort()).toEqual(Object.keys(LATER).sort())
  })

  it('judges what is typed like the backend', () => {
    expect(settingProblem('reset_age_days', '7')).toBeNull()
    expect(settingProblem('reset_age_days', '0')).toEqual({ code: 'int', min: 1, max: 365 })
    expect(settingProblem('reset_age_days', '366')).toEqual({ code: 'int', min: 1, max: 365 })
    expect(settingProblem('reset_age_days', '1.5')).toEqual({ code: 'int', min: 1, max: 365 })
    expect(settingProblem('reset_age_days', '-3')).toEqual({ code: 'int', min: 1, max: 365 })
    expect(settingProblem('reset_age_days', '')).toEqual({ code: 'empty' })
    expect(settingProblem('default_memory_mib', '1023')).toEqual({ code: 'int', min: 1024, max: 1048576 })
    expect(settingProblem('reset_time', '23:59')).toBeNull()
    expect(settingProblem('reset_time', '24:00')).toEqual({ code: 'time' })
    expect(settingProblem('reset_time', '4:00')).toEqual({ code: 'time' })
    expect(settingProblem('rebuild_day', 'sun')).toBeNull()
    expect(settingProblem('rebuild_day', 'sunday')?.code).toBe('choice')
    expect(settingProblem('young_domain_days', '21')).toBeNull()
    expect(settingProblem('young_domain_days', '10')?.code).toBe('choice')
    expect(settingProblem('default_image', 'base-2')).toBeNull()
    expect(settingProblem('default_image', 'Base')).toEqual({ code: 'name' })
    expect(settingProblem('default_image', '-base')).toEqual({ code: 'name' })
    expect(settingProblem('default_image', 'a'.repeat(33))).toEqual({ code: 'name' })
    expect(settingProblem('default_image', 'a'.repeat(32))).toBeNull()
    expect(settingProblem('some_new_key', 'anything')).toBeNull()
    for (const lang of ['browser', 'en', 'de']) expect(settingProblem('default_language', lang)).toBeNull()
    expect(settingProblem('default_language', 'fr')).toEqual({ code: 'choice', options: 'browser, en, de' })
    // orca_version: empty is automatic, else a version as agentbox/settings.py takes it
    for (const ok of ['', ' ', '1.4.250', '0.0.0', ' 1.4.250 ']) expect(settingProblem('orca_version', ok), ok).toBeNull()
    for (const bad of ['1.4', 'v1.4.250', '1.4.250-rc.0', '01.4.250', 'latest', '1.4.250;true']) {
      expect(settingProblem('orca_version', bad), bad).toEqual({ code: 'version' })
    }
  })

  it('sends numbers as numbers, text as text, and `default` for the default', () => {
    const info = (def: string | number, value: string | number = def) => ({ value, default: def, is_default: value === def, applies_later: null, allowed: '' })
    expect(settingValue('reset_age_days', ' 9 ')).toBe(9)
    expect(settingValue('young_domain_days', '21')).toBe(21)
    expect(settingValue('reset_time', '05:30')).toBe('05:30')
    expect(toSend('reset_age_days', '9', info(7))).toBe(9)
    expect(toSend('reset_age_days', '7', info(7, 9))).toBe('default')
    expect(toSend('reset_age_days', '007', info(7, 9))).toBe('default')
    expect(toSend('young_domain_days', '14', info(14, 21))).toBe('default')
    expect(toSend('reset_time', '04:00', info('04:00', '05:30'))).toBe('default')
    expect(toSend('default_image', 'work', info('base'))).toBe('work')
    expect(toSend('default_language', 'browser', info('browser', 'de'))).toBe('default')
    expect(toSend('default_language', 'en', info('browser'))).toBe('en')
    expect(toSend('orca_version', '1.4.250', info(''))).toBe('1.4.250')
    expect(toSend('orca_version', ' ', info('', '1.4.250'))).toBe('default')
    // auto_update: true and false as booleans
    expect(settingValue('auto_update', 'false')).toBe(false)
    expect(toSend('auto_update', 'false', { ...info('x'), value: true, default: true })).toBe(false)
    expect(toSend('auto_update', 'true', { ...info('x'), value: false, default: true })).toBe('default')
    expect(settingChanged('auto_update', 'true', { ...info('x'), value: true, default: true })).toBe(false)
    expect(settingProblem('auto_update', 'maybe')?.code).toBe('choice')
  })

  it('does not count a differently written number as a change', () => {
    const info = { value: 7, default: 7, is_default: true, applies_later: null, allowed: '' }
    expect(settingChanged('reset_age_days', '7', info)).toBe(false)
    expect(settingChanged('reset_age_days', ' 007 ', info)).toBe(false)
    expect(settingChanged('reset_age_days', '8', info)).toBe(true)
    expect(settingChanged('reset_age_days', '', info)).toBe(true)
  })
})

describe('Settings page', () => {
  useFreshApp()

  it('shows all 18 settings with label, current value and default, and when a change applies', async () => {
    const { wrapper } = await openPage('/settings', {
      'GET /settings': ok(settingsBody({ reset_age_days: 14, rebuild_day: 'fri', temporary_entry_default: '8h', young_domain_days: 28, default_language: 'en' })),
    })
    const rows = wrapper.findAll('[data-testid="setting"]')
    expect(rows).toHaveLength(18)
    expect(new Set(rows.map(r => r.attributes('data-key')))).toEqual(new Set(Object.keys(DEFAULTS)))
    expect(wrapper.find('[data-testid="settings-load-error"]').exists()).toBe(false)

    const age = field(wrapper, 'reset_age_days')
    expect(age.text()).toContain('Reset age')
    expect((input(wrapper, 'reset_age_days').element as HTMLInputElement).value).toBe('14')
    expect(age.find('[data-testid="setting-default"]').text()).toBe('Default: 7')
    // a value that is not the default can go back to it; one that is, cannot
    expect(age.find('[data-testid="setting-restore"]').exists()).toBe(true)
    expect(field(wrapper, 'reset_time').find('[data-testid="setting-restore"]').exists()).toBe(false)

    // choices show the translated option, also as the default
    expect((input(wrapper, 'rebuild_day').element as HTMLSelectElement).value).toBe('fri')
    expect(field(wrapper, 'rebuild_day').text()).toContain('Rebuild day')
    expect(field(wrapper, 'rebuild_day').find('[data-testid="setting-default"]').text()).toBe('Default: Sunday')
    expect(field(wrapper, 'temporary_entry_default').find('[data-testid="setting-default"]').text()).toBe('Default: 1 hour')
    expect((input(wrapper, 'young_domain_days').element as HTMLSelectElement).value).toBe('28')
    expect(field(wrapper, 'young_domain_days').find('[data-testid="setting-default"]').text()).toBe('Default: 14 days')
    // the default language: `browser` by default, en and de by their names
    const language = input(wrapper, 'default_language').element as HTMLSelectElement
    expect(language.value).toBe('en')
    expect(field(wrapper, 'default_language').findAll('option').map(o => [o.attributes('value'), o.text()])).toEqual([['browser', 'Browser language'], ['en', 'English'], ['de', 'Deutsch']])
    expect(field(wrapper, 'default_language').find('[data-testid="setting-default"]').text()).toBe('Default: Browser language')
    expect(field(wrapper, 'default_language').find('[data-testid="setting-restore"]').exists()).toBe(true)
    expect(field(wrapper, 'default_memory_mib').find('[data-testid="setting-default"]').text()).toBe('Default: 26624 MiB')
    expect(field(wrapper, 'default_data_disk_gib').find('[data-testid="setting-default"]').text()).toBe('Default: 300 GiB')
    // auto_update: on by default, a choice of On and Off
    expect((input(wrapper, 'auto_update').element as HTMLSelectElement).value).toBe('true')
    expect(field(wrapper, 'auto_update').findAll('option').map(o => [o.attributes('value'), o.text()])).toEqual([['true', 'On'], ['false', 'Off']])
    expect(field(wrapper, 'auto_update').find('[data-testid="setting-default"]').text()).toBe('Default: On')
    expect(field(wrapper, 'auto_update').text()).toContain('Automatic updates')
    // orca_version: empty is automatic, also as the default
    expect((input(wrapper, 'orca_version').element as HTMLInputElement).value).toBe('')
    expect(input(wrapper, 'orca_version').attributes('placeholder')).toBe('automatic')
    expect(field(wrapper, 'orca_version').find('[data-testid="setting-default"]').text()).toBe('Default: automatic')
    expect(field(wrapper, 'orca_version').text()).toContain('Orca version')

    // exactly the settings the backend says apply later show the note, in English
    const later = Object.fromEntries(rows.map(r => [r.attributes('data-key')!, r.find('[data-testid="setting-later"]')]))
    for (const key of Object.keys(DEFAULTS)) expect(later[key]!.exists(), key).toBe(key in LATER)
    expect(later.builds_kept!.text()).toBe('Applies from the next Build.')
    expect(later.default_image!.text()).toBe('Applies to new VMs only.')
    expect(later.session_hours!.text()).toBe('Applies from the next sign-in.')
    const text = wrapper.find('[data-testid="settings-page"]').text()
    for (const german of Object.values(LATER)) expect(text).not.toContain(german)
    expect(text).not.toContain('ganze Zahl')
  })

  it('does not offer the password, the home network or the ports, and says why', async () => {
    const { wrapper } = await openPage('/settings', { 'GET /settings': ok(settingsBody()) })
    const form = wrapper.find('[data-testid="settings-form"]')
    for (const el of form.findAll('input, select, textarea')) {
      expect(`${el.attributes('name') ?? ''} ${el.attributes('id') ?? ''}`).not.toMatch(/password|network|port/i)
      expect(el.attributes('type')).not.toBe('password')
    }
    expect(form.findAll('input[type="password"]')).toHaveLength(0)
    expect(form.find('[data-testid="settings-host-only"]').text()).toContain('password, the home network and the ports can only be changed on the Host')
  })

  it('shows a load error', async () => {
    const { wrapper, api } = await openPage('/settings', {
      'GET /settings': { status: 502, body: { error: { code: 'agentbox_failed', message: 'agentbox failed' } } },
    })
    expect(wrapper.find('[data-testid="settings-load-error"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="setting"]')).toHaveLength(0)
    // only the form asks, and it does not ask again by itself
    expect(api.calls.filter(c => c.path === '/settings')).toHaveLength(1)
  })

  it('shows no Save button until something changed', async () => {
    const { wrapper } = await openPage('/settings', { 'GET /settings': ok(settingsBody()) })
    expect(save(wrapper).exists()).toBe(false)
    await type(wrapper, 'reset_age_days', '9')
    expect(save(wrapper).exists()).toBe(true)
    expect(field(wrapper, 'reset_age_days').attributes('data-changed')).toBe('true')
    await type(wrapper, 'reset_age_days', '7')
    expect(save(wrapper).exists()).toBe(false)
  })

  describe('checks before the request', () => {
    const cases: [string, string, string][] = [
      ['reset_age_days', '0', 'A whole number from 1 to 365.'],
      ['reset_age_days', '366', 'A whole number from 1 to 365.'],
      ['builds_kept', 'abc', 'A whole number from 1 to 20.'],
      ['default_memory_mib', '512', 'A whole number from 1024 to 1048576.'],
      ['session_hours', '1.5', 'A whole number from 1 to 168.'],
      ['reset_age_days', '', 'Enter a value.'],
      ['default_image', 'My Image', 'An Image name: lowercase letters, digits and hyphens, up to 32 characters.'],
    ]
    it.each(cases)('%s = "%s" is not sent and shows its message at the field', async (key, text, message) => {
      const { wrapper, api } = await openPage('/settings', { 'GET /settings': ok(settingsBody()) })
      await type(wrapper, key, text)
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(patches(api.calls)).toHaveLength(0)
      expect(field(wrapper, key).find('[data-testid="setting-error"]').text()).toBe(message)
      expect(input(wrapper, key).attributes('aria-invalid')).toBe('true')
      // only that field is marked
      expect(wrapper.findAll('[data-testid="setting-error"]')).toHaveLength(1)
      expect(wrapper.find('[data-testid="settings-problems"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="settings-saved"]').exists()).toBe(false)
    })

    it('checks a time as HH:MM and a choice against its options', async () => {
      const { wrapper, api } = await openPage('/settings', { 'GET /settings': ok(settingsBody()) })
      await type(wrapper, 'rebuild_time', '25:00')
      const select = input(wrapper, 'rebuild_day')
      ;(select.element as HTMLSelectElement).insertAdjacentHTML('beforeend', '<option value="xyz">xyz</option>')
      await select.setValue('xyz')
      await flushPromises()
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(patches(api.calls)).toHaveLength(0)
      expect(field(wrapper, 'rebuild_day').find('[data-testid="setting-error"]').text()).toContain('One of: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday.')
      expect(field(wrapper, 'rebuild_time').find('[data-testid="setting-error"]').exists()).toBe(true)
    })

    it('clears the message of a field when it is edited again', async () => {
      const { wrapper } = await openPage('/settings', { 'GET /settings': ok(settingsBody()) })
      await type(wrapper, 'reset_age_days', '0')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.findAll('[data-testid="setting-error"]')).toHaveLength(1)
      await type(wrapper, 'reset_age_days', '5')
      expect(wrapper.findAll('[data-testid="setting-error"]')).toHaveLength(0)
    })
  })

  describe('saving', () => {
    it('sends PATCH /settings with X-Agentbox: 1 and only the changed keys, numbers as numbers', async () => {
      let stored = settingsBody()
      const { wrapper, api } = await openPage('/settings', {
        'GET /settings': () => ok(stored),
        'PATCH /settings': () => {
          stored = settingsBody({ reset_age_days: 14, rebuild_day: 'fri', default_image: 'work' })
          return ok({ changed: { reset_age_days: { from: 7, to: 14 }, rebuild_day: { from: 'sun', to: 'fri' }, default_image: { from: 'base', to: 'work' } }, applies_later: { default_image: 'fuer neue VMs' }, regenerated: null })
        },
      })
      const loaded = reads(api.calls)
      await type(wrapper, 'reset_age_days', '14')
      await type(wrapper, 'rebuild_day', 'fri')
      await type(wrapper, 'default_image', 'work')
      expect(save(wrapper).text()).toBe('Save 3 changes')
      await save(wrapper).trigger('click')
      await flushPromises()

      const sent = patches(api.calls)
      expect(sent).toHaveLength(1)
      expect(sent[0]!.path).toBe('/settings')
      expect(sent[0]!.headers['x-agentbox']).toBe('1')
      expect(sent[0]!.body).toEqual({ reset_age_days: 14, rebuild_day: 'fri', default_image: 'work' })

      // the form reloaded and shows what is stored now
      expect(reads(api.calls)).toBe(loaded + 1)
      expect((input(wrapper, 'reset_age_days').element as HTMLInputElement).value).toBe('14')
      expect((input(wrapper, 'rebuild_day').element as HTMLSelectElement).value).toBe('fri')
      expect(save(wrapper).exists()).toBe(false)
      const note = wrapper.find('[data-testid="settings-saved"]')
      expect(note.text()).toContain('Saved 3 settings.')
      // only a setting that applies later says so
      const items = note.findAll('li').map(li => li.text())
      expect(items).toEqual(['Reset age', 'Rebuild day', 'Image of new VMs — applies later'])
    })

    it('says "1 setting" and "Save 1 change" in the singular', async () => {
      const { wrapper } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': ok({ changed: { reset_age_days: { from: 7, to: 9 } }, applies_later: {}, regenerated: null }),
      })
      await type(wrapper, 'reset_age_days', '9')
      expect(save(wrapper).text()).toBe('Save 1 change')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-saved"]').text()).toContain('Saved 1 setting.')
      expect(wrapper.find('[data-testid="settings-saved"]').text()).not.toContain('settings.')
    })

    it('sends "default" for a value equal to the default', async () => {
      const { wrapper, api } = await openPage('/settings', {
        'GET /settings': ok(settingsBody({ reset_age_days: 14, young_domain_days: 28, reset_time: '05:30', default_image: 'work' })),
        'PATCH /settings': ok(changedBody),
      })
      await type(wrapper, 'reset_age_days', '7')
      await type(wrapper, 'young_domain_days', '14')
      await type(wrapper, 'reset_time', '04:00')
      await type(wrapper, 'default_image', 'base')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(patches(api.calls)[0]!.body).toEqual({ reset_age_days: 'default', young_domain_days: 'default', reset_time: 'default', default_image: 'default' })
    })

    it('sends a choice with numbers as a number', async () => {
      const { wrapper, api } = await openPage('/settings', { 'GET /settings': ok(settingsBody()), 'PATCH /settings': ok(changedBody) })
      await type(wrapper, 'young_domain_days', '21')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(patches(api.calls)[0]!.body).toEqual({ young_domain_days: 21 })
    })

    it('switches automatic updates off with a boolean', async () => {
      const { wrapper, api } = await openPage('/settings', { 'GET /settings': ok(settingsBody()), 'PATCH /settings': ok(changedBody) })
      await type(wrapper, 'auto_update', 'false')
      expect(field(wrapper, 'auto_update').attributes('data-changed')).toBe('true')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(patches(api.calls)[0]!.body).toEqual({ auto_update: false })
    })

    describe('the default language', () => {
      /** The Settings page where saving default_language changes it from `from` to `to`. */
      async function savingLanguage(from: string, to: string) {
        let stored = settingsBody({ default_language: from })
        const page = await openPage('/settings', {
          'GET /settings': () => ok(stored),
          'PATCH /settings': () => {
            stored = settingsBody({ default_language: to })
            return ok({ changed: { default_language: { from, to } }, applies_later: { default_language: LATER.default_language }, regenerated: null })
          },
        })
        await type(page.wrapper, 'default_language', to)
        await save(page.wrapper).trigger('click')
        await flushPromises()
        return page
      }

      const notes = (w: Page) => w.find('[data-testid="settings-saved"]').findAll('li').map(li => li.text())

      it('applies at once in this browser when no language was chosen here', async () => {
        const { api, wrapper } = await savingLanguage('browser', 'de')
        expect(patches(api.calls)[0]!.body).toEqual({ default_language: 'de' })
        expect(useNuxtApp().$i18n.locale.value).toBe('de')
        expect(localStorage.getItem(LANGUAGE_KEY)).toBeNull()      // still a default, not a choice
        expect(notes(wrapper)).toEqual(['Standardsprache'])          // not "gilt später": it did here
        expect(useSession().state.defaultLanguage).toBe('de')        // Automatic and the next page use it
      })

      it('goes back to the browser\'s language when it is `browser` again', async () => {
        await useNuxtApp().$i18n.setLocale('de')     // as default_language `de` showed it
        const { api } = await savingLanguage('de', 'browser')
        expect(patches(api.calls)[0]!.body).toEqual({ default_language: 'default' })
        expect(useNuxtApp().$i18n.locale.value).toBe('en')        // happy-dom's browser: en-US
        expect(useSession().state.defaultLanguage).toBe('browser')
      })

      it('leaves a language chosen in this browser', async () => {
        localStorage.setItem(LANGUAGE_KEY, 'en')
        const { wrapper } = await savingLanguage('browser', 'de')
        expect(useNuxtApp().$i18n.locale.value).toBe('en')
        expect(localStorage.getItem(LANGUAGE_KEY)).toBe('en')
        expect(notes(wrapper)).toEqual(['Default language — applies later'])
      })

      it('changes nothing when saving fails', async () => {
        const { wrapper } = await openPage('/settings', {
          'GET /settings': ok(settingsBody()),
          'PATCH /settings': { status: 502, body: { error: { code: 'agentbox_failed', message: 'x' } } },
        })
        await type(wrapper, 'default_language', 'de')
        await save(wrapper).trigger('click')
        await flushPromises()
        expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(true)
        expect(useNuxtApp().$i18n.locale.value).toBe('en')
      })
    })

    it('says nothing changed when the backend changed nothing', async () => {
      const { wrapper } = await openPage('/settings', { 'GET /settings': ok(settingsBody()), 'PATCH /settings': ok(changedBody) })
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-saved"]').text()).toBe('Nothing changed.')
    })

    it('does not send a second request while one is running', async () => {
      let release: (v: unknown) => void = () => {}
      const gate = new Promise(r => (release = r))
      const { wrapper, api } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': ok(changedBody),
      })
      const original = globalThis.fetch
      vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
        if (init?.method === 'PATCH') await gate
        return original(input, init)
      }))
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await save(wrapper).trigger('click')
      release(null)
      await flushPromises()
      expect(patches(api.calls)).toHaveLength(1)
    })
  })

  describe('problems from the backend', () => {
    it('shows invalid_setting with a field next to that field, translated, and marks nothing as saved', async () => {
      const { wrapper, api } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': { status: 400, body: { error: { code: 'invalid_setting', message: 'Invalid setting', field: 'reset_time', detail: 'reset_time: ungueltiger Wert 04:61 (erlaubt: Uhrzeit HH:MM)' } } },
      })
      const loaded = reads(api.calls)
      await type(wrapper, 'reset_time', '05:30')
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(patches(api.calls)).toHaveLength(1)
      const err = field(wrapper, 'reset_time').find('[data-testid="setting-error"]')
      expect(err.text()).toContain('A setting has a value that is not allowed.')
      expect(err.text()).toContain('A time as HH:MM')
      expect(wrapper.findAll('[data-testid="setting-error"]')).toHaveLength(1)
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="settings-saved"]').exists()).toBe(false)
      expect(wrapper.text()).not.toContain('ungueltiger Wert')
      // the edits stay, nothing was reloaded
      expect((input(wrapper, 'reset_age_days').element as HTMLInputElement).value).toBe('9')
      expect(reads(api.calls)).toBe(loaded)
      expect(save(wrapper).exists()).toBe(true)
    })

    it('shows invalid_setting without a field as a general error', async () => {
      const { wrapper } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': { status: 400, body: { error: { code: 'invalid_setting', message: 'Invalid setting' } } },
      })
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-save-error"]').text()).toContain('A setting has a value that is not allowed.')
      expect(wrapper.findAll('[data-testid="setting-error"]')).toHaveLength(0)
      expect(wrapper.find('[data-testid="settings-saved"]').exists()).toBe(false)
    })

    it.each([
      [502, 'agentbox_failed'],
      [400, 'unknown_setting'],
      [503, 'network'],
    ])('shows %s %s as a general error', async (status, code) => {
      const { wrapper } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': { status, body: { error: { code, message: 'x' } } },
      })
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="settings-saved"]').exists()).toBe(false)
      expect(wrapper.findAll('[data-testid="setting-error"]')).toHaveLength(0)
      expect((input(wrapper, 'reset_age_days').element as HTMLInputElement).value).toBe('9')
    })

    it('shows an error for a field this form does not have as a general error', async () => {
      const { wrapper } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': { status: 400, body: { error: { code: 'invalid_setting', message: 'x', field: 'something_else' } } },
      })
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(true)
    })

    it('clears an earlier error when saved again successfully', async () => {
      let fail = true
      const { wrapper } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': () => fail
          ? { status: 502, body: { error: { code: 'agentbox_failed', message: 'x' } } }
          : ok({ changed: { reset_age_days: { from: 7, to: 9 } }, applies_later: {}, regenerated: null }),
      })
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(true)
      fail = false
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="settings-saved"]').exists()).toBe(true)
    })
  })

  describe('use default and discard', () => {
    it('"Use default" puts the default into the field', async () => {
      const { wrapper } = await openPage('/settings', {
        'GET /settings': ok(settingsBody({ reset_age_days: 14, rebuild_day: 'fri', young_domain_days: 28 })),
      })
      await field(wrapper, 'reset_age_days').find('[data-testid="setting-restore"]').trigger('click')
      await flushPromises()
      expect((input(wrapper, 'reset_age_days').element as HTMLInputElement).value).toBe('7')
      expect(field(wrapper, 'reset_age_days').find('[data-testid="setting-restore"]').exists()).toBe(false)
      await field(wrapper, 'rebuild_day').find('[data-testid="setting-restore"]').trigger('click')
      await field(wrapper, 'young_domain_days').find('[data-testid="setting-restore"]').trigger('click')
      await flushPromises()
      expect((input(wrapper, 'rebuild_day').element as HTMLSelectElement).value).toBe('sun')
      expect((input(wrapper, 'young_domain_days').element as HTMLSelectElement).value).toBe('14')
      expect(save(wrapper).text()).toBe('Save 3 changes')
    })

    it('"Discard changes" restores the loaded values and sends nothing', async () => {
      const { wrapper, api } = await openPage('/settings', { 'GET /settings': ok(settingsBody({ reset_age_days: 14 })) })
      await type(wrapper, 'reset_age_days', '0')
      await type(wrapper, 'reset_time', '06:00')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-problems"]').exists()).toBe(true)
      await wrapper.find('[data-testid="settings-discard"]').trigger('click')
      await flushPromises()
      expect((input(wrapper, 'reset_age_days').element as HTMLInputElement).value).toBe('14')
      expect((input(wrapper, 'reset_time').element as HTMLInputElement).value).toBe('04:00')
      expect(wrapper.find('[data-testid="settings-actions"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="settings-problems"]').exists()).toBe(false)
      expect(wrapper.findAll('[data-testid="setting-error"]')).toHaveLength(0)
      expect(patches(api.calls)).toHaveLength(0)
    })

    it('"Discard changes" also removes a general save error', async () => {
      const { wrapper } = await openPage('/settings', {
        'GET /settings': ok(settingsBody()),
        'PATCH /settings': { status: 502, body: { error: { code: 'agentbox_failed', message: 'x' } } },
      })
      await type(wrapper, 'reset_age_days', '9')
      await save(wrapper).trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(true)
      await wrapper.find('[data-testid="settings-discard"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(false)
    })
  })

  it('shows a key it does not know with the API text, at the end', async () => {
    const body = settingsBody()
    ;(body.settings as Record<string, unknown>).future_key = { value: 5, default: 5, is_default: true, applies_later: 'beim naechsten Mal', allowed: 'ganze Zahl' }
    const { wrapper, api } = await openPage('/settings', {
      'GET /settings': ok(body),
      'PATCH /settings': ok(changedBody),
    })
    const rows = wrapper.findAll('[data-testid="setting"]')
    expect(rows).toHaveLength(19)
    expect(rows.at(-1)!.attributes('data-key')).toBe('future_key')
    expect(rows.at(-1)!.text()).toContain('future_key')
    expect(rows.at(-1)!.find('[data-testid="setting-later"]').text()).toBe('beim naechsten Mal')
    await type(wrapper, 'future_key', '6')
    await save(wrapper).trigger('click')
    await flushPromises()
    expect(patches(api.calls)[0]!.body).toEqual({ future_key: '6' })
  })

  it('renders in German', async () => {
    const { wrapper, api } = await openPage('/settings', {
      'GET /settings': ok(settingsBody({ reset_age_days: 14, rebuild_day: 'fri' })),
      'PATCH /settings': { status: 400, body: { error: { code: 'invalid_setting', message: 'x', field: 'reset_time' } } },
    })
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    const age = field(wrapper, 'reset_age_days')
    expect(age.text()).toContain('Alter für den Reset')
    expect(age.find('[data-testid="setting-default"]').text()).toBe('Standard: 7')
    expect(age.find('[data-testid="setting-restore"]').text()).toBe('Standard verwenden')
    expect(field(wrapper, 'rebuild_day').find('[data-testid="setting-default"]').text()).toBe('Standard: Sonntag')
    expect(field(wrapper, 'default_language').find('[data-testid="setting-default"]').text()).toBe('Standard: Sprache des Browsers')
    expect(field(wrapper, 'default_language').find('option[value="browser"]').text()).toBe('Sprache des Browsers')
    expect(field(wrapper, 'builds_kept').find('[data-testid="setting-later"]').text()).toMatch(/^Gilt ab dem nächsten Build/)
    expect(wrapper.find('[data-testid="settings-host-only"]').text()).toContain('Das Passwort des Control Room')
    expect(wrapper.text()).not.toContain('naechsten')

    await type(wrapper, 'reset_age_days', '0')
    expect(save(wrapper).text()).toBe('1 Änderung speichern')
    await type(wrapper, 'rebuild_day', 'mon')
    expect(save(wrapper).text()).toBe('2 Änderungen speichern')
    await save(wrapper).trigger('click')
    await flushPromises()
    expect(field(wrapper, 'reset_age_days').find('[data-testid="setting-error"]').text()).toBe('Eine ganze Zahl von 1 bis 365.')
    await type(wrapper, 'reset_age_days', '15')
    await type(wrapper, 'reset_time', '05:00')
    await save(wrapper).trigger('click')
    await flushPromises()
    expect(patches(api.calls)).toHaveLength(1)
    expect(field(wrapper, 'reset_time').find('[data-testid="setting-error"]').text()).toContain('Eine Einstellung hat einen unzulässigen Wert.')
    expect(wrapper.text()).not.toContain('A setting has a value')
  })

  it('shows the German saved note with plural forms', async () => {
    const { wrapper } = await openPage('/settings', {
      'GET /settings': ok(settingsBody()),
      'PATCH /settings': ok({ changed: { reset_age_days: { from: 7, to: 9 }, builds_kept: { from: 3, to: 4 } }, applies_later: { builds_kept: 'ab dem naechsten Build' }, regenerated: null }),
    })
    await useNuxtApp().$i18n.setLocale('de')
    await type(wrapper, 'reset_age_days', '9')
    await save(wrapper).trigger('click')
    await flushPromises()
    const note = wrapper.find('[data-testid="settings-saved"]').text()
    expect(note).toContain('2 Einstellungen gespeichert.')
    expect(note).toContain('gilt später')
  })
})

describe('Notifications preferences', () => {
  useFreshApp()

  const prefs = (w: Page) => w.find('[data-testid="notification-prefs"]')
  const switches = (w: Page) => w.findAll('[data-testid="notify-kind"]')
  const kinds = ['alert', 'github-refused', 'build-failed', 'build-rejected', 'host-reboot', 'temperature']
  const toggle = (w: Page, kind: string) => w.find(`[data-testid="notify-kind"][data-kind="${kind}"] [role="switch"]`)
  const checked = (w: Page, kind: string) => toggle(w, kind).attributes('aria-checked')

  function stubNotification(permission: NotificationPermission | undefined, answer: NotificationPermission = 'granted') {
    if (permission === undefined) {
      vi.stubGlobal('Notification', undefined)
      return null
    }
    const request = vi.fn(async () => answer)
    vi.stubGlobal('Notification', Object.assign(function Notification() {}, { permission, requestPermission: request }))
    return request
  }

  const open = () => {
    resetNotificationsForTests()
    return openPage('/settings', { 'GET /settings': ok(settingsBody()) })
  }

  it('asks for permission with a button when the permission is still open', async () => {
    const request = stubNotification('default', 'granted')
    const { wrapper } = await open()
    expect(wrapper.find('[data-testid="notify-ask"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="notify-granted"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="notify-ask"]').text()).toContain('Allow notifications')
    expect(request).not.toHaveBeenCalled()
    await wrapper.find('[data-testid="notify-ask"] button').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="notify-ask"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="notify-granted"]').text()).toContain('This browser may show notifications.')
  })

  it('shows the blocked state when the user refuses', async () => {
    const request = stubNotification('default', 'denied')
    const { wrapper } = await open()
    await wrapper.find('[data-testid="notify-ask"] button').trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="notify-denied"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="notify-ask"]').exists()).toBe(false)
  })

  it('shows that notifications are allowed', async () => {
    stubNotification('granted')
    const { wrapper } = await open()
    expect(wrapper.find('[data-testid="notify-granted"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="notify-ask"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="notify-denied"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="notify-unsupported"]').exists()).toBe(false)
  })

  it('shows that notifications are blocked, without a button', async () => {
    stubNotification('denied')
    const { wrapper } = await open()
    expect(wrapper.find('[data-testid="notify-denied"]').text()).toContain('Notifications are blocked for this site.')
    expect(wrapper.find('[data-testid="notify-ask"]').exists()).toBe(false)
    expect(prefs(wrapper).text()).not.toContain('Allow notifications')
  })

  it('says so when the browser has no notifications', async () => {
    stubNotification(undefined)
    const { wrapper } = await open()
    expect(wrapper.find('[data-testid="notify-unsupported"]').text()).toContain('cannot show notifications')
    expect(wrapper.find('[data-testid="notify-ask"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="notify-granted"]').exists()).toBe(false)
  })

  it('follows a permission changed in the browser while the page is open', async () => {
    stubNotification('denied')
    const { wrapper } = await open()
    expect(wrapper.find('[data-testid="notify-denied"]').exists()).toBe(true)
    ;(Notification as unknown as { permission: string }).permission = 'granted'
    window.dispatchEvent(new Event('focus'))
    await flushPromises()
    expect(wrapper.find('[data-testid="notify-denied"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="notify-granted"]').exists()).toBe(true)
  })

  it('shows a switch per kind, all on at first', async () => {
    stubNotification('granted')
    const { wrapper } = await open()
    expect(switches(wrapper).map(s => s.attributes('data-kind'))).toEqual(kinds)
    for (const kind of kinds) expect(checked(wrapper, kind), kind).toBe('true')
    expect(prefs(wrapper).text()).toContain('Failed Builds')
  })

  it('stores each switch in this browser and restores it on the next visit', async () => {
    stubNotification('granted')
    const first = await open()
    await toggle(first.wrapper, 'build-failed').trigger('click')
    await toggle(first.wrapper, 'temperature').trigger('click')
    await flushPromises()
    expect(checked(first.wrapper, 'build-failed')).toBe('false')
    expect(checked(first.wrapper, 'temperature')).toBe('false')
    expect(checked(first.wrapper, 'alert')).toBe('true')
    expect(JSON.parse(localStorage.getItem('agentbox-notifications')!)).toEqual({
      alert: true, 'github-refused': true, 'build-failed': false, 'build-rejected': true, 'host-reboot': true, temperature: false,
    })
    first.wrapper.unmount()

    // a later mount reads it again
    const second = await open()
    expect(checked(second.wrapper, 'build-failed')).toBe('false')
    expect(checked(second.wrapper, 'temperature')).toBe('false')
    expect(checked(second.wrapper, 'alert')).toBe('true')
    // and a switch can be turned on again
    await toggle(second.wrapper, 'build-failed').trigger('click')
    await flushPromises()
    expect(JSON.parse(localStorage.getItem('agentbox-notifications')!)['build-failed']).toBe(true)
  })

  it('keeps the switches when the browser has no notifications, and ignores unreadable storage', async () => {
    stubNotification(undefined)
    localStorage.setItem('agentbox-notifications', '{nonsense')
    const { wrapper } = await open()
    for (const kind of kinds) expect(checked(wrapper, kind), kind).toBe('true')
    await toggle(wrapper, 'alert').trigger('click')
    await flushPromises()
    expect(checked(wrapper, 'alert')).toBe('false')
  })

  it('shows the section in German', async () => {
    stubNotification('default')
    const { wrapper } = await open()
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    expect(prefs(wrapper).text()).toContain('Benachrichtigungen in diesem Browser')
    expect(prefs(wrapper).text()).toContain('Benachrichtigungen erlauben')
  })
})
