// The Log tab: the VM's journal through GET /vms/<vm>/logs with the filters of
// `agentbox logs`, invalid filters refused before anything is sent, following new
// entries via the cursor, Pairing codes never shown.
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeApi, SIGNED_IN, vm, type Call } from './fakes'
import { byTestId, exists, openApp, setUp, submit, tearDown, text, type } from './vm-helpers'

beforeEach(setUp)
afterEach(() => {
  vi.useRealTimers()
  tearDown()
})

const WORK = vm('work')

function entry(n: number, message: string, priority = 6) {
  return { time: `2026-11-03T12:00:0${n}Z`, unit: 'agentbox-orca.service', identifier: 'orca', priority, message, boot: 'b1', cursor: `s=${n}` }
}

function logCalls(calls: Call[]) {
  return calls.filter(c => c.path.startsWith('/vms/work/logs')).map(c => Object.fromEntries(new URLSearchParams(c.path.split('?')[1])))
}

/** A fake logs endpoint: answers by the query (a function gets the parsed query). */
function logs(answer: (q: Record<string, string>) => { status: number, body?: unknown }) {
  return (call: Call) => answer(Object.fromEntries(new URLSearchParams(call.path.split('?')[1])))
}

function routes(answer: (q: Record<string, string>) => { status: number, body?: unknown }) {
  // fakeApi matches the whole path; the logs route takes every query
  const route = logs(answer)
  return new Proxy({ 'GET /session': SIGNED_IN } as Record<string, unknown>, {
    get(target, key: string) {
      if (typeof key === 'string' && key.startsWith('GET /vms/work/logs')) return route
      return target[key]
    },
  }) as Parameters<typeof fakeApi>[0]
}

describe('Log tab', () => {
  it('shows the newest entries of the current boot, with Pairing codes hidden', async () => {
    const api = fakeApi(routes(() => ({ status: 200, body: { vm: 'work', entries: [
      entry(1, 'Orca started'),
      entry(2, 'pair with orca://pair?[hidden]', 5),
      entry(3, 'disk failing', 3),
    ], cursor: 's=3', truncated: false } })))
    await openApp('/vms/work/log', [WORK])
    expect(logCalls(api.calls)).toEqual([{ boot: 'current', lines: '200' }])
    const messages = byTestId('log-entries').findAll('[data-testid="log-message"]').map(m => m.text())
    expect(messages).toEqual(['Orca started', 'pair with [Pairing code hidden]', 'disk failing'])
    expect(text('log-count')).toBe('3 entries')
    expect(byTestId('log-entries').text()).toContain('err')
    expect(byTestId('log-view').text()).toContain('Pairing codes are hidden')
    expect(document.body.innerHTML).not.toContain('orca://pair')
  })

  it('sends each filter as agentbox logs expects it', async () => {
    const api = fakeApi(routes(() => ({ status: 200, body: { vm: 'work', entries: [], cursor: null, truncated: false } })))
    await openApp('/vms/work/log', [WORK])
    await type('log-unit', 'agentbox-orca.service')
    await type('log-since', '-2h')
    await type('log-until', '2026-11-03 12:00')
    await type('log-grep', '-x error')
    await type('log-lines', '50')
    await submit('log-filters')
    expect(logCalls(api.calls).at(-1)).toEqual({ unit: 'agentbox-orca.service', since: '-2h', until: '2026-11-03 12:00',
      boot: 'current', grep: '-x error', lines: '50' })
    expect(exists('log-empty')).toBe(true)
  })

  it('refuses invalid filters without asking the VM', async () => {
    const api = fakeApi(routes(() => ({ status: 200, body: { vm: 'work', entries: [], cursor: null, truncated: false } })))
    await openApp('/vms/work/log', [WORK])
    const before = logCalls(api.calls).length
    await type('log-unit', 'two words')
    await type('log-since', 'last week')
    await type('log-lines', '9000')
    await submit('log-filters')
    expect(logCalls(api.calls).length).toBe(before)
    const form = byTestId('log-filters').text()
    expect(form).toContain('Letters, digits and :_.@- only.')
    expect(form).toContain('Not a time agentbox understands.')
    expect(form).toContain('A whole number from 1 to 5000.')
  })

  it('says when the output was cut off and when the VM is not running', async () => {
    let running = true
    fakeApi(routes(() => (running
      ? { status: 200, body: { vm: 'work', entries: [entry(1, 'x')], cursor: 's=1', truncated: true } }
      : { status: 409, body: { error: { code: 'vm_not_running', message: 'The VM is not running', detail: 'VM laeuft nicht' } } })))
    await openApp('/vms/work/log', [WORK])
    expect(text('log-truncated')).toContain('Narrow the filters')
    running = false
    await submit('log-filters')
    expect(text('log-error')).toBe('The VM is not running.')
  })

  it('follows new entries after the last cursor', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const api = fakeApi(routes(q => ({ status: 200, body: q.after_cursor === 's=2'
      ? { vm: 'work', entries: [entry(3, 'third'), entry(4, 'pairing orca://pair?code=abcDEF123')], cursor: 's=4', truncated: false }
      : q.after_cursor === 's=4'
        ? { vm: 'work', entries: [], cursor: null, truncated: false }
        : { vm: 'work', entries: [entry(1, 'first'), entry(2, 'second')], cursor: 's=2', truncated: false } })))
    await openApp('/vms/work/log', [WORK])
    await byTestId('log-follow').trigger('click')
    await flushPromises()
    expect(exists('log-following')).toBe(true)
    expect(logCalls(api.calls).at(-1)).toEqual({ boot: 'current', lines: '200', after_cursor: 's=2' })
    expect(byTestId('log-entries').findAll('[data-testid="log-message"]').map(m => m.text()))
      .toEqual(['first', 'second', 'third', 'pairing [Pairing code hidden]'])
    vi.advanceTimersByTime(3000)
    await flushPromises()
    expect(logCalls(api.calls).at(-1)).toEqual({ boot: 'current', lines: '200', after_cursor: 's=4' })
    expect(text('log-count')).toBe('4 entries')
    // off: no more requests
    await byTestId('log-follow').trigger('click')
    await flushPromises()
    const n = logCalls(api.calls).length
    vi.advanceTimersByTime(10000)
    await flushPromises()
    expect(logCalls(api.calls).length).toBe(n)
  })

  it('stops following when the VM stops answering', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    fakeApi(routes(q => (q.after_cursor
      ? { status: 503, body: { error: { code: 'guest_agent_unavailable', message: 'x' } } }
      : { status: 200, body: { vm: 'work', entries: [entry(1, 'first')], cursor: 's=1', truncated: false } })))
    await openApp('/vms/work/log', [WORK])
    await byTestId('log-follow').trigger('click')
    await flushPromises()
    expect(text('log-error')).toBe('The VM\'s guest agent does not answer.')
    expect(exists('log-following')).toBe(false)
  })
})
