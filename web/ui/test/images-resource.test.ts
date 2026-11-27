// The resource `images` through the app (#214): every reader shows the same copy, and a failed
// read shows where ADR 0026 puts it (components/ReadFailure.vue).
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { defineComponent, h } from 'vue'
import { useResource } from '~/utils/resources'
import { fakeApi, FakeEventSource, SIGNED_IN, vm } from './fakes'
import { byTestId, click, exists, openApp, setUp, tearDown } from './vm-helpers'

beforeEach(setUp)
afterEach(tearDown)

const OK = (body: unknown) => ({ status: 200, body })
const FAILED = { status: 502, body: { error: { code: 'agentbox_failed', message: 'agentbox failed' } } }
const OFFLINE = () => { throw new TypeError('Failed to fetch') }

const WORK = vm('work', 'running', { reset_due: false, image: 'base',
  system_disk: { since: null, age_days: 1, file: 'base-2.qcow2', image: 'base', build: '2' } })
const IMAGES = { images: [{ name: 'base', base: true, in_release: true, current: '2', vms: ['work'], running: null, builds: [
  { build: '2', status: 'ok', file: 'base-2.qcow2', current: true, tools: { orca: '1.4.0' }, error: null, problems: [] },
  { build: '1', status: 'ok', file: 'base-1.qcow2', tools: { orca: '1.3.0' }, error: null, problems: [] }] }],
builds_kept: 3, release: '1.0.0', disks_readable: true }

const reads = (api: ReturnType<typeof fakeApi>) => api.calls.filter(c => c.path === '/images').length
const pinnable = () => [...document.body.querySelectorAll('[data-testid="build-choice"] [role="radio"]')].map(r => r.getAttribute('value'))
/** the Images section: other sections of the System page show their own reads */
const images = () => byTestId('system-images').element
const failure = (within: ParentNode = document.body) => within.querySelector('[data-read]')?.getAttribute('data-read') ?? null

describe('one copy of the Images', () => {
  it('a VM\'s versions and its Image dialog share one read, and a build event changes both', async () => {
    let host = IMAGES
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => OK(host) })
    const { es } = await openApp('/vms/work/web', [WORK])
    expect(byTestId('overview-versions').text()).toContain('1.4.0')
    await click('image-change')
    expect(pinnable()).toEqual(['__current__', '2', '1'])
    expect(reads(api)).toBe(1)

    // a Build 3 starts: written into the one copy, no read
    es.emit('build', { image: 'base', build: '3', status: 'running', step: 'build', started: 't', finished: null, error: null, problems: [], actor: null, unit: 'u' })
    await flushPromises()
    expect(reads(api)).toBe(1)
    // and ends: the one copy is read again, once, for both
    host = { ...IMAGES, available: { checked: 't', package_min_age_days: 2, tools: { orca: '1.5.0' } }, images: [{ ...IMAGES.images[0]!, current: '3', builds: [
      { build: '3', status: 'ok', file: 'base-3.qcow2', current: true, tools: { orca: '1.5.0' }, error: null, problems: [] }, ...IMAGES.images[0]!.builds] }] }
    es.emit('build', { image: 'base', build: '3', status: 'ok', step: 'done', started: 't', finished: 't', error: null, problems: [], actor: null, unit: 'u' })
    await flushPromises()
    expect(reads(api)).toBe(2)
    expect(pinnable()).toEqual(['__current__', '3', '2', '1'])
    expect(byTestId('overview-versions').text()).toContain('1.5.0')     // what is available now
  })

  it('the System page shows a VM created meanwhile when it is opened again', async () => {
    let host = IMAGES
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => OK(host) })
    const { es } = await openApp('/system', [WORK])
    expect(reads(api)).toBe(1)
    await useRouter().push('/vms')
    await flushPromises()
    host = { ...IMAGES, images: [{ ...IMAGES.images[0]!, vms: ['work', 'new'] }] }
    es.emit('activity', { time: 't', vm: 'new', actor: 'owner', kind: 'action', action: 'create', code: 'create', outcome: 'ok', description: '' })
    await flushPromises()
    await useRouter().push('/system')
    await flushPromises()
    expect(reads(api)).toBe(2)
    expect(byTestId('system-images').text()).toContain('new')
  })

  it('a failed re-read\'s notice stays through a later Build event', async () => {
    let answers = 0
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (++answers === 2 ? FAILED : OK(IMAGES)) })
    const { es } = await openApp('/system', [WORK])
    es.emit('activity', { time: 't', vm: null, actor: 'timer', kind: 'action', action: 'maintenance.versions', code: 'maintenance.versions', outcome: 'ok', description: '' })
    await flushPromises()
    expect(failure()).toBe('stale')
    es.emit('build', { image: 'base', build: '3', status: 'running', step: 'build', started: 't', finished: null, error: null, problems: [], actor: null, unit: 'u' })
    await flushPromises()
    expect(failure()).toBe('stale')
    expect(answers).toBe(2)
  })

  it('two views of it on screen show the same data and follow the same event', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES) })
    const { es } = await openApp('/vms', [WORK])
    const Reader = defineComponent({
      props: { id: { type: String, required: true } },
      setup(props) {
        const images = useResource('images')
        return () => h('p', { 'data-testid': props.id }, (images.data.value?.images[0]?.builds ?? []).map(b => `${b.build}:${b.status}`).join(' '))
      },
    })
    const both = mount(defineComponent({ render: () => h('div', [h(Reader, { id: 'one' }), h(Reader, { id: 'two' })]) }), { attachTo: document.body })
    await flushPromises()
    expect([byTestId('one').text(), byTestId('two').text()]).toEqual(['2:ok 1:ok', '2:ok 1:ok'])
    es.emit('build', { image: 'base', build: '3', status: 'running', step: 'build', started: 't', finished: null, error: null, problems: [], actor: null, unit: 'u' })
    await flushPromises()
    expect([byTestId('one').text(), byTestId('two').text()]).toEqual(['3:running 2:ok 1:ok', '3:running 2:ok 1:ok'])
    both.unmount()
  })
})

describe('a failed read of the Images', () => {
  it('nothing read yet: the System page shows what and why in place of the section, with Retry', async () => {
    let answers = 0
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (++answers === 1 ? FAILED : OK(IMAGES)) })
    await openApp('/system', [WORK])
    const section = byTestId('system-images').element
    expect(failure(section)).toBe('failed')
    expect(section.textContent).toContain('Could not read the Images.')
    expect(section.textContent).toContain('agentbox failed')
    expect(section.querySelector('[data-testid="image-card"]')).toBeNull()
    await byTestId('images-error').find('button').trigger('click')
    await flushPromises()
    expect(failure(section)).toBeNull()
    expect(section.querySelectorAll('[data-testid="image-card"]')).toHaveLength(1)
    expect(reads(api)).toBe(2)
  })

  it('data shown already: it stays, with a notice that it could not be refreshed and Retry', async () => {
    let answers = 0
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (++answers === 2 ? FAILED : OK(IMAGES)) })
    const { es } = await openApp('/system', [WORK])
    const section = byTestId('system-images').element
    es.emit('activity', { time: 't', vm: null, actor: 'timer', kind: 'action', action: 'maintenance.versions', code: 'maintenance.versions', outcome: 'ok', description: '' })
    await flushPromises()
    expect(answers).toBe(2)
    expect(failure(section)).toBe('stale')
    expect(section.textContent).toContain('Could not be refreshed')
    expect(section.querySelectorAll('[data-testid="image-card"]')).toHaveLength(1)
    await byTestId('images-error').find('button').trigger('click')
    await flushPromises()
    expect(failure(section)).toBeNull()
  })

  it('in the Image dialog, with Retry', async () => {
    let fail = true
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (fail ? FAILED : OK(IMAGES)), 'GET /settings': OK({ settings: {} }) })
    await openApp('/vms/work/web', [WORK])
    await click('image-change')
    const dialog = byTestId('image-dialog').element
    expect(failure(dialog)).toBe('failed')
    expect(dialog.textContent).toContain('Could not read the Images.')
    fail = false
    await (dialog.querySelector('[data-read] button') as HTMLElement).click()
    await flushPromises()
    expect(failure(dialog)).toBeNull()
    expect(pinnable()).toEqual(['__current__', '2', '1'])
  })

  it('the Create dialog picks its Image from the Images read again, not from the stale copy', async () => {
    let host: unknown = { ...IMAGES, images: [...IMAGES.images, { ...IMAGES.images[0]!, name: 'rust', base: false, vms: [] }] }
    let release: (() => void) | null = null
    fakeApi({ 'GET /session': SIGNED_IN,
      'GET /images': () => (release === null ? OK(host) : new Promise(resolve => { const r = release!; release = () => { r(); resolve(OK(host)) } })) as never,
      'GET /settings': OK({ settings: { default_image: { value: 'rust' } } }) })
    const { es } = await openApp('/vms', [WORK])
    await click('vm-create')
    expect(byTestId('create-image').text()).toContain('rust')
    document.body.querySelectorAll<HTMLElement>('[data-testid="create-dialog"] button').forEach((b) => { if (b.textContent?.trim() === 'Cancel') b.click() })
    await flushPromises()
    // the Recipe of rust goes while the dialog is closed
    host = IMAGES
    release = () => {}
    es.emit('activity', { time: 't', vm: null, actor: 'owner', kind: 'recipe', action: 'recipe.remove', code: 'recipe.remove', outcome: 'ok', description: '' })
    await click('vm-create')
    expect(byTestId('create-image').text()).not.toContain('rust')
    release!()
    await flushPromises()
    expect(byTestId('create-image').text()).toContain('base')
    expect(byTestId('create-image').text()).not.toContain('rust')
  })

  it('in the Create dialog, with Retry', async () => {
    let fail = true
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (fail ? FAILED : OK(IMAGES)), 'GET /settings': OK({ settings: {} }) })
    await openApp('/vms', [WORK])
    await click('vm-create')
    const dialog = byTestId('create-dialog').element
    expect(failure(dialog)).toBe('failed')
    expect(dialog.textContent).not.toContain('No Image has a Build')
    fail = false
    await (dialog.querySelector('[data-read] button') as HTMLElement).click()
    await flushPromises()
    expect(failure(dialog)).toBeNull()
    expect(byTestId('create-image').text()).toContain('base')
  })

  it('a VM\'s versions are an extra: they show unknown, nothing else', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': FAILED })
    await openApp('/vms/work/web', [WORK])
    expect(failure(byTestId('overview-versions').element)).toBeNull()
    expect(byTestId('overview-versions').text()).toContain('unknown')
  })

  it('a page that loads while the Control Room is out of reach reads again with the first hello', async () => {
    let answers = 0
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (++answers === 1 ? OFFLINE() : OK(IMAGES)) })
    await openApp('/system', [WORK])
    expect(reads(api)).toBe(2)
    expect(failure(images())).toBeNull()
    expect(byTestId('system-images').element.querySelectorAll('[data-testid="image-card"]')).toHaveLength(1)
  })

  it('with the connection down only the connection notice speaks; the read happens again when it is back', async () => {
    let offline = false
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (offline ? OFFLINE() : OK(IMAGES)) })
    const { es } = await openApp('/system', [WORK])
    const section = byTestId('system-images').element
    offline = true
    es.emit('activity', { time: 't', vm: null, actor: 'timer', kind: 'action', action: 'maintenance.versions', code: 'maintenance.versions', outcome: 'ok', description: '' })
    es.drop()
    await flushPromises()
    expect(reads(api)).toBe(2)
    expect(exists('connection-notice')).toBe(true)
    expect(failure(images())).toBeNull()
    expect(section.querySelectorAll('[data-testid="image-card"]')).toHaveLength(1)     // what it showed stays

    offline = false
    es.emit('hello', { stream: 'ab12cd34', resumed: true })
    await flushPromises()
    expect(reads(api)).toBe(3)
    expect(failure(images())).toBeNull()
  })

  it('says nothing while the read again after the connection came back is on the way', async () => {
    let answer: () => unknown = () => OK(IMAGES)
    let release!: () => void
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => answer() as never })
    const { es } = await openApp('/system', [WORK])
    answer = OFFLINE
    es.emit('activity', { time: 't', vm: null, actor: 'timer', kind: 'action', action: 'maintenance.versions', code: 'maintenance.versions', outcome: 'ok', description: '' })
    es.drop()
    await flushPromises()
    answer = () => new Promise(resolve => { release = () => resolve(OK(IMAGES)) })
    es.emit('hello', { stream: 'ab12cd34', resumed: true })
    await flushPromises()
    expect(exists('connection-notice')).toBe(false)
    expect(failure(images())).toBeNull()
    release()
    await flushPromises()
    expect(failure(images())).toBeNull()
  })

  it('a read that cannot reach the Control Room while the stream is live shows Retry', async () => {
    let offline = true
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': () => (offline ? OFFLINE() : OK(IMAGES)) })
    await openApp('/system', [WORK])
    const section = byTestId('system-images').element
    expect(exists('connection-notice')).toBe(false)
    expect(failure(section)).toBe('failed')
    expect(section.textContent).toContain('Could not read the Images.')
    offline = false
    await byTestId('images-error').find('button').trigger('click')
    await flushPromises()
    expect(failure(section)).toBeNull()
    expect(section.querySelectorAll('[data-testid="image-card"]')).toHaveLength(1)
    expect(reads(api)).toBe(3)
  })

  it('signing out forgets the Images', async () => {
    const api = fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': OK(IMAGES) })
    await openApp('/vms', [WORK])
    const Reader = defineComponent({
      setup() {
        const images = useResource('images')
        return () => h('p', { 'data-testid': 'reader' }, images.data.value ? 'data' : 'none')
      },
    })
    let reader = mount(Reader, { attachTo: document.body })
    await flushPromises()
    expect(byTestId('reader').text()).toBe('data')
    reader.unmount()
    useSession().markSignedOut()
    reader = mount(Reader, { attachTo: document.body })
    expect(byTestId('reader').text()).toBe('none')
    await flushPromises()
    expect(reads(api)).toBe(2)
    reader.unmount()
  })

  it('in German', async () => {
    fakeApi({ 'GET /session': SIGNED_IN, 'GET /images': FAILED })
    await useNuxtApp().$i18n.setLocale('de')
    await openApp('/system', [WORK])
    expect(byTestId('system-images').text()).toContain('Konnte die Images nicht lesen.')
    expect(byTestId('images-error').find('button').text()).toBe('Erneut versuchen')
  })
})
