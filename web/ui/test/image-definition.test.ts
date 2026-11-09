// An Image's definition (#67) on the System page: its Containerfile, linked on GitHub at the
// Release tag or commit its current Build came from, the repository the Releases come from, and on
// request the Containerfile's text from the installed Release (read-only, GET
// /images/<image>/containerfile). No link where the repository or the commit is not known.
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { githubLink } from '~/utils/images'
import { ok, openPage, useFreshApp } from './system-helpers'

type Wrapper = Awaited<ReturnType<typeof openPage>>['wrapper']

const REPO = 'https://github.com/likt0r/askos'
const COMMIT = 'abcdef0123456789abcdef0123456789abcdef01'
const BASE_TEXT = 'FROM quay.io/fedora/fedora-bootc:44\nRUN dnf -y install git\n'
const RUST_TEXT = 'ARG BASE\nFROM ${BASE}\nRUN dnf -y install clang\n'

const path = (name: string) => (name === 'base' ? 'vm/Containerfile' : `vm/images/${name}/Containerfile`)

function build(name: string, over: Record<string, unknown> = {}) {
  return {
    build: name, status: 'ok', step: 'done', started: '2026-11-01T03:00:00Z', finished: '2026-11-01T03:20:00Z',
    release: '0.2.0', commit: null, base_build: null, error: null, problems: [], actor: 'timer',
    current: false, used_by: [], pinned_by: [], ...over,
  }
}

function image(name: string, over: Record<string, unknown> = {}) {
  return {
    name, base: name === 'base', in_release: true, current: '20261101-0300', vms: [], running: null,
    containerfile: path(name), containerfile_url: `${REPO}/blob/v0.2.0/${path(name)}`,
    builds: [build('20261101-0300', { current: true })], ...over,
  }
}

function overview(images: unknown[], over: Record<string, unknown> = {}) {
  return {
    images, builds_kept: 3, disks_readable: true, release: '0.2.0', repository: 'likt0r/askos',
    repository_url: REPO, ...over,
  }
}

function containerfile(name: string, over: Record<string, unknown> = {}) {
  const text = name === 'base' ? BASE_TEXT : RUST_TEXT
  return {
    image: name, path: path(name), release: '0.2.0', url: `${REPO}/blob/v0.2.0/${path(name)}`, text,
    size: text.length, truncated: false, ...over,
  }
}

async function openSystem(images: unknown, routes: Record<string, unknown> = {}) {
  return openPage('/system', { 'GET /images': ok(images), ...routes } as never)
}

const card = (wrapper: Wrapper, name: string) => wrapper.find(`[data-testid="image-card"][data-image="${name}"]`)
const part = (wrapper: Wrapper, name: string, id: string) => card(wrapper, name).find(`[data-testid="${id}"]`)

async function toggle(wrapper: Wrapper, name: string) {
  await part(wrapper, name, 'containerfile-toggle').trigger('click')
  await flushPromises()
}

describe('Image definition (units)', () => {
  it('links only to pages on GitHub', () => {
    expect(githubLink(`${REPO}/blob/v0.2.0/vm/Containerfile`)).toBe(`${REPO}/blob/v0.2.0/vm/Containerfile`)
    expect(githubLink(REPO)).toBe(REPO)
    for (const odd of ['javascript:alert(1)', 'http://github.com/likt0r/askos', 'https://github.com.evil.example/x',
      'https://evil.example/https://github.com/x', 'https://github.com/a b', '', null, undefined, 42]) {
      expect(githubLink(odd)).toBeNull()
    }
  })
})

describe('System page: Image definition (#67)', () => {
  useFreshApp()

  it('links the repository and each Image\'s Containerfile at the Release of its current Build', async () => {
    const { wrapper } = await openSystem(overview([image('base'), image('rust')]))
    const repo = wrapper.find('[data-testid="images-repository"]')
    expect(repo.element.tagName).toBe('A')
    expect(repo.attributes('href')).toBe(REPO)
    expect(repo.attributes('target')).toBe('_blank')
    expect(repo.attributes('rel')).toContain('noopener')
    expect(repo.text()).toBe('likt0r/askos')
    expect(wrapper.find('[data-testid="images-source"]').text()).toContain('Repository: likt0r/askos')

    const link = part(wrapper, 'base', 'containerfile-link')
    expect(link.element.tagName).toBe('A')
    expect(link.attributes('href')).toBe(`${REPO}/blob/v0.2.0/vm/Containerfile`)
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
    expect(link.text()).toBe('vm/Containerfile')
    expect(part(wrapper, 'base', 'image-definition').text()).toContain('Definition:')
    expect(part(wrapper, 'base', 'containerfile-ref').text()).toBe('as in the current Build, Release 0.2.0')
    // a derived Image: its own Containerfile
    expect(part(wrapper, 'rust', 'containerfile-link').attributes('href')).toBe(`${REPO}/blob/v0.2.0/vm/images/rust/Containerfile`)
    expect(part(wrapper, 'rust', 'containerfile-link').text()).toBe('vm/images/rust/Containerfile')
  })

  it('links a Build from before ADR 0010 at its commit', async () => {
    const old = image('base', {
      current: '20261031-0300', containerfile_url: `${REPO}/blob/${COMMIT}/vm/Containerfile`,
      builds: [build('20261031-0300', { current: true, release: null, commit: COMMIT })],
    })
    const { wrapper } = await openSystem(overview([old]))
    expect(part(wrapper, 'base', 'containerfile-link').attributes('href')).toBe(`${REPO}/blob/${COMMIT}/vm/Containerfile`)
    expect(part(wrapper, 'base', 'containerfile-ref').text()).toBe('as in the current Build, commit abcdef012345')
  })

  it('shows the paths without links when the repository or the commit is not known, and no error', async () => {
    const { wrapper } = await openSystem(overview([
      image('base', { current: null, builds: [], containerfile_url: null }),
      image('odd', { containerfile_url: 'javascript:alert(1)' }),
      // from a backend before #67: no definition at all
      image('older', { containerfile: undefined, containerfile_url: undefined }),
    ], { repository: null, repository_url: null }))
    expect(wrapper.find('[data-testid="images-repository"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="images-source"]').text()).not.toContain('Repository')
    expect(wrapper.find('[data-testid="images-error"]').exists()).toBe(false)
    for (const name of ['base', 'odd']) {
      expect(part(wrapper, name, 'containerfile-link').exists()).toBe(false)
      expect(part(wrapper, name, 'containerfile-ref').exists()).toBe(false)
      expect(part(wrapper, name, 'containerfile-path').text()).toBe(path(name))
      expect(card(wrapper, name).find('[data-testid="image-definition"] a').exists()).toBe(false)
    }
    expect(part(wrapper, 'older', 'image-definition').exists()).toBe(false)
  })

  it('names the repository without a link when its page is not known', async () => {
    const { wrapper } = await openSystem(overview([image('base')], { repository_url: null }))
    const repo = wrapper.find('[data-testid="images-repository"]')
    expect(repo.element.tagName).toBe('SPAN')
    expect(repo.text()).toBe('likt0r/askos')
  })

  it('shows the Containerfile on request, read-only, and reads it once', async () => {
    const { wrapper, api } = await openSystem(overview([image('base')]), {
      'GET /images/base/containerfile': ok(containerfile('base')),
    })
    const button = () => part(wrapper, 'base', 'containerfile-toggle')
    expect(button().attributes('aria-expanded')).toBe('false')
    expect(button().text()).toBe('Show Containerfile')
    expect(part(wrapper, 'base', 'containerfile').exists()).toBe(false)
    expect(api.calls.some(c => c.path.includes('/containerfile'))).toBe(false)

    await toggle(wrapper, 'base')
    const text = part(wrapper, 'base', 'containerfile-text')
    expect(text.element.tagName).toBe('PRE')
    expect(text.text()).toBe(BASE_TEXT.trim())
    expect(text.attributes('aria-label')).toBe('Containerfile vm/Containerfile')
    expect(part(wrapper, 'base', 'containerfile-source').text()).toContain('From the installed Release 0.2.0, which the next Build takes.')
    const onGithub = part(wrapper, 'base', 'containerfile-file-link')
    expect(onGithub.attributes('href')).toBe(`${REPO}/blob/v0.2.0/vm/Containerfile`)
    expect(onGithub.text()).toBe('On GitHub')
    // the current Build came from the installed Release: nothing to say
    expect(part(wrapper, 'base', 'containerfile-differs').exists()).toBe(false)
    expect(part(wrapper, 'base', 'containerfile-truncated').exists()).toBe(false)
    expect(button().attributes('aria-expanded')).toBe('true')
    expect(button().text()).toBe('Hide Containerfile')
    // nothing in it can be edited
    expect(part(wrapper, 'base', 'containerfile').find('textarea, input, [contenteditable]').exists()).toBe(false)

    await toggle(wrapper, 'base')
    expect(part(wrapper, 'base', 'containerfile').exists()).toBe(false)
    await toggle(wrapper, 'base')
    expect(part(wrapper, 'base', 'containerfile-text').text()).toBe(BASE_TEXT.trim())
    const reads = api.calls.filter(c => c.path === '/images/base/containerfile')
    expect(reads.map(c => c.method)).toEqual(['GET'])
  })

  it('shows a derived Image\'s Containerfile and says when the current Build came from another Release', async () => {
    const rust = image('rust', {
      containerfile_url: `${REPO}/blob/v0.1.0/vm/images/rust/Containerfile`,
      builds: [build('20261101-0300', { current: true, release: '0.1.0' })],
    })
    const { wrapper } = await openSystem(overview([rust]), { 'GET /images/rust/containerfile': ok(containerfile('rust')) })
    expect(part(wrapper, 'rust', 'containerfile-ref').text()).toBe('as in the current Build, Release 0.1.0')
    await toggle(wrapper, 'rust')
    expect(part(wrapper, 'rust', 'containerfile-text').text()).toBe(RUST_TEXT.trim())
    expect(part(wrapper, 'rust', 'containerfile-differs').text()).toBe(
      'The current Build came from Release 0.1.0; its Containerfile may differ from this one until the next Build.')
    expect(part(wrapper, 'rust', 'containerfile-file-link').attributes('href')).toBe(`${REPO}/blob/v0.2.0/vm/images/rust/Containerfile`)
  })

  it('says so for a Build from a commit, one of unknown origin, and nothing without a Build', async () => {
    const { wrapper } = await openSystem(overview([
      image('base', { current: '20261031-0300', builds: [build('20261031-0300', { current: true, release: null, commit: COMMIT })] }),
      image('ml', { builds: [build('20261101-0300', { current: true, status: 'migrated', release: null })], containerfile_url: null }),
      image('cuda', { current: null, builds: [], containerfile_url: null }),
    ]), {
      'GET /images/base/containerfile': ok(containerfile('base')),
      'GET /images/ml/containerfile': ok(containerfile('ml')),
      'GET /images/cuda/containerfile': ok(containerfile('cuda')),
    })
    for (const name of ['base', 'ml', 'cuda']) await toggle(wrapper, name)
    expect(part(wrapper, 'base', 'containerfile-differs').text()).toContain('The current Build came from commit abcdef012345;')
    expect(part(wrapper, 'ml', 'containerfile-differs').text()).toContain('Where the current Build came from is not recorded;')
    expect(part(wrapper, 'cuda', 'containerfile-differs').exists()).toBe(false)
    expect(part(wrapper, 'cuda', 'containerfile-text').text()).toBe(RUST_TEXT.trim())
  })

  it('says when only the beginning is shown, and links nothing for a checkout', async () => {
    const { wrapper } = await openSystem(overview([image('base')]), {
      'GET /images/base/containerfile': ok(containerfile('base', { truncated: true, size: 90000, release: null, url: null })),
    })
    await toggle(wrapper, 'base')
    expect(part(wrapper, 'base', 'containerfile-truncated').text()).toBe('Only the beginning is shown: the file has 90000 bytes.')
    expect(part(wrapper, 'base', 'containerfile-source').text()).toBe('From the installed Release, which the next Build takes.')
    expect(part(wrapper, 'base', 'containerfile-file-link').exists()).toBe(false)
  })

  it('shows an error translated, and reads again when opened again', async () => {
    let answers = 0
    const { wrapper } = await openSystem(overview([image('base')]), {
      'GET /images/base/containerfile': () => (++answers === 1
        ? { status: 404, body: { error: { code: 'containerfile_not_found', message: 'The installed Release has no Containerfile for this Image' } } }
        : ok(containerfile('base'))),
    })
    await toggle(wrapper, 'base')
    expect(part(wrapper, 'base', 'containerfile-error').text()).toBe('The installed Release has no Containerfile for this Image.')
    expect(part(wrapper, 'base', 'containerfile-text').exists()).toBe(false)
    expect(wrapper.find('[data-testid="images-error"]').exists()).toBe(false)
    await toggle(wrapper, 'base')
    await toggle(wrapper, 'base')
    expect(answers).toBe(2)
    expect(part(wrapper, 'base', 'containerfile-error').exists()).toBe(false)
    expect(part(wrapper, 'base', 'containerfile-text').text()).toBe(BASE_TEXT.trim())
  })

  it('offers no text for an Image the installed Release no longer defines, but keeps its link', async () => {
    const { wrapper, api } = await openSystem(overview([image('old', { in_release: false })]))
    expect(part(wrapper, 'old', 'containerfile-toggle').exists()).toBe(false)
    expect(part(wrapper, 'old', 'containerfile-link').attributes('href')).toBe(`${REPO}/blob/v0.2.0/vm/images/old/Containerfile`)
    expect(api.calls.some(c => c.path.includes('/containerfile'))).toBe(false)
  })

  it('in German', async () => {
    const rust = image('rust', { builds: [build('20261101-0300', { current: true, release: '0.1.0' })] })
    const { wrapper } = await openSystem(overview([rust]), { 'GET /images/rust/containerfile': ok(containerfile('rust')) })
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    expect(wrapper.find('[data-testid="images-source"]').text()).toContain('Repository: likt0r/askos')
    expect(part(wrapper, 'rust', 'image-definition').text()).toContain('Definition:')
    expect(part(wrapper, 'rust', 'containerfile-ref').text()).toBe('wie im aktuellen Build, Release 0.1.0')
    expect(part(wrapper, 'rust', 'containerfile-toggle').text()).toBe('Containerfile zeigen')
    await toggle(wrapper, 'rust')
    expect(part(wrapper, 'rust', 'containerfile-toggle').text()).toBe('Containerfile ausblenden')
    expect(part(wrapper, 'rust', 'containerfile-source').text()).toContain('Aus dem installierten Release 0.2.0, das der nächste Build nimmt.')
    expect(part(wrapper, 'rust', 'containerfile-file-link').text()).toBe('Auf GitHub')
    expect(part(wrapper, 'rust', 'containerfile-differs').text()).toBe(
      'Der aktuelle Build kam aus Release 0.1.0; sein Containerfile kann bis zum nächsten Build von diesem abweichen.')
  })
})
