// The Release of Agent Box is shown in the sidebar footer, from the resource `version` (GET /version).
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { ok, openPage, useFreshApp } from './system-helpers'

const version = (extra: Record<string, unknown> = {}) => ok({ api: 1, version: 'aaaa', ui: 'nuxt', installed: null, ...extra })

describe('Release in the sidebar', () => {
  useFreshApp()

  it('shows the Release the Host runs', async () => {
    const { wrapper, api } = await openPage('/vms', { 'GET /version': version({ release: '0.1.0' }) })
    expect(wrapper.find('[data-testid="release"]').text()).toBe('Agent Box v0.1.0')
    expect(api.calls.filter(c => c.path === '/version')).toHaveLength(1)
  })

  it('shows the Release a reconnect that could not resume reads', async () => {
    let release = '0.1.0'
    const { wrapper, es } = await openPage('/vms', { 'GET /version': () => version({ release }) })
    release = '0.2.0'
    es.emit('hello', { stream: 'efgh5678', resumed: false })
    await flushPromises()
    expect(wrapper.find('[data-testid="release"]').text()).toBe('Agent Box v0.2.0')
  })

  it('shows nothing when the Host reports no Release', async () => {
    const { wrapper } = await openPage('/vms', { 'GET /version': version({ release: null }) })
    expect(wrapper.find('[data-testid="release"]').exists()).toBe(false)
  })

  it('shows nothing when the answer has no release field (an older backend)', async () => {
    const { wrapper } = await openPage('/vms', { 'GET /version': version() })
    expect(wrapper.find('[data-testid="release"]').exists()).toBe(false)
  })
})
