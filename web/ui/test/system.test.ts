// System page (#16, #17): Images and Builds (overview, rebuild, live progress, logs), services,
// Host reboot. Mounts the app on /system against a fake backend and a fake event stream.
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { appendLines, applyBuild, stepIndex } from '~/utils/images'
import type { ImagesOverview } from '~/utils/images'
import { ok, openPage, useFreshApp } from './system-helpers'

type Page = Awaited<ReturnType<typeof openPage>>

function build(name: string, over: Record<string, unknown> = {}) {
  return {
    build: name, status: 'ok', step: 'done', started: '2026-11-01T03:00:00Z', finished: '2026-11-01T03:20:00Z',
    release: '0.1.0', commit: null, base_build: null, error: null, problems: [], actor: 'timer',
    current: false, used_by: [], pinned_by: [], ...over,
  }
}

function image(name: string, over: Record<string, unknown> = {}) {
  return { name, base: false, in_release: true, current: null, vms: [], running: null, builds: [], ...over }
}

function overview(images: unknown[], over: Record<string, unknown> = {}) {
  return { images, builds_kept: 3, disks_readable: true, release: '0.2.0', ...over }
}

const BASE = image('base', {
  base: true, current: '20261101-0300', vms: ['work', 'priv'],
  builds: [
    build('20261103-0300', {
      status: 'rejected', step: 'check', problems: ['firewall.nft: the allowlist rule is missing', 'sshd: root login allowed'],
      error: 'safeguards weakened',
    }),
    build('20261102-0300', { status: 'failed', step: 'build', error: 'dnf: package not found: foo', problems: [] }),
    build('20261101-0300', { current: true, used_by: ['work'], pinned_by: ['priv'], agents: { claude: '2.1.0', codex: null } }),
    // from before ADR 0010: built from a commit of main, no Release
    build('20261031-0300', { used_by: null, pinned_by: [], release: null, commit: 'abcdef0123456789abcdef0123456789abcdef01' }),
  ],
})
const ML = image('ml', {
  current: '20261030-0300', vms: [],
  builds: [build('20261030-0300', { current: true, base_build: '20261101-0300' })],
})

const IMAGES = overview([BASE, ML])

const rows = (wrapper: Page['wrapper'], name = 'base') =>
  wrapper.find(`[data-testid="image-card"][data-image="${name}"]`).findAll('[data-testid="build-row"]')
const card = (wrapper: Page['wrapper'], name = 'base') => wrapper.find(`[data-testid="image-card"][data-image="${name}"]`)

async function openSystem(routes: Record<string, unknown> = {}) {
  return openPage('/system', { 'GET /images': ok(IMAGES), ...routes } as never)
}

describe('Images and Builds (units)', () => {
  it('orders the steps and finds them by name', () => {
    expect(['source', 'build', 'check', 'disk', 'done'].map(stepIndex)).toEqual([0, 1, 2, 3, 4])
    expect(stepIndex(null)).toBe(-1)
    expect(stepIndex('nonsense')).toBe(-1)
  })

  it('drops repeated log lines by cursor and keeps the newest 2000', () => {
    const line = (n: number) => ({ time: '2026-11-03T12:00:00Z', message: `l${n}`, cursor: `c${n}` })
    const held = [line(1), line(2)]
    expect(appendLines(held, [line(2)])).toBe(held)
    expect(appendLines(held, [line(2), line(3)]).map(l => l.cursor)).toEqual(['c1', 'c2', 'c3'])
    const many = appendLines([], Array.from({ length: 2100 }, (_, i) => line(i)))
    expect(many).toHaveLength(2000)
    expect(many[0]!.cursor).toBe('c100')
  })

  it('applies a Build event: replaces the record, adds a new one newest first, follows the running Build', () => {
    const o = IMAGES as unknown as ImagesOverview
    const started = applyBuild(o, { ...build('20261104-0300', { status: 'running', step: 'build', finished: null }), image: 'ml' } as never)!
    const ml = started.images.find(i => i.name === 'ml')!
    expect(ml.builds.map(b => b.build)).toEqual(['20261104-0300', '20261030-0300'])
    expect(ml.builds[0]!.release).toBe('0.1.0')
    expect(ml.running).toEqual({ build: '20261104-0300', step: 'build', started: '2026-11-01T03:00:00Z' })
    const done = applyBuild(started, { ...build('20261104-0300', { status: 'ok', step: 'done' }), image: 'ml' } as never)!
    expect(done.images.find(i => i.name === 'ml')!.running).toBeNull()
    // the other Image is untouched; an unknown Image is not applied
    expect(done.images.find(i => i.name === 'base')).toBe(o.images.find(i => i.name === 'base'))
    expect(applyBuild(o, { ...build('x'), image: 'nope' } as never)).toBeNull()
  })

  it('keeps who uses a Build when its record is replaced by an event', () => {
    const o = IMAGES as unknown as ImagesOverview
    const next = applyBuild(o, { ...build('20261101-0300', { status: 'ok' }), image: 'base' } as never)!
    const b = next.images[0]!.builds.find(x => x.build === '20261101-0300')!
    expect(b.used_by).toEqual(['work'])
    expect(b.pinned_by).toEqual(['priv'])
    expect(b.current).toBe(true)
  })
})

describe('System page: Images', () => {
  useFreshApp()

  it('shows each Image with its current Build, Base badge, VMs and the installed Release', async () => {
    const { wrapper } = await openSystem()
    const cards = wrapper.findAll('[data-testid="image-card"]')
    expect(cards.map(c => c.attributes('data-image'))).toEqual(['base', 'ml'])
    const base = card(wrapper, 'base')
    expect(base.text()).toContain('Base image')
    expect(base.find('[data-testid="image-current"]').text()).toBe('20261101-0300')
    expect(base.find('[data-testid="image-vms"]').text()).toContain('Used by: work, priv')
    expect(base.find('[data-testid="image-vms"] a[href="/vms/work"]').exists()).toBe(true)
    const ml = card(wrapper, 'ml')
    expect(ml.text()).not.toContain('Base image')
    expect(ml.find('[data-testid="image-vms"]').text()).toBe('No VM uses this Image.')
    const src = wrapper.find('[data-testid="images-source"]').text()
    expect(src).toContain('installed Release 0.2.0')
    expect(src).toContain('last 3 Builds')
  })

  it('marks an Image the installed Release no longer defines and one without a Build', async () => {
    const { wrapper } = await openSystem({ 'GET /images': ok(overview([image('old', { in_release: false })])) })
    const old = card(wrapper, 'old')
    expect(old.text()).toContain('not in the installed Release')
    expect(old.find('[data-testid="image-current"]').text()).toBe('none yet')
    expect(old.text()).toContain('No Builds yet.')
  })

  it('lists the Builds with status, current mark, Release, and who uses or is pinned to each', async () => {
    const { wrapper } = await openSystem()
    const r = rows(wrapper)
    expect(r.map(x => [x.attributes('data-build'), x.attributes('data-status')])).toEqual([
      ['20261103-0300', 'rejected'], ['20261102-0300', 'failed'], ['20261101-0300', 'ok'], ['20261031-0300', 'ok'],
    ])
    expect(r.map(x => x.find('[data-testid="build-status"]').text())).toEqual(['Rejected', 'Failed', 'OK', 'OK'])
    const current = r[2]!
    expect(current.text()).toContain('current')
    expect(current.find('[data-testid="build-release"]').text()).toBe('Release 0.1.0')
    expect(current.find('[data-testid="build-commit"]').exists()).toBe(false)
    // a Build from before ADR 0010 names the commit of main it came from
    expect(r[3]!.find('[data-testid="build-release"]').exists()).toBe(false)
    expect(r[3]!.find('[data-testid="build-commit"]').text()).toBe('abcdef012345')
    expect(current.text()).toContain('claude 2.1.0')
    expect(current.find('[data-testid="build-users"]').text()).toBe('System disk of work · pinned by priv')
    expect(r[3]!.find('[data-testid="build-users"]').exists()).toBe(false)
    expect(r[0]!.text()).not.toContain('current')
  })

  it('shows why a Build was rejected or failed', async () => {
    const { wrapper } = await openSystem()
    const r = rows(wrapper)
    const rejected = r[0]!.find('[data-testid="build-reasons"]')
    expect(rejected.text()).toContain('Rejected: a safeguard is missing or weakened')
    expect(rejected.findAll('li').map(l => l.text())).toEqual(['firewall.nft: the allowlist rule is missing', 'sshd: root login allowed'])
    const failed = r[1]!.find('[data-testid="build-reasons"]')
    expect(failed.text()).toContain('The Build failed')
    expect(failed.text()).toContain('dnf: package not found: foo')
    expect(r[2]!.find('[data-testid="build-reasons"]').exists()).toBe(false)
  })

  it('copes with a failed Build that has neither reasons nor an error', async () => {
    const odd = image('odd', { builds: [{ ...build('b1', { status: 'failed' }), problems: undefined, error: null }] })
    const { wrapper } = await openSystem({ 'GET /images': ok(overview([odd])) })
    expect(rows(wrapper, 'odd')).toHaveLength(1)
    expect(rows(wrapper, 'odd')[0]!.find('[data-testid="build-reasons"]').text()).toContain('The Build failed')
  })

  it('says so when the Images cannot be read, and tries again', async () => {
    let answers = 0
    const { wrapper } = await openSystem({
      'GET /images': () => (++answers === 1 ? { status: 502, body: { error: { code: 'agentbox_failed', message: 'agentbox failed' } } } : ok(IMAGES)),
    })
    expect(wrapper.find('[data-testid="images-error"]').exists()).toBe(true)
    const retry = wrapper.find('[data-testid="images-error"] button')
    await retry.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="images-error"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="image-card"]')).toHaveLength(2)
  })

  it('warns when not every System disk could be read', async () => {
    const { wrapper } = await openSystem({ 'GET /images': ok(overview([ML], { disks_readable: false })) })
    expect(wrapper.find('[data-testid="system-images"]').text()).toContain('Not every System disk could be read')
  })
})

describe('System page: tool versions (#104)', () => {
  useFreshApp()

  const AVAILABLE = {
    checked: '2026-11-08T04:00:00Z', package_min_age_days: 2, orca_version: null,
    tools: { 'orca': '1.4.251', 'claude-code': '2.1.7', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': null },
  }
  const VERSIONED = overview([image('base', {
    base: true, current: '20261107-0300', vms: ['work'], package_updates: 37, updates: ['orca', 'claude-code', 'packages'],
    update: true,
    builds: [build('20261107-0300', {
      current: true, updates: ['orca', 'claude-code'],
      tools: { 'orca': '1.4.241', 'claude-code': '2.1.5', 'codex': '0.80.0', 'opencode': '1.2.3', 'fedora': '44' },
    })],
  })], { available: AVAILABLE })
  const cells = (c: ReturnType<typeof card>, id: string) => c.findAll('[data-testid="tool-row"]').map(r => r.find(`[data-testid="${id}"]`).text())

  it('shows the current Build\'s versions next to what is available and marks the Image with an update', async () => {
    const { wrapper } = await openSystem({ 'GET /images': ok(VERSIONED) })
    const base = card(wrapper)
    expect(base.find('[data-testid="image-update"]').text()).toBe('Update available')
    const versions = base.find('[data-testid="image-versions"]')
    expect(versions.text()).toContain('In Build 20261107-0300')
    expect(versions.findAll('[data-testid="tool-row"]').map(r => [r.attributes('data-tool'), r.attributes('data-update')])).toEqual([
      ['orca', 'true'], ['claude-code', 'true'], ['codex', 'false'], ['opencode', 'false'], ['fedora', 'false'],
    ])
    expect(cells(base, 'tool-installed')).toEqual(['1.4.241', '2.1.5', '0.80.0', '1.2.3', '44'])
    const available = cells(base, 'tool-available')
    expect(available[0]).toContain('1.4.251')
    expect(available[0]).not.toContain('pinned')
    expect(available[0]).toContain('a new Build brings it')
    expect(available[1]).toContain('2.1.7')
    expect(available.slice(2)).toEqual(['0.80.0', '1.2.3', '—'])
    expect(versions.find('[data-testid="tool-packages"]').text()).toContain('37 updates available')
    const checked = wrapper.find('[data-testid="images-available"]').text()
    expect(checked).toContain('the newest at least 2 days old')
    expect(checked).toContain('Orca from its GitHub release')
    expect(checked).not.toContain('pinned')
  })

  it('marks Orca when the Settings pin its version (#113)', async () => {
    const pinned = overview([image('base', {
      base: true, current: '20261107-0300', updates: ['orca'], update: true,
      builds: [build('20261107-0300', { current: true, updates: ['orca'], tools: { 'orca': '1.4.251' } })],
    })], { available: { ...AVAILABLE, orca_version: '1.4.250', tools: { ...AVAILABLE.tools, orca: '1.4.250' } } })
    const { wrapper } = await openSystem({ 'GET /images': ok(pinned) })
    const base = card(wrapper)
    expect(cells(base, 'tool-available')[0]).toContain('1.4.250')
    expect(base.find('[data-testid="orca-pinned"]').text()).toBe('pinned')
    expect(base.findAll('[data-testid="orca-pinned"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="images-available"]').text()).toContain('Orca is pinned to 1.4.250 in the Settings.')
  })

  it('says unknown where nothing was recorded or checked, and marks nothing', async () => {
    const old = image('base', { current: '20261031-0300', update: null, package_updates: null, builds: [build('20261031-0300', { current: true })] })
    const { wrapper } = await openSystem({ 'GET /images': ok(overview([old])) })
    const base = card(wrapper)
    expect(base.find('[data-testid="image-update"]').exists()).toBe(false)
    expect(cells(base, 'tool-installed')).toEqual(['unknown', 'unknown', 'unknown', 'unknown', 'unknown'])
    expect(cells(base, 'tool-available').slice(0, 4)).toEqual(['unknown', 'unknown', 'unknown', 'unknown'])
    expect(base.find('[data-testid="tool-packages"]').text()).toContain('unknown')
    expect(base.find('[data-testid="orca-pinned"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="images-available"]').text()).toContain('not checked yet')
  })

  it('shows no versions for an Image without a Build', async () => {
    const { wrapper } = await openSystem({ 'GET /images': ok(overview([image('cuda')], { available: AVAILABLE })) })
    expect(card(wrapper, 'cuda').find('[data-testid="image-versions"]').exists()).toBe(false)
  })

  it('in German', async () => {
    const { wrapper } = await openSystem({ 'GET /images': ok(VERSIONED) })
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    const base = card(wrapper)
    expect(base.find('[data-testid="image-update"]').text()).toBe('Update verfügbar')
    expect(base.find('[data-testid="tool-packages"]').text()).toContain('37 Updates verfügbar')
    expect(cells(base, 'tool-available')[0]).toContain('1.4.251')
    expect(wrapper.find('[data-testid="images-available"]').text()).toContain('mindestens 2 Tage alte')
  })
})

describe('System page: rebuild', () => {
  useFreshApp()

  it('posts the Image name with X-Agentbox: 1 and fetches the overview again', async () => {
    const { wrapper, api } = await openSystem({ 'POST /builds': { status: 202, body: { image: 'ml', unit: 'agentbox-build@ml.service', started: true, message: 'ok' } } })
    const before = api.calls.filter(c => c.method === 'GET' && c.path === '/images').length
    await card(wrapper, 'ml').find('[data-testid="image-rebuild"]').trigger('click')
    await flushPromises()
    const post = api.calls.filter(c => c.method === 'POST' && c.path === '/builds')
    expect(post).toHaveLength(1)
    expect(post[0]!.body).toEqual({ image: 'ml' })
    expect(post[0]!.headers['x-agentbox']).toBe('1')
    expect(api.calls.filter(c => c.method === 'GET' && c.path === '/images')).toHaveLength(before + 1)
  })

  it('"Rebuild all" posts {"image": "all"}', async () => {
    const { wrapper, api } = await openSystem({ 'POST /builds': { status: 202, body: { image: 'all', unit: 'u', started: true, message: 'ok' } } })
    await wrapper.find('[data-testid="images-rebuild-all"]').trigger('click')
    await flushPromises()
    const post = api.calls.filter(c => c.method === 'POST' && c.path === '/builds')
    expect(post.map(c => c.body)).toEqual([{ image: 'all' }])
    expect(post[0]!.headers['x-agentbox']).toBe('1')
  })

  it('shows the translated error of a Build that runs already, and reloads to show it', async () => {
    const { wrapper, api } = await openSystem({
      'POST /builds': { status: 409, body: { error: { code: 'build_running', message: 'A build is already running' } } },
    })
    await wrapper.find('[data-testid="images-rebuild-all"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="images-start-error"]').text()).toContain('A Build is running already.')
    // buttons work again afterwards (nothing was started by this click)
    expect(wrapper.find('[data-testid="images-rebuild-all"]').attributes('disabled')).toBeUndefined()
    expect(api.calls.filter(c => c.method === 'GET' && c.path === '/images').length).toBeGreaterThan(1)
  })

  it('disables every rebuild button while a Build runs, and enables them when it ends', async () => {
    const running = overview([
      { ...BASE, running: { build: '20261104-0300', step: 'build', started: '2026-11-04T03:00:00Z' }, builds: [build('20261104-0300', { status: 'running', step: 'build', finished: null }), ...BASE.builds] },
      ML,
    ])
    const { wrapper, es } = await openSystem({ 'GET /images': ok(running) })
    const disabled = () => [
      wrapper.find('[data-testid="images-rebuild-all"]').attributes('disabled'),
      ...wrapper.findAll('[data-testid="image-rebuild"]').map(b => b.attributes('disabled')),
    ]
    expect(disabled().every(d => d !== undefined)).toBe(true)
    // the Build ends: the refetched overview no longer has it running
    ;(running as any).images[0].running = null
    es.emit('build', { ...build('20261104-0300', { status: 'ok' }), image: 'base', unit: 'u' })
    await flushPromises()
    expect(disabled().every(d => d === undefined)).toBe(true)
  })
})

describe('System page: live progress', () => {
  useFreshApp()

  it('follows a Build through its steps to the end and refetches the overview when it ends', async () => {
    let loads = 0
    const after = overview([
      { ...BASE, current: '20261104-0300', builds: [build('20261104-0300', { status: 'ok', current: true, used_by: ['work'] }), ...BASE.builds] },
      ML,
    ])
    const { wrapper, es, api } = await openSystem({
      'GET /images': () => { loads++; return ok(loads === 1 ? IMAGES : after) },
      'GET /images/base/log?build=20261104-0300&lines=500': ok({ image: 'base', build: '20261104-0300', status: 'running', step: 'build', error: null, problems: [], entries: [], cursor: null, truncated: false }),
    })
    const ev = (status: string, step: string | null, extra: Record<string, unknown> = {}) =>
      ({ ...build('20261104-0300', { status, step, finished: null, actor: 'timer' }), image: 'base', unit: 'agentbox-build@base.service', ...extra })

    expect(card(wrapper).find('[data-testid="build-progress"]').exists()).toBe(false)
    es.emit('build', ev('running', 'source'))
    await flushPromises()
    let progress = card(wrapper).find('[data-testid="build-progress"]')
    expect(progress.exists()).toBe(true)
    expect(progress.text()).toContain('Build 20261104-0300 is running')
    expect(progress.attributes('data-step')).toBe('source')
    expect(rows(wrapper)[0]!.attributes('data-status')).toBe('running')
    expect(rows(wrapper)[0]!.attributes('data-build')).toBe('20261104-0300')
    expect(rows(wrapper)).toHaveLength(5)
    // a running Build shows its log without being asked
    expect(api.calls.some(c => c.path.startsWith('/images/base/log?build=20261104-0300'))).toBe(true)
    expect(loads).toBe(1)

    for (const step of ['build', 'check', 'disk']) {
      es.emit('build', ev('running', step))
      await flushPromises()
      progress = card(wrapper).find('[data-testid="build-progress"]')
      expect(progress.attributes('data-step')).toBe(step)
      const steps = progress.findAll('li')
      const at = stepIndex(step)
      expect(steps.map(s => s.attributes('aria-current'))).toEqual(steps.map((_, i) => (i === at ? 'step' : undefined)))
      expect(steps.slice(0, at).every(s => s.classes().includes('text-success'))).toBe(true)
    }
    expect(progress.text()).toContain('Make disk')
    expect(loads).toBe(1)           // nothing refetched while it runs

    es.emit('build', ev('ok', 'done', { finished: '2026-11-04T03:30:00Z' }))
    await flushPromises()
    expect(card(wrapper).find('[data-testid="build-progress"]').exists()).toBe(false)
    expect(loads).toBe(2)
    expect(card(wrapper).find('[data-testid="image-current"]').text()).toBe('20261104-0300')
    expect(rows(wrapper)[0]!.attributes('data-status')).toBe('ok')
    expect(rows(wrapper)[0]!.text()).toContain('current')
  })

  it.each([['failed', 'The Build failed'], ['rejected', 'Rejected: a safeguard']])('shows a %s Build with its reasons', async (status, hint) => {
    // the backend knows the Build once it ended, so the refetch after the event keeps it
    const ended = { ...build('20261104-0300', { status, step: 'check', problems: ['sshd: root login allowed'], error: 'x' }) }
    const { wrapper, es } = await openSystem({
      'GET /images': () => ok(overview([BASE, { ...ML, builds: [ended, ...ML.builds] }])),
    })
    es.emit('build', {
      ...build('20261104-0300', { status: 'running', step: 'check', finished: null }), image: 'ml', unit: 'u',
    })
    await flushPromises()
    expect(card(wrapper, 'ml').find('[data-testid="build-progress"]').exists()).toBe(true)
    es.emit('build', {
      ...build('20261104-0300', { status, step: 'check', problems: ['sshd: root login allowed'], error: 'x' }), image: 'ml', unit: 'u',
    })
    await flushPromises()
    expect(card(wrapper, 'ml').find('[data-testid="build-progress"]').exists()).toBe(false)
    const row = rows(wrapper, 'ml')[0]!
    expect(row.attributes('data-status')).toBe(status)
    expect(row.find('[data-testid="build-reasons"]').text()).toContain(hint)
    expect(row.find('[data-testid="build-reasons"]').text()).toContain('sshd: root login allowed')
  })

  it('fetches the overview when a Build of an unknown Image starts', async () => {
    let loads = 0
    const { es } = await openSystem({ 'GET /images': () => { loads++; return ok(IMAGES) } })
    es.emit('build', { ...build('b1', { status: 'running' }), image: 'new', unit: 'u' })
    await flushPromises()
    expect(loads).toBe(2)
  })
})

describe('System page: Build log', () => {
  useFreshApp()

  const line = (n: number, message = `line ${n}`) => ({ time: '2026-11-03T12:00:0' + (n % 10) + 'Z', priority: 6, message, cursor: `c${n}` })
  const logOf = (image: string, build: string, entries: unknown[], cursor: string | null) =>
    ok({ image, build, status: 'ok', step: 'done', error: null, problems: [], entries, cursor, truncated: false })
  const lines = (wrapper: Page['wrapper']) => wrapper.findAll('[data-testid="build-log-line"]').map(l => l.text().replace(/^.*?\d{1,2}:\d{2}:\d{2}[^ ]*\s*(AM|PM)?\s*/, ''))

  const RUNNING = overview([
    { ...ML, running: { build: 'r1', step: 'build', started: '2026-11-04T03:00:00Z' }, builds: [build('r1', { status: 'running', step: 'build', finished: null }), ...ML.builds] },
  ])
  const LOG_R1 = 'GET /images/ml/log?build=r1&lines=500'
  const rEvent = (entries: unknown[], extra: Record<string, unknown> = {}) =>
    ({ unit: 'u', target: 'ml', image: 'ml', build: 'r1', entries, cursor: (entries.at(-1) as any)?.cursor ?? 'c0', truncated: false, ...extra })

  it('opens the log of an older Build by fetching it, and hides it again', async () => {
    const { wrapper, api } = await openSystem({
      'GET /images/base/log?build=20261102-0300&lines=500': logOf('base', '20261102-0300', [line(1, 'dnf install foo'), line(2, 'error: no such package')], 'c2'),
      'GET /images/base/log?build=20261102-0300&lines=500&after_cursor=c2': logOf('base', '20261102-0300', [], 'c2'),
    })
    expect(wrapper.find('[data-testid="build-log"]').exists()).toBe(false)
    const toggle = rows(wrapper)[1]!.find('[data-testid="build-log-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')
    await flushPromises()
    expect(api.calls.filter(c => c.path.startsWith('/images/base/log'))).toHaveLength(1)
    expect(lines(wrapper)).toEqual(['dnf install foo', 'error: no such package'])
    expect(rows(wrapper)[1]!.find('[data-testid="build-log-toggle"]').attributes('aria-expanded')).toBe('true')
    await rows(wrapper)[1]!.find('[data-testid="build-log-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="build-log"]').exists()).toBe(false)
    // opening it again asks only for what is newer than the held cursor
    await rows(wrapper)[1]!.find('[data-testid="build-log-toggle"]').trigger('click')
    await flushPromises()
    expect(api.calls.at(-1)!.path).toBe('/images/base/log?build=20261102-0300&lines=500&after_cursor=c2')
    expect(lines(wrapper)).toEqual(['dnf install foo', 'error: no such package'])
  })

  it('shows an error of the log endpoint translated', async () => {
    const { wrapper } = await openSystem({
      'GET /images/base/log?build=20261102-0300&lines=500': { status: 404, body: { error: { code: 'not_found', message: 'Not found' } } },
    })
    await rows(wrapper)[1]!.find('[data-testid="build-log-toggle"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="build-log"]').text()).not.toBe('')
    expect(wrapper.find('[data-testid="build-log-lines"]').exists()).toBe(false)
  })

  it('says so when a log has no lines', async () => {
    const { wrapper } = await openSystem({ 'GET /images/base/log?build=20261102-0300&lines=500': logOf('base', '20261102-0300', [], null) })
    await rows(wrapper)[1]!.find('[data-testid="build-log-toggle"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="build-log"]').text()).toBe('The log has no lines.')
  })

  it('shows a running Build\'s log at once and appends build-log events, dropping repeats by cursor', async () => {
    const { wrapper, es, api } = await openSystem({
      'GET /images': ok(RUNNING),
      [LOG_R1]: logOf('ml', 'r1', [line(1), line(2)], 'c2'),
    })
    expect(api.calls.filter(c => c.path.startsWith('/images/ml/log'))).toHaveLength(1)
    expect(lines(wrapper)).toEqual(['line 1', 'line 2'])
    es.emit('build-log', rEvent([line(3), line(4)]))
    await flushPromises()
    expect(lines(wrapper)).toEqual(['line 1', 'line 2', 'line 3', 'line 4'])
    // the same lines again (a resumed stream): nothing doubles
    es.emit('build-log', rEvent([line(3), line(4), line(5)]))
    await flushPromises()
    expect(lines(wrapper)).toEqual(['line 1', 'line 2', 'line 3', 'line 4', 'line 5'])
    expect(api.calls.filter(c => c.path.startsWith('/images/ml/log'))).toHaveLength(1)
  })

  it('shows waiting text before the first lines of a running Build', async () => {
    const { wrapper } = await openSystem({ 'GET /images': ok(RUNNING), [LOG_R1]: logOf('ml', 'r1', [], null) })
    expect(wrapper.find('[data-testid="build-log"]').text()).toBe('Waiting for the first lines…')
  })

  it('fills the gap of a truncated event from the log endpoint, after the held cursor', async () => {
    const { wrapper, es, api } = await openSystem({
      'GET /images': ok(RUNNING),
      [LOG_R1]: logOf('ml', 'r1', [line(1), line(2)], 'c2'),
      [`${LOG_R1}&after_cursor=c2`]: logOf('ml', 'r1', [line(3), line(4), line(5), line(6)], 'c6'),
    })
    es.emit('build-log', rEvent([line(6)], { truncated: true }))
    await flushPromises()
    const fetched = api.calls.filter(c => c.path.startsWith('/images/ml/log')).map(c => c.path)
    expect(fetched).toEqual([`/images/ml/log?build=r1&lines=500`, `/images/ml/log?build=r1&lines=500&after_cursor=c2`])
    expect(lines(wrapper)).toEqual(['line 1', 'line 2', 'line 3', 'line 4', 'line 5', 'line 6'])
    // the next event continues after the new cursor
    es.emit('build-log', rEvent([line(7)]))
    await flushPromises()
    expect(lines(wrapper).at(-1)).toBe('line 7')
    expect(lines(wrapper)).toHaveLength(7)
  })

  it('keeps lines that arrive while the log is being fetched, after the fetched ones', async () => {
    const { wrapper, es, api } = await openSystem({ 'POST /builds': ok({}) })
    // the first fetch of the log is held back by the "network"
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => { release = resolve })
    const answer = logOf('ml', 'r1', [line(1), line(2)], 'c2')
    const fake = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      if (!String(input).includes('/log?')) return fake(input, init)
      api.calls.push({ method: 'GET', path: String(input).replace(/^\/api\/v1/, ''), headers: {}, body: undefined })
      await gate
      return new Response(JSON.stringify(answer.body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    es.emit('build', { ...build('r1', { status: 'running', step: 'build', finished: null }), image: 'ml', unit: 'u' })
    await flushPromises()
    es.emit('build-log', rEvent([line(2), line(3)]))
    es.emit('build-log', rEvent([line(4)]))
    release()
    await flushPromises()
    expect(lines(wrapper)).toEqual(['line 1', 'line 2', 'line 3', 'line 4'])
    // the next fetch continues after the newest line
    const logCalls = api.calls.filter(c => c.path.startsWith('/images/ml/log')).map(c => c.path)
    expect(logCalls).toEqual(['/images/ml/log?build=r1&lines=500'])
    es.emit('build', { ...build('r1', { status: 'ok' }), image: 'ml', unit: 'u' })
    await flushPromises()
    expect(api.calls.filter(c => c.path.startsWith('/images/ml/log')).at(-1)!.path).toBe('/images/ml/log?build=r1&lines=500&after_cursor=c4')
  })

  it('fetches again after a truncated event that arrived while the log was being fetched', async () => {
    const { wrapper, es, api } = await openSystem()
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => { release = resolve })
    const answers = [logOf('ml', 'r1', [line(1)], 'c1'), logOf('ml', 'r1', [line(2), line(3)], 'c3')]
    const fake = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      if (!String(input).includes('/log?')) return fake(input, init)
      api.calls.push({ method: 'GET', path: String(input).replace(/^\/api\/v1/, ''), headers: {}, body: undefined })
      await gate
      return new Response(JSON.stringify(answers.shift()!.body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    es.emit('build', { ...build('r1', { status: 'running', step: 'build', finished: null }), image: 'ml', unit: 'u' })
    await flushPromises()
    es.emit('build-log', rEvent([line(3)], { truncated: true }))
    release()
    await flushPromises()
    expect(api.calls.filter(c => c.path.startsWith('/images/ml/log')).map(c => c.path)).toEqual([
      '/images/ml/log?build=r1&lines=500',
      '/images/ml/log?build=r1&lines=500&after_cursor=c1',     // not c3: line 2 is missing
    ])
    await flushPromises()
    expect(wrapper.findAll('[data-testid="build-log-line"]')).toHaveLength(3)
  })

  it('keeps the lines it has when the log cannot be updated', async () => {
    const { wrapper, es } = await openSystem({
      'GET /images': ok(RUNNING),
      [LOG_R1]: logOf('ml', 'r1', [line(1)], 'c1'),
    })
    es.emit('build', { ...build('r1', { status: 'ok' }), image: 'ml', unit: 'u' })     // refetch: 404 for after_cursor
    await flushPromises()
    expect(lines(wrapper)).toEqual(['line 1'])
    expect(wrapper.find('[data-testid="build-log-error"]').exists()).toBe(true)
  })

  it('refetches the log of an open Build once it ended, so the end of it is there', async () => {
    const { wrapper, es, api } = await openSystem({
      'GET /images': ok(RUNNING),
      [LOG_R1]: logOf('ml', 'r1', [line(1)], 'c1'),
      [`${LOG_R1}&after_cursor=c1`]: logOf('ml', 'r1', [line(2, 'done')], 'c2'),
    })
    es.emit('build', { ...build('r1', { status: 'ok' }), image: 'ml', unit: 'u' })
    await flushPromises()
    expect(api.calls.some(c => c.path === `/images/ml/log?build=r1&lines=500&after_cursor=c1`)).toBe(true)
    expect(lines(wrapper)).toEqual(['line 1', 'done'])
  })

  it('fetches the overview and the open logs again when the stream could not resume', async () => {
    let loads = 0
    const { es, api } = await openSystem({
      'GET /images': () => { loads++; return ok(RUNNING) },
      [LOG_R1]: logOf('ml', 'r1', [line(1)], 'c1'),
      [`${LOG_R1}&after_cursor=c1`]: logOf('ml', 'r1', [line(2)], 'c2'),
    })
    expect(loads).toBe(1)
    es.emit('hello', { stream: 'other', resumed: false })
    await flushPromises()
    expect(loads).toBe(2)
    expect(api.calls.some(c => c.path === `/images/ml/log?build=r1&lines=500&after_cursor=c1`)).toBe(true)
  })

  it('ignores log events of a Build whose log is not open', async () => {
    const { wrapper, es, api } = await openSystem()
    es.emit('build-log', { unit: 'u', target: 'base', image: 'base', build: '20261101-0300', entries: [line(1)], cursor: 'c1', truncated: false })
    await flushPromises()
    expect(wrapper.find('[data-testid="build-log"]').exists()).toBe(false)
    expect(api.calls.filter(c => c.path.includes('/log'))).toHaveLength(0)
  })
})

describe('System page: services', () => {
  useFreshApp()

  const unit = (name: string, active: string, over: Record<string, unknown> = {}) =>
    ({ unit: name, load: 'loaded', active, sub: active === 'active' ? 'running' : active, description: `${name} description`, ...over })

  it('says so until the first services event', async () => {
    const { wrapper } = await openSystem()
    expect(wrapper.find('[data-testid="services-unknown"]').text()).toContain('not known yet')
    expect(wrapper.findAll('[data-testid="service-row"]')).toHaveLength(0)
  })

  it('lists a failed unit first and highlighted, with a count', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('services', {
      units: [
        unit('agentbox-allowlist.service', 'active'),
        unit('ollama.service', 'active'),
        unit('agentbox-web.service', 'failed', { sub: 'failed' }),
        unit('agentbox-reset.timer', 'inactive', { sub: 'dead' }),
        unit('agentbox-gone.service', 'inactive', { load: 'not-found', sub: 'dead' }),
      ],
    })
    await flushPromises()
    const r = wrapper.findAll('[data-testid="service-row"]')
    expect(r.map(x => x.attributes('data-unit'))).toEqual([
      'agentbox-gone.service', 'agentbox-web.service', 'agentbox-allowlist.service', 'agentbox-reset.timer', 'ollama.service',
    ])
    expect(r.map(x => x.attributes('data-color'))).toEqual(['error', 'error', 'success', 'neutral', 'success'])
    expect(r[1]!.text()).toContain('failed')
    expect(r[2]!.text()).toContain('active (running)')
    expect(wrapper.find('[data-testid="services-failed"]').text()).toBe('2 not running')
    expect(wrapper.find('[data-testid="services-unknown"]').exists()).toBe(false)
  })

  it('shows no failure badge when every unit runs, and updates on the next event', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('services', { units: [unit('a.service', 'failed')] })
    await flushPromises()
    expect(wrapper.find('[data-testid="services-failed"]').exists()).toBe(true)
    es.emit('services', { units: [unit('a.service', 'active')] })
    await flushPromises()
    expect(wrapper.find('[data-testid="services-failed"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="service-row"]').attributes('data-color')).toBe('success')
  })

  it('warns while the timer of the Maintenance window is missing, in the user\'s language', async () => {
    const { wrapper, es } = await openSystem()
    const message = 'The timer of the Maintenance window (agentbox-maintenance.timer) is missing: sudo agentbox regenerate creates it'
    es.emit('services', { units: [unit('agentbox-weekly.timer', 'active')], warnings: [{ code: 'maintenance_timer_missing', message }] })
    await flushPromises()
    const warning = wrapper.find('[data-testid="services-warning"]')
    expect(warning.attributes('data-code')).toBe('maintenance_timer_missing')
    expect(warning.text()).toContain('The timer of the Maintenance window is missing')
    expect(warning.text()).toContain('sudo agentbox regenerate')
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    expect(wrapper.find('[data-testid="services-warning"]').text()).toContain('Der Timer des Wartungsfensters fehlt')
    // one this UI does not know yet: the server's English; none once regenerate created the timer
    es.emit('services', { units: [], warnings: [{ code: 'something_new', message: 'Something is missing' }] })
    await flushPromises()
    expect(wrapper.find('[data-testid="services-warning"]').text()).toContain('Something is missing')
    es.emit('services', { units: [unit('agentbox-maintenance.timer', 'active')], warnings: [] })
    await flushPromises()
    expect(wrapper.find('[data-testid="services-warning"]').exists()).toBe(false)
  })

  it('treats a malformed services event as not known', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('services', { units: 'nope' })
    await flushPromises()
    expect(wrapper.find('[data-testid="services-unknown"]').exists()).toBe(true)
  })

  it('shows a service under the timer or socket that starts it, waiting while it is inactive', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('services', {
      units: [
        unit('agentbox-feeds.service', 'inactive', { sub: 'dead', triggered_by: ['agentbox-feeds.timer'] }),
        unit('agentbox-feeds.timer', 'active', { sub: 'waiting', triggered_by: [] }),
        unit('agentbox-proxy-priv.service', 'active', { triggered_by: ['agentbox-proxy-priv.socket'] }),
        unit('agentbox-proxy-priv.socket', 'active', { triggered_by: [] }),
        unit('agentbox-proxy-work.service', 'inactive', { sub: 'dead', triggered_by: ['agentbox-proxy-work.socket'] }),
        unit('agentbox-proxy-work.socket', 'active', { sub: 'listening', triggered_by: [] }),
        unit('agentbox-maintenance.service', 'failed', { sub: 'failed', triggered_by: ['agentbox-maintenance.timer'] }),
        unit('agentbox-maintenance.timer', 'active', { sub: 'waiting', triggered_by: [] }),
        unit('agentbox-versions-fetch.service', 'inactive', { sub: 'dead', triggered_by: null }),
        unit('agentbox-web.service', 'active', { triggered_by: [] }),
      ],
    })
    await flushPromises()
    const r = wrapper.findAll('[data-testid="service-row"]')
    expect(r.map(x => [x.attributes('data-unit'), x.attributes('data-trigger')])).toEqual([
      // the group with the failed service first
      ['agentbox-maintenance.timer', undefined],
      ['agentbox-maintenance.service', 'agentbox-maintenance.timer'],
      ['agentbox-feeds.timer', undefined],
      ['agentbox-feeds.service', 'agentbox-feeds.timer'],
      ['agentbox-proxy-priv.socket', undefined],
      ['agentbox-proxy-priv.service', 'agentbox-proxy-priv.socket'],
      ['agentbox-proxy-work.socket', undefined],
      ['agentbox-proxy-work.service', 'agentbox-proxy-work.socket'],
      ['agentbox-versions-fetch.service', undefined],
      ['agentbox-web.service', undefined],
    ])
    expect(wrapper.findAll('[data-testid="service-group"]')).toHaveLength(6)
    const row = (name: string) => r.find(x => x.attributes('data-unit') === name)!
    expect(row('agentbox-feeds.service').text()).toContain('waits for its timer')
    expect(row('agentbox-feeds.service').text()).not.toContain('inactive')
    expect(row('agentbox-feeds.service').attributes('data-color')).toBe('neutral')
    expect(row('agentbox-proxy-work.service').text()).toContain('starts on connection')
    expect(row('agentbox-proxy-priv.service').text()).toContain('active (running)')
    expect(row('agentbox-proxy-priv.service').attributes('data-color')).toBe('success')
    // failed stays failed, red and counted
    expect(row('agentbox-maintenance.service').text()).toContain('failed')
    expect(row('agentbox-maintenance.service').text()).not.toContain('waits')
    expect(row('agentbox-maintenance.service').attributes('data-color')).toBe('error')
    expect(wrapper.find('[data-testid="services-failed"]').text()).toBe('1 not running')
    // no trigger known: systemd's state as before
    expect(row('agentbox-versions-fetch.service').text()).toContain('inactive (dead)')
  })

  it('names the wait in German, also when the trigger is not listed', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('services', {
      units: [
        unit('agentbox-feeds.service', 'inactive', { sub: 'dead', triggered_by: ['agentbox-feeds.timer'] }),
        unit('agentbox-feeds.timer', 'active', { sub: 'waiting', triggered_by: [] }),
        unit('agentbox-proxy-work.service', 'inactive', { sub: 'dead', triggered_by: ['agentbox-proxy-work.socket'] }),
      ],
    })
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    const row = (name: string) => wrapper.find(`[data-testid="service-row"][data-unit="${name}"]`)
    expect(row('agentbox-feeds.service').text()).toContain('wartet auf Timer')
    expect(row('agentbox-feeds.service').attributes('data-trigger')).toBe('agentbox-feeds.timer')
    expect(row('agentbox-proxy-work.service').text()).toContain('startet bei Verbindung')
    expect(row('agentbox-proxy-work.service').attributes('data-trigger')).toBeUndefined()
  })
})

describe('System page: Host reboot', () => {
  useFreshApp()

  const section = (wrapper: Page['wrapper']) => wrapper.find('[data-testid="system-reboot"]')

  it('says it is not known before the first event', async () => {
    const { wrapper } = await openSystem()
    expect(section(wrapper).attributes('data-needed')).toBe('unknown')
    expect(section(wrapper).text()).toContain('not known yet')
  })

  it('reminds of the disk passphrase and lists the packages when a reboot is needed', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('host-reboot', { needed: true, packages: ['kernel-core', 'glibc'], checked: '2026-11-03T06:00:00Z' })
    await flushPromises()
    const s = section(wrapper)
    expect(s.attributes('data-needed')).toBe('true')
    expect(s.text()).toContain('Updates need a Host reboot.')
    expect(s.text()).toContain('disk passphrase')
    expect(s.find('[data-testid="reboot-packages"]').text()).toBe('Updated: kernel-core, glibc')
  })

  it('shortens a long package list', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('host-reboot', { needed: true, packages: Array.from({ length: 15 }, (_, i) => `pkg${i}`), checked: '2026-11-03T06:00:00Z' })
    await flushPromises()
    const text = section(wrapper).find('[data-testid="reboot-packages"]').text()
    expect(text).toContain('pkg11')
    expect(text).not.toContain('pkg12')
    expect(text).toContain('… and 3 more')
  })

  it('needs no reminder when no reboot is needed', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('host-reboot', { needed: false, packages: [], checked: '2026-11-03T06:00:00Z' })
    await flushPromises()
    const s = section(wrapper)
    expect(s.attributes('data-needed')).toBe('false')
    expect(s.text()).toContain('No Host reboot needed.')
    expect(s.text()).toContain('checked')
    expect(s.text()).not.toContain('passphrase')
  })

  it('says it is not known when the check could not tell (needed null)', async () => {
    const { wrapper, es } = await openSystem()
    es.emit('host-reboot', { needed: null, packages: [], checked: null })
    await flushPromises()
    expect(section(wrapper).attributes('data-needed')).toBe('unknown')
    expect(section(wrapper).text()).toContain('not known yet')
    expect(section(wrapper).text()).not.toContain('passphrase')
  })
})

describe('System page: structure and language', () => {
  useFreshApp()

  it('shows the GitHub App section between Images and services', async () => {
    const { wrapper } = await openSystem()
    const order = wrapper.findAll('[data-testid="system-page"] > *').map(el => el.attributes('data-testid'))
    const images = order.indexOf('system-images')
    expect(images).toBeGreaterThanOrEqual(0)
    expect(order[images + 1]).toBe('github-app')
    expect(order[images + 2]).toBe('system-services')
  })

  it('uses only bundled lucide icons and no inline style attribute', async () => {
    const sources = import.meta.glob('../app/components/system/*.vue', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
    const lucide = (await import('@iconify-json/lucide/icons.json')).default as { icons: Record<string, unknown>, aliases?: Record<string, unknown> }
    expect(Object.keys(sources).length).toBeGreaterThanOrEqual(5)
    for (const [file, source] of Object.entries(sources)) {
      for (const [, name] of source.matchAll(/i-lucide-([a-z0-9-]+)/g)) {
        expect(name! in lucide.icons || name! in (lucide.aliases ?? {}), `${file}: ${name}`).toBe(true)
      }
      expect(source, file).not.toMatch(/\sstyle="/)
      expect(source, file).not.toMatch(/<style|<script src|https?:\/\//)
    }
  })

  it('shows its texts in German', async () => {
    const { wrapper, es } = await openSystem({ 'POST /builds': { status: 409, body: { error: { code: 'build_running', message: 'A build is already running' } } } })
    es.emit('host-reboot', { needed: true, packages: ['kernel-core'], checked: '2026-11-03T06:00:00Z' })
    es.emit('services', { units: [{ unit: 'a.service', load: 'loaded', active: 'failed', sub: 'failed', description: 'a' }] })
    await useNuxtApp().$i18n.setLocale('de')
    await flushPromises()
    const text = wrapper.find('[data-testid="system-page"]').text()
    expect(text).toContain('Images und Builds')
    expect(text).toContain('Base-Image')
    expect(text).toContain('Verworfen')
    expect(text).toContain('Gescheitert')
    expect(text).toContain('Neustart')
    expect(text).toContain('Passphrase')
    expect(text).toContain('1 laufen nicht')
    expect(text).toContain('Neu bauen')
    expect(text).not.toContain('Images and Builds')
    expect(text).not.toContain('Rejected')
    expect(text).not.toContain('Used by')
    expect(card(wrapper).text()).toContain('Genutzt von: work, priv')
    expect(rows(wrapper)[2]!.find('[data-testid="build-users"]').text()).toBe('Systemplatte von work · festgelegt für priv')
    await wrapper.find('[data-testid="images-rebuild-all"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="images-start-error"]').text()).toContain('Es läuft schon ein Build.')
  })
})
