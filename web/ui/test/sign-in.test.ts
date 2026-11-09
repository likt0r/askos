// Sign-in against POST /api/v1/session: CSRF header, errors from codes, the login brake.
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SignIn from '~/pages/sign-in.vue'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { fakeApi, SIGNED_IN, SIGNED_OUT } from './fakes'

async function mount(route = '/sign-in?redirect=/vms/work') {
  const wrapper = await mountSuspended(SignIn, { route })
  return wrapper
}

async function submit(wrapper: Awaited<ReturnType<typeof mount>>, password: string) {
  await wrapper.find('input[type="password"]').setValue(password)
  await wrapper.find('form').trigger('submit')
  await flushPromises()
}

beforeEach(async () => {
  resetSessionForTests()
  resetLiveForTests()
  await useNuxtApp().$i18n.setLocale('en')
})
afterEach(() => {
  resetSessionForTests()
})

describe('sign-in', () => {
  it('sends the password with the CSRF header and returns to the page it came from', async () => {
    let signedIn = false
    const api = fakeApi({
      'GET /session': () => (signedIn ? SIGNED_IN : SIGNED_OUT),
      'POST /session': () => {
        signedIn = true
        return SIGNED_IN
      },
    })
    const wrapper = await mount()
    expect(wrapper.text()).toContain('Sign in')
    await submit(wrapper, 'correct horse')

    const post = api.calls.find(c => c.method === 'POST')!
    expect(post.path).toBe('/session')
    expect(post.body).toEqual({ password: 'correct horse' })
    expect(post.headers['x-agentbox']).toBe('1')
    expect(post.headers['content-type']).toBe('application/json')
    await vi.waitFor(() => expect(useRouter().currentRoute.value.fullPath).toBe('/vms/work/web'))  // the VM's first tab
    expect(useSession().state.authenticated).toBe(true)
  })

  it('never returns to another origin', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT, 'POST /session': SIGNED_IN })
    const wrapper = await mount('/sign-in?redirect=//evil.example/x')
    await submit(wrapper, 'pw')
    await vi.waitFor(() => expect(useRouter().currentRoute.value.fullPath).toBe('/vms'))
  })

  it('shows the translated error of a wrong password, in English and German', async () => {
    fakeApi({
      'GET /session': SIGNED_OUT,
      'POST /session': { status: 401, body: { error: { code: 'wrong_password', message: 'Wrong password' } } },
    })
    const wrapper = await mount()
    await submit(wrapper, 'nope')
    expect(wrapper.find('[data-testid="sign-in-error"]').text()).toContain('Wrong password.')
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    expect(wrapper.find('[data-testid="sign-in-error"]').text()).toContain('Falsches Passwort.')
    expect(useRouter().currentRoute.value.path).toBe('/sign-in')
  })

  it('waits as long as the login brake says and sends nothing meanwhile', async () => {
    const api = fakeApi({
      'GET /session': SIGNED_OUT,
      'POST /session': {
        status: 429,
        headers: { 'Retry-After': '120' },
        body: { error: { code: 'too_many_attempts', message: 'Too many failed sign-ins, try again later' } },
      },
    })
    const wrapper = await mount()
    await submit(wrapper, 'again')
    expect(wrapper.find('[data-testid="sign-in-error"]').text()).toMatch(/Too many failed attempts\. Try again in 1[12]\d s\./)
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(api.calls.filter(c => c.method === 'POST')).toHaveLength(1)
  })

  it('says when the backend cannot be reached', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT })
    const wrapper = await mount()
    globalThis.fetch = (async () => {
      throw new TypeError('Failed to fetch')
    }) as typeof fetch
    await submit(wrapper, 'pw')
    expect(wrapper.find('[data-testid="sign-in-error"]').text()).toContain('cannot be reached')
  })

  it('explains an ended session', async () => {
    fakeApi({ 'GET /session': SIGNED_OUT })
    const wrapper = await mount('/sign-in?redirect=/vms&expired=1')
    expect(wrapper.text()).toContain('Your session has ended')
  })
})
