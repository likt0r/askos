// The API client: CSRF header on changes only, errors as codes, Retry-After, 401.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, onUnauthenticated } from '~/utils/api'
import { fakeApi } from './fakes'

afterEach(() => onUnauthenticated(null))

describe('api', () => {
  it('sends X-Agentbox: 1 on every change, not on reads', async () => {
    const { calls } = fakeApi({
      'GET /vms': { status: 200, body: { vms: [], problems: [] } },
      'POST /vms/work/start': { status: 200, body: {} },
      'PUT /vms/work/github-owners': { status: 200, body: {} },
      'PATCH /settings': { status: 200, body: {} },
      'DELETE /focus': { status: 200, body: {} },
    })
    expect(await api('/vms')).toEqual({ vms: [], problems: [] })
    await api('/vms/work/start', { method: 'POST' })
    await api('/vms/work/github-owners', { method: 'PUT', body: { owners: ['likt0r'] } })
    await api('/settings', { method: 'PATCH', body: { session_hours: 8 } })
    await api('/focus', { method: 'DELETE' })
    expect(calls.map(c => [c.method, c.headers['x-agentbox']])).toEqual([
      ['GET', undefined], ['POST', '1'], ['PUT', '1'], ['PATCH', '1'], ['DELETE', '1'],
    ])
    expect(calls[2]!.body).toEqual({ owners: ['likt0r'] })
  })

  it('turns error answers into ApiError with the stable code', async () => {
    fakeApi({
      'POST /vms': { status: 400, body: { error: { code: 'invalid_name', message: 'Invalid VM name', field: 'name', detail: 'Name ungueltig' } } },
      'GET /host': { status: 503, headers: { 'Retry-After': '10' }, body: { error: { code: 'readings_unavailable', message: 'No readings' } } },
      'GET /odd': { status: 502, body: 'not an error object' },
    })
    const e = await api('/vms', { method: 'POST', body: { name: 'X' } }).catch(e => e)
    expect(e).toBeInstanceOf(ApiError)
    expect(e).toMatchObject({ status: 400, code: 'invalid_name', field: 'name', detail: 'Name ungueltig', message: 'Invalid VM name' })
    expect(await api('/host').catch(e => e)).toMatchObject({ code: 'readings_unavailable', retryAfter: 10 })
    expect(await api('/odd').catch(e => e)).toMatchObject({ status: 502, code: 'http_502' })
  })

  it('reports a network failure as code network', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }))
    expect(await api('/vms').catch(e => e)).toMatchObject({ status: 0, code: 'network' })
  })

  it('reports an ended session, but not a failed sign-in', async () => {
    const ended = vi.fn()
    onUnauthenticated(ended)
    fakeApi({
      'GET /vms': { status: 401, body: { error: { code: 'unauthenticated', message: 'Sign in first' } } },
      'POST /session': { status: 401, body: { error: { code: 'wrong_password', message: 'Wrong password' } } },
      'GET /session': { status: 401, body: { error: { code: 'unauthenticated', message: 'Sign in first' } } },
    })
    await api('/session', { method: 'POST', body: { password: 'x' } }).catch(() => {})
    await api('/session').catch(() => {})
    expect(ended).not.toHaveBeenCalled()
    await api('/vms').catch(() => {})
    expect(ended).toHaveBeenCalledTimes(1)
  })
})
