// Images and Builds (#17, #96): GET /images, `build` and `build-log` events; the versions of
// the tools in a Build and what is available (#104).

/** The tools whose versions a Build records, in the order they are shown. */
export const TOOLS = ['orca', 'claude-code', 'codex', 'opencode', 'playwright', 'fedora'] as const
export type Tool = typeof TOOLS[number]
/** Product names, the same in every language. */
export const TOOL_NAMES: Record<Tool, string> = {
  'orca': 'Orca', 'claude-code': 'Claude Code', 'codex': 'codex', 'opencode': 'opencode', 'playwright': 'Playwright',
  'fedora': 'Fedora',
}
/** {tool: version}; null: unknown (not recorded, not checked, or the check failed) */
export type ToolVersions = Partial<Record<Tool, string | null>>

/** The two images a Build takes from quay.io, only by the digest the version check pinned
 * (#144): fedora-bootc:44, what the Base image starts from and the disk step's buildroot, and
 * bootc-image-builder, the disk step. */
export const DIGEST_IMAGES = ['fedora-bootc', 'bootc-image-builder'] as const
export type DigestImage = typeof DIGEST_IMAGES[number]
/** A pinned digest (`available.digests` of GET /images). */
export interface PinnedDigest {
  digest: string
  /** when the tag moved to it */
  since: string | null
  /** when the version check pinned it */
  pinned: string | null
  /** the image by its tag, e.g. quay.io/fedora/fedora-bootc:44 */
  image: string
}

/** What the daily version check found (`available` of GET /images). */
export interface Available {
  /** when it was checked; null: not yet */
  checked: string | null
  package_min_age_days: number | null
  /** what a new Build would bring: the npm tools from the registry, Orca from its GitHub
   * release (#113), each the newest at least package_min_age_days old or the pinned one */
  tools: ToolVersions
  /** the Orca version pinned in the Settings (orca_version); null: the newest old enough */
  orca_version?: string | null
  /** the Playwright version pinned in the Settings (playwright_version); null: the newest old enough */
  playwright_version?: string | null
  /** the digests the next Build uses; null: none pinned yet */
  digests?: Partial<Record<DigestImage, PinnedDigest | null>>
}

export type BuildStatus = 'running' | 'ok' | 'failed' | 'rejected' | 'migrated' | 'missing' | string

export interface Build {
  build: string
  status: BuildStatus
  step: string | null
  started: string | null
  finished: string | null
  /** the Release the Build came from; null for Builds from before ADR 0010 */
  release?: string | null
  /** the commit of main a Build from before ADR 0010 came from; null for every newer one */
  commit: string | null
  base_build: string | null
  /** the SHA-256 of the Recipe tar the Build came from; null for an Image of the Release */
  recipe_sha256?: string | null
  package_min_age_days?: number | null
  /** the Orca version pinned when the Build was made; null: the newest old enough */
  orca_version?: string | null
  /** the Playwright version pinned when the Build was made; null: the newest old enough */
  playwright_version?: string | null
  /** the digest of fedora-bootc the Build is built on (a derived Image: its Base Build's) */
  fedora_bootc_digest?: string | null
  /** the digest of bootc-image-builder that made its disk */
  builder_digest?: string | null
  agents?: Record<string, string | null> | null
  /** versions of the tools in this Build */
  tools?: ToolVersions
  /** the tools a new Build would change (compared with `available`) */
  updates?: string[]
  error: string | null
  problems: string[]
  actor: string | null
  current?: boolean
  file?: string | null
  size?: number | null
  /** VMs whose System disk was made from this Build; null: not known */
  used_by?: string[] | null
  pinned_by?: string[]
  unit?: string | null
}

export interface Image {
  name: string
  base: boolean
  /** the installed Release defines this Image; null: its files are not there */
  in_release: boolean | null
  /** a Recipe on the Host defines this Image (ADR 0018); in_release is false then */
  recipe?: boolean
  current: string | null
  vms: string[]
  running: { build: string, step: string | null, started: string | null } | null
  builds: Build[]
  /** Fedora package updates available for the current Build; null: not counted */
  package_updates?: number | null
  /** what a new Build would bring: tools and "packages" */
  updates?: string[]
  /** a new Build would bring something; null: nothing to tell it by */
  update?: boolean | null
  /** the Containerfile that defines the Image: its path in the repository (#67) */
  containerfile?: string
  /** that file on GitHub at the Release tag or commit the current Build came from; null: not known */
  containerfile_url?: string | null
}

export interface ImagesOverview {
  images: Image[]
  builds_kept: number
  /** the installed Release, which the next Build takes */
  release: string | null
  /** the repository the Releases come from (owner/name) and its page on GitHub */
  repository?: string | null
  repository_url?: string | null
  disks_readable: boolean
  available?: Available
  /** an update of every Image runs (Update now, or the automatic one after the daily check,
   * #114): its unit may still be checking the versions, before any of its Builds shows */
  updating?: boolean
}

/** An Image's Containerfile in the installed Release (GET /images/<image>/containerfile, #67):
 * what its next Build takes, as text, read on the Host (never fetched) and cut at 64 KiB. */
export interface ContainerfileText {
  image: string
  /** its path in the repository and in the Release */
  path: string
  /** the installed Release; null: its VERSION cannot be read */
  release: string | null
  /** the file on GitHub at the installed Release's tag; null for a checkout (0.0.0) */
  url: string | null
  /** the SHA-256 of the Recipe's tar, for an Image a Recipe defines; null otherwise */
  recipe_sha256?: string | null
  text: string
  /** bytes of the whole file */
  size: number
  /** only its beginning is in `text` */
  truncated: boolean
}

/** A link to GitHub from the API, or null: only pages on https://github.com/ become links. */
export function githubLink(url: unknown): string | null {
  return typeof url === 'string' && /^https:\/\/github\.com\/\S+$/.test(url) ? url : null
}

export interface ToolRow {
  tool: Tool
  name: string
  installed: string | null
  available: string | null
  /** a new Build would change it */
  update: boolean
}

/** The tools of a Build next to what is available, one row each. Fedora has no available
 * version: a new release comes with a new Containerfile, in a Release of Agent Box. */
export function toolRows(build: Pick<Build, 'tools' | 'updates'> | null | undefined, available: Available | null | undefined): ToolRow[] {
  return TOOLS.map(tool => ({
    tool,
    name: TOOL_NAMES[tool],
    installed: build?.tools?.[tool] ?? null,
    available: tool === 'fedora' ? null : (available?.tools?.[tool] ?? null),
    update: !!build?.updates?.includes(tool),
  }))
}

export interface DigestRow {
  image: DigestImage
  /** sha256: and the first 12 hex digits; null: unknown */
  installed: string | null
  available: string | null
  /** a new Build would change it (only fedora-bootc counts as an update) */
  update: boolean
}

/** sha256: and the first 12 hex digits of a digest, null for anything else. */
export function shortDigest(digest: unknown): string | null {
  return typeof digest === 'string' && /^sha256:[0-9a-f]{64}$/.test(digest) ? digest.slice(0, 19) : null
}

/** The digests a Build was made with next to the pinned ones a new Build would use. */
export function digestRows(build: Pick<Build, 'fedora_bootc_digest' | 'builder_digest' | 'updates'> | null | undefined,
  available: Available | null | undefined): DigestRow[] {
  return DIGEST_IMAGES.map(image => ({
    image,
    installed: shortDigest(image === 'fedora-bootc' ? build?.fedora_bootc_digest : build?.builder_digest),
    available: shortDigest(available?.digests?.[image]?.digest),
    update: image === 'fedora-bootc' && !!build?.updates?.includes('fedora-bootc'),
  }))
}

/** The `build` event. */
export interface BuildEvent extends Build {
  image: string
}

export interface LogLine { time: string, priority?: number | string | null, message: string | null, cursor: string }

export interface BuildLogEvent {
  unit: string
  target: string
  image: string | null
  build: string | null
  entries: LogLine[]
  cursor: string
  truncated: boolean
}

export interface BuildLog {
  lines: LogLine[]
  cursor: string | null
  status?: BuildStatus
  step?: string | null
  error?: string | null
  problems?: string[]
}

/** The steps of a Build in order (the `step` field of a running Build). */
export const BUILD_STEPS = ['source', 'build', 'check', 'disk', 'done'] as const

/** lines of a Build log kept in the page (the Log tab's journal limit is vmRules' MAX_LOG_LINES) */
export const MAX_BUILD_LOG_LINES = 2000

export const buildRunning = (b: { status: BuildStatus }) => b.status === 'running'
export const buildBad = (b: { status: BuildStatus }) => b.status === 'failed' || b.status === 'rejected'

export function stepIndex(step: string | null | undefined): number {
  return step ? (BUILD_STEPS as readonly string[]).indexOf(step) : -1
}

export function logKey(image: string, build: string) {
  return `${image}/${build}`
}

/** A Build event applied to the overview: the Build's record is replaced (or added, newest
 * first), and the Image's running Build follows it. Returns null when the Image is unknown
 * (the caller fetches the overview again). */
export function applyBuild(overview: ImagesOverview, event: BuildEvent): ImagesOverview | null {
  const at = overview.images.findIndex(i => i.name === event.image)
  if (at < 0) return null
  const image = overview.images[at]!
  const old = image.builds.find(b => b.build === event.build)
  const merged: Build = {
    ...(old ?? { used_by: [], pinned_by: [], current: false, file: null }),
    build: event.build,
    status: event.status,
    step: event.step ?? null,
    started: event.started ?? null,
    finished: event.finished ?? null,
    release: event.release ?? null,
    commit: event.commit ?? null,
    base_build: event.base_build ?? null,
    error: event.error ?? null,
    problems: event.problems ?? [],
    actor: event.actor ?? null,
    unit: event.unit ?? null,
  }
  const builds = old
    ? image.builds.map(b => (b.build === event.build ? merged : b))
    : [merged, ...image.builds].sort((a, b) => b.build.localeCompare(a.build))
  const running = event.status === 'running'
    ? { build: event.build, step: event.step ?? null, started: event.started ?? null }
    : image.running?.build === event.build ? null : image.running
  const next = { ...image, builds, running }
  return { ...overview, images: overview.images.map((i, n) => (n === at ? next : i)) }
}

/** New lines after those held, by cursor (a repeated line is dropped); the newest stay. */
export function appendLines(held: LogLine[], incoming: LogLine[]): LogLine[] {
  const seen = new Set(held.map(l => l.cursor))
  const fresh = incoming.filter(l => !seen.has(l.cursor))
  if (!fresh.length) return held
  return [...held, ...fresh].slice(-MAX_BUILD_LOG_LINES)
}
