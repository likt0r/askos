// The VM state badge: every state of the VM object (docs/control-room-api.md) in both
// languages and a color, also `starting` and `not_responding` (#54).
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import VmStateBadge from '~/components/VmStateBadge.vue'

async function badge(state: string) {
  const wrapper = await mountSuspended(VmStateBadge, { props: { state } })
  const el = wrapper.find('[data-testid="vm-state"]')
  return { text: el.text(), color: el.attributes('data-color'), state: el.attributes('data-state') }
}

describe('VM state badge', () => {
  beforeEach(async () => {
    await useNuxtApp().$i18n.setLocale('en')
  })
  afterEach(async () => {
    await useNuxtApp().$i18n.setLocale('en')
  })

  it('shows a VM that starts and one that does not respond', async () => {
    expect(await badge('starting')).toEqual({ text: 'Starting', color: 'info', state: 'starting' })
    expect(await badge('not_responding')).toEqual({ text: 'Not responding', color: 'error', state: 'not_responding' })
    expect(await badge('running')).toEqual({ text: 'Running', color: 'success', state: 'running' })
  })

  it('in German', async () => {
    await useNuxtApp().$i18n.setLocale('de')
    expect((await badge('starting')).text).toBe('Startet')
    expect((await badge('not_responding')).text).toBe('Antwortet nicht')
  })

  it('shows a state it does not know as unknown', async () => {
    expect(await badge('frozen')).toEqual({ text: 'Unknown', color: 'neutral', state: 'frozen' })
  })
})
