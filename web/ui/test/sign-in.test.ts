// Sign-in against POST /api/v1/session: CSRF header, errors from codes, the login brake, and a
// sign-in link from the Host whose token comes in the fragment.
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SignIn from '~/pages/sign-in.vue'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { linkFromHash } from '~/utils/signInLink'
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

// A sign-in link from the Host (agentbox sign-in-link): /sign-in#link=<token>.
const TOKEN = 'Qx7-_aZ09bcdefghijklmnopqrstuvwxyzABCDEFGHI'

describe('sign-in link', () => {
  it('signs in with the token from the fragment, once, and returns to the page it came from', async () => {
    let signedIn = false
    const api = fakeApi({
      'GET /session': () => (signedIn ? SIGNED_IN : SIGNED_OUT),
      'POST /session': () => {
        signedIn = true
        return SIGNED_IN
      },
    })
    await mount(`/sign-in?redirect=/vms/work#link=${TOKEN}`)
    await vi.waitFor(() => expect(useRouter().currentRoute.value.fullPath).toBe('/vms/work/web'))
    const posts = api.calls.filter(c => c.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.path).toBe('/session')
    expect(posts[0]!.body).toEqual({ link: TOKEN })
    expect(posts[0]!.headers['x-agentbox']).toBe('1')
    // the token only ever in a body, never in a path or query
    expect(api.calls.some(c => c.path.includes(TOKEN))).toBe(false)
    expect(useSession().state.authenticated).toBe(true)
  })

  it('names an ended session beside the link notice while the link is sent', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    fakeApi({
      'GET /session': SIGNED_OUT,
      'POST /session': async () => {
        await gate
        return SIGNED_IN
      },
    })
    const wrapper = await mount(`/sign-in?redirect=/vms&expired=1#link=${TOKEN}`)
    await vi.waitFor(() => expect(wrapper.find('[data-testid="sign-in-link"]').exists()).toBe(true))
    expect(wrapper.text()).toContain('Your session has ended')
    release()
  })

  it('takes the token out of the address before it sends it', async () => {
    let address = ''
    fakeApi({
      'GET /session': SIGNED_OUT,
      'POST /session': () => {
        address = useRouter().currentRoute.value.fullPath
        return SIGNED_IN
      },
    })
    await mount(`/sign-in?redirect=/vms#link=${TOKEN}`)
    await vi.waitFor(() => expect(address).not.toBe(''))
    expect(address).toBe('/sign-in?redirect=/vms')
  })

  it('shows the password form after a refused link, in English and German', async () => {
    const api = fakeApi({
      'GET /session': SIGNED_OUT,
      'POST /session': (call) => {
        if ((call.body as { link?: string }).link) {
          return { status: 401, body: { error: { code: 'sign_in_link_refused', message: 'This sign-in link is used, expired or unknown' } } }
        }
        return SIGNED_IN
      },
    })
    const wrapper = await mount(`/sign-in?redirect=/vms/work#link=${TOKEN}`)
    await vi.waitFor(() => expect(wrapper.find('[data-testid="sign-in-error"]').exists()).toBe(true))
    expect(wrapper.find('[data-testid="sign-in-error"]').text()).toContain('This sign-in link is used, expired or unknown.')
    expect(wrapper.find('[data-testid="sign-in-link"]').exists()).toBe(false)
    expect(useRouter().currentRoute.value.fullPath).toBe('/sign-in?redirect=/vms/work')
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    expect(wrapper.find('[data-testid="sign-in-error"]').text()).toContain('Dieser Anmeldelink ist benutzt, abgelaufen oder unbekannt.')
    await useNuxtApp().$i18n.setLocale('en')
    // the password still works, and the link is not sent again
    await submit(wrapper, 'correct horse')
    await vi.waitFor(() => expect(useRouter().currentRoute.value.fullPath).toBe('/vms/work/web'))
    expect(api.calls.filter(c => c.method === 'POST').map(c => c.body)).toEqual([{ link: TOKEN }, { password: 'correct horse' }])
  })

  it('waits as long as the login brake says', async () => {
    const api = fakeApi({
      'GET /session': SIGNED_OUT,
      'POST /session': {
        status: 429,
        headers: { 'Retry-After': '300' },
        body: { error: { code: 'too_many_attempts', message: 'Too many failed sign-ins, try again later' } },
      },
    })
    const wrapper = await mount(`/sign-in#link=${TOKEN}`)
    await vi.waitFor(() => expect(wrapper.find('[data-testid="sign-in-error"]').exists()).toBe(true))
    expect(wrapper.find('[data-testid="sign-in-error"]').text()).toMatch(/Too many failed attempts\. Try again in (29\d|300) s\./)
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(api.calls.filter(c => c.method === 'POST')).toHaveLength(1)
  })

  it('without a link in the fragment shows only the password form', async () => {
    for (const route of ['/sign-in', '/sign-in#top', '/sign-in#link=', '/sign-in#link=a/b', `/sign-in#token=${TOKEN}`]) {
      const api = fakeApi({ 'GET /session': SIGNED_OUT })
      const wrapper = await mount(route)
      await flushPromises()
      expect(api.calls.filter(c => c.method === 'POST')).toHaveLength(0)
      expect(wrapper.find('[data-testid="sign-in-link"]').exists()).toBe(false)
      expect(wrapper.find('input[type="password"]').exists()).toBe(true)
      wrapper.unmount()
    }
  })
})

describe('linkFromHash', () => {
  it('reads only #link=<token>', () => {
    expect(linkFromHash(`#link=${TOKEN}`)).toBe(TOKEN)
    expect(linkFromHash('')).toBeNull()
    expect(linkFromHash('#link=')).toBeNull()
    expect(linkFromHash(`#link=${TOKEN}&x=1`)).toBeNull()
    expect(linkFromHash(`#other=${TOKEN}`)).toBeNull()
  })
})
