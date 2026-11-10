// The VM views' rules match the CLI's (agentbox/registry.py, vms.py, guest.py).
import { describe, expect, it } from 'vitest'
import {
  defaultLogFilters, envFromRows, hidePairingCodes, lifecycleActions, logFilterProblems, logQuery, memoryInUse,
  nameProblem, parseOwners, pinnableBuilds, wholeNumber,
} from '~/utils/vmRules'
import { notResponding } from '~/utils/vm'
import type { Vm } from '~/utils/live'
import { vm as fakeVm } from './fakes'

const vm = (name: string, state: string, fields: Record<string, unknown> = {}) => fakeVm(name, state, fields) as Vm

describe('VM names', () => {
  it('follows the registry: 2 to 16 of a-z, 0-9, -, starting with a letter', () => {
    expect(nameProblem('lab', [])).toBeNull()
    expect(nameProblem('a1-b2', [])).toBeNull()
    expect(nameProblem('a234567890123456', [])).toBeNull()
    for (const bad of ['a', 'A1', '1ab', '-ab', 'ab_c', 'a2345678901234567', 'äb', 'ab c']) {
      expect(nameProblem(bad, []), bad).toBe('invalid')
    }
    expect(nameProblem('', [])).toBe('required')
  })

  it('refuses reserved words and names taken already', () => {
    for (const word of ['off', 'all', 'new', 'vms', 'github-app', 'v1', 'v23']) expect(nameProblem(word, [])).toBe('reserved')
    expect(nameProblem('va', [])).toBeNull()
    expect(nameProblem('work', ['work', 'priv'])).toBe('exists')
  })
})

describe('VM variables', () => {
  it('takes the rows as names and values, as agentbox/vmenv.py checks them', () => {
    const { env, problems, tooMany } = envFromRows([
      { name: 'DATABASE_URL', value: 'postgres://db/x' }, { name: ' NODE_ENV ', value: '' }, { name: '', value: '' },
      { name: '_a1', value: 'Grüße "x" $HOME' }, { name: 'constructor', value: '1' }, { name: '__proto__', value: '2' },
    ])
    expect(Object.entries(env)).toEqual([['DATABASE_URL', 'postgres://db/x'], ['NODE_ENV', ''], ['_a1', 'Grüße "x" $HOME'],
      ['constructor', '1'], ['__proto__', '2']])
    expect(problems).toEqual({})
    expect(tooMany).toBe(false)
  })

  it('says what is wrong per row: the name, a name twice, a control character, a value over 4 KiB', () => {
    const { env, problems } = envFromRows([
      { name: '1A', value: 'x' }, { name: 'A-B', value: 'x' }, { name: 'A'.repeat(65), value: 'x' }, { name: '', value: 'x' },
      { name: 'A', value: '1' }, { name: 'A', value: '2' }, { name: 'B', value: 'a\nb' }, { name: 'C', value: 'tab\tok' },
      { name: 'D', value: 'ü'.repeat(2049) }, { name: 'E', value: 'x'.repeat(4096) },
    ])
    expect(problems).toEqual({ 0: 'name', 1: 'name', 2: 'name', 3: 'name', 5: 'twice', 6: 'value', 8: 'long' })
    expect(Object.keys(env)).toEqual(['A', 'C', 'E'])
  })

  it('takes at most 64 variables', () => {
    const rows = Array.from({ length: 65 }, (_, i) => ({ name: `V${i}`, value: 'x' }))
    expect(envFromRows(rows.slice(0, 64)).tooMany).toBe(false)
    expect(envFromRows(rows).tooMany).toBe(true)
  })
})

describe('create fields', () => {
  it('reads GitHub owners from free text', () => {
    expect(parseOwners('likt0r, acme-org  likt0r\nother')).toEqual({ owners: ['likt0r', 'acme-org', 'other'], invalid: [] })
    expect(parseOwners('ok bad/name')).toEqual({ owners: ['ok'], invalid: ['bad/name'] })
    expect(parseOwners('')).toEqual({ owners: [], invalid: [] })
  })

  it('takes whole numbers in range, empty for the default', () => {
    expect(wholeNumber('', 1, 10)).toBeNull()
    expect(wholeNumber(' 5 ', 1, 10)).toBe(5)
    expect(wholeNumber(10, 1, 10)).toBe(10)
    expect(wholeNumber('11', 1, 10)).toBe('invalid')
    expect(wholeNumber('1.5', 1, 10)).toBe('invalid')
    expect(wholeNumber('-1', 1, 10)).toBe('invalid')
  })
})

describe('log filters', () => {
  it('accepts what agentbox logs accepts', () => {
    expect(logFilterProblems(defaultLogFilters())).toEqual([])
    expect(logFilterProblems({ ...defaultLogFilters(), unit: 'agentbox-orca.service', priority: 'err', since: '-2h',
      until: '2026-11-03 12:00', grep: '-x', lines: '5000' })).toEqual([])
    for (const t of ['today', 'yesterday', 'now', '-30min', '-7d', '-1w', '2026-11-03', '2026-11-03T12:00:30']) {
      expect(logFilterProblems({ ...defaultLogFilters(), since: t }), t).toEqual([])
    }
  })

  it('refuses what agentbox would refuse', () => {
    expect(logFilterProblems({ ...defaultLogFilters(), unit: 'two words' })).toEqual(['unit'])
    expect(logFilterProblems({ ...defaultLogFilters(), priority: '9' })).toEqual(['priority'])
    expect(logFilterProblems({ ...defaultLogFilters(), since: 'last week', until: '3pm' })).toEqual(['since', 'until'])
    expect(logFilterProblems({ ...defaultLogFilters(), grep: 'x'.repeat(201) })).toEqual(['grep'])
    expect(logFilterProblems({ ...defaultLogFilters(), grep: 'a\u001bb' })).toEqual(['grep'])
    for (const lines of ['0', '5001', '', 'ten']) expect(logFilterProblems({ ...defaultLogFilters(), lines }), lines).toEqual(['lines'])
  })

  it('builds the query, leaving empty filters out and the search text as typed', () => {
    expect(logQuery(defaultLogFilters())).toBe('boot=current&lines=200')
    const q = new URLSearchParams(logQuery({ ...defaultLogFilters(), unit: ' orca ', priority: 'warning', since: '-1h',
      boot: 'all', grep: ' -x y ', lines: 50 }, 'c=1;b=2'))
    expect(Object.fromEntries(q)).toEqual({ unit: 'orca', priority: 'warning', since: '-1h', boot: 'all', grep: ' -x y ',
      lines: '50', after_cursor: 'c=1;b=2' })
  })

  it('hides anything that looks like a Pairing code', () => {
    const p = '[hidden]'
    expect(hidePairingCodes('pair with orca://pair?code=abc_DEF-123 now', p)).toBe(`pair with ${p} now`)
    // agentbox's own marker, and its older German one
    expect(hidePairingCodes('orca://pair?[hidden]', p)).toBe(p)
    expect(hidePairingCodes('orca://pair?[verborgen]', p)).toBe(p)
    expect(hidePairingCodes('ORCA%3A%2F%2Fpair%3Fcode%3Dx', p)).toBe(p)
    expect(hidePairingCodes('orca started', p)).toBe('orca started')
  })
})

describe('VM state', () => {
  it('offers the actions that fit the state', () => {
    expect(lifecycleActions({ state: 'running' })).toMatchObject({ start: false, pause: true, restart: true, reset: true })
    expect(lifecycleActions({ state: 'paused' })).toMatchObject({ start: true, wake: true, pause: false })
    expect(lifecycleActions({ state: 'stopped' })).toMatchObject({ start: true, wake: false, pause: false, restart: false })
    expect(lifecycleActions({ state: 'shutting_down' })).toMatchObject({ start: false, pause: false, reset: false })
  })

  it('offers Restart and Reset, not Start, while a VM starts or does not respond', () => {
    for (const state of ['starting', 'not_responding']) {
      expect(lifecycleActions({ state }), state).toEqual({ start: false, wake: false, pause: false, restart: true, reset: true })
    }
  })

  it('says what does not answer only when the VM does not respond', () => {
    const since = '2026-11-04T18:15:02Z'
    const last = '2026-11-05T08:14:00Z'
    expect(notResponding(vm('priv', 'not_responding', {
      responding: { guest_agent: false, orca: false, since }, failed_wakes: { count: 12, last, address: '192.0.2.36' },
    }))).toEqual({ what: 'system', since, wakes: { count: 12, last } })
    expect(notResponding(vm('priv', 'not_responding', {
      responding: { guest_agent: true, orca: false, since }, failed_wakes: { count: 0, last: null, address: null },
    }))).toEqual({ what: 'orca', since, wakes: null })
    expect(notResponding(vm('priv', 'not_responding', { responding: null }))).toEqual({ what: 'unknown', since: null, wakes: null })
    expect(notResponding(vm('priv', 'starting', { responding: { guest_agent: false, orca: false, since } }))).toBeNull()
    expect(notResponding(vm('priv', 'running'))).toBeNull()
  })

  it('reads memory in use from the host readings', () => {
    expect(memoryInUse({ vms: { work: { memory_bytes: 1024 } } }, 'work')).toBe(1024)
    expect(memoryInUse({ vms: {} }, 'work')).toBeNull()
    expect(memoryInUse(null, 'work')).toBeNull()
  })

  it('pins only to finished Builds with a disk', () => {
    const image = { name: 'base', current: 'b3', builds: [
      { build: 'b4', status: 'running', file: null }, { build: 'b3', status: 'ok', file: 'x' },
      { build: 'b2', status: 'failed', file: null }, { build: 'b1', status: 'migrated', file: 'y' },
      { build: 'b0', status: 'ok', file: null }] }
    expect(pinnableBuilds(image).map(b => b.build)).toEqual(['b3', 'b1'])
  })
})
