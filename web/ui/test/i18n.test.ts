// Runs from web/ui (npm test). Both languages have every string, and every error code of the API has a translation.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import en from '../i18n/locales/en'
import de from '../i18n/locales/de'
import appConfig from '../app/app.config'

function keys(obj: object, prefix = ''): string[] {
  return Object.entries(obj).flatMap(([k, v]) => (typeof v === 'object' && v !== null ? keys(v, `${prefix}${k}.`) : [`${prefix}${k}`]))
}

describe('translations', () => {
  it('has the same keys in English and German', () => {
    expect(keys(de).sort()).toEqual(keys(en).sort())
  })

  it('translates every error code in docs/control-room-api.md', () => {
    const doc = readFileSync(resolve(process.cwd(), '../../docs/control-room-api.md'), 'utf8')
    const section = doc.slice(doc.indexOf('### Errors'), doc.indexOf('## Session'))
    const codes = new Set([...section.matchAll(/^\| \d{3} \| (.+?) \|/gm)].flatMap(m => [...m[1]!.matchAll(/`([a-z_]+)`/g)].map(c => c[1]!)))
    expect(codes.size).toBeGreaterThan(60)
    const missing = [...codes].filter(c => !(c in en.errors))
    expect(missing).toEqual([])
  })
})

describe('colors', () => {
  it('main.css carries the colors of app.config.ts (the runtime colors plugin is off)', () => {
    const css = readFileSync(resolve(process.cwd(), 'app/assets/css/colors.css'), 'utf8')
    const colors = (appConfig as { ui: { colors: Record<string, string> } }).ui.colors
    for (const [key, value] of Object.entries(colors)) {
      expect(css).toContain(`--ui-color-${key}-500: var(--color-${value}-500,`)
    }
  })
})
