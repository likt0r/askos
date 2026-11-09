// Host readings (GET /api/v1/host and the `host` event): what is shown, in which order,
// and what counts as over its threshold. The backend decides `over`; the UI only shows it.

export interface HostReading {
  id: string
  kind: string
  device: string | null
  source?: string
  unit: string
  value: number | null
  threshold: number | null
  over: boolean
  unavailable?: string
  used_bytes?: number
  total_bytes?: number
  loadavg?: number[] | null
  cpus?: number
  [key: string]: unknown
}

export interface HostSample {
  time: number | null
  readings: HostReading[]
  vms: Record<string, { memory_bytes: number }>
}

export interface HostHistoryPoint {
  time: number
  values: Record<string, number | null>
  vms: Record<string, number>
}

export interface HostSnapshot extends HostSample {
  interval: number
  history_seconds: number
  thresholds: Record<string, number | null>
  history: HostHistoryPoint[]
}

/** Order of the tiles; unknown kinds (a newer backend) come last. */
export const HOST_KIND_ORDER = [
  'cpu_load', 'cpu_temp', 'cpu_power', 'memory', 'swap', 'swap_activity',
  'gpu_temp', 'gpu_load', 'gpu_memory', 'gpu_power', 'nvme_temp',
]

/** What the collapsed bar always shows: the readings that say whether the Host is under strain. */
export const HOST_KEY_KINDS = ['cpu_load', 'cpu_temp', 'gpu_temp', 'memory']

export function sortReadings(readings: HostReading[]): HostReading[] {
  const rank = (r: HostReading) => {
    const i = HOST_KIND_ORDER.indexOf(r.kind)
    return i < 0 ? HOST_KIND_ORDER.length : i
  }
  return [...readings].sort((a, b) => rank(a) - rank(b) || a.id.localeCompare(b.id, undefined, { numeric: true }))
}

/** Readings of the collapsed bar: the key ones, and any other reading over its threshold
 * (an NVMe drive, GPU memory), so that overheating is never hidden by collapsing. */
export function barReadings(readings: HostReading[]): HostReading[] {
  const key = sortReadings(readings).filter(r => HOST_KEY_KINDS.includes(r.kind) && r.value !== null)
  // a GPU's temperature only once: the first GPU's reading; further GPUs show when over
  const seen = new Set<string>()
  const shown = key.filter((r) => {
    if (seen.has(r.kind) && !r.over) return false
    seen.add(r.kind)
    return true
  })
  const extra = sortReadings(readings).filter(r => r.over && !shown.includes(r))
  return sortReadings([...shown, ...extra])
}

export function overReadings(readings: HostReading[]): HostReading[] {
  return sortReadings(readings.filter(r => r.over))
}

export function formatReading(value: number | null, unit: string): string {
  if (value === null || value === undefined) return '—'
  const n = Math.abs(value) >= 100 ? Math.round(value) : Math.round(value * 10) / 10
  return unit === '%' || unit === '°C' ? `${n}${unit === '%' ? ' %' : ' °C'}` : `${n} ${unit}`
}

export function formatHostBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—'
  const gib = bytes / 1024 ** 3
  if (gib >= 1) return `${gib >= 100 ? Math.round(gib) : Math.round(gib * 10) / 10} GiB`
  return `${Math.round(bytes / 1024 ** 2)} MiB`
}

/** Memory of each VM's QEMU process, largest first, and the sum. */
export function vmMemory(vms: HostSample['vms'] | null | undefined) {
  const rows = Object.entries(vms ?? {})
    .map(([name, v]) => ({ name, bytes: v?.memory_bytes ?? 0 }))
    .sort((a, b) => b.bytes - a.bytes || a.name.localeCompare(b.name))
  return { rows, total: rows.reduce((n, r) => n + r.bytes, 0) }
}

/** The history of one reading as numbers (gaps skipped), oldest first. */
export function hostSeries(history: HostHistoryPoint[], id: string): { time: number, value: number }[] {
  const out: { time: number, value: number }[] = []
  for (const point of history) {
    const v = point.values?.[id]
    if (typeof v === 'number' && Number.isFinite(v)) out.push({ time: point.time, value: v })
  }
  return out
}

/** The polyline of a sparkline in a width × height box. `max` fixes the scale (100 for
 * percentages); otherwise the series' own range, so small changes stay visible. */
export function sparkPoints(points: { time: number, value: number }[], width: number, height: number,
  options: { min?: number, max?: number, from?: number, to?: number } = {}): string {
  if (points.length < 2) return ''
  const from = options.from ?? points[0]!.time
  const to = options.to ?? points[points.length - 1]!.time
  const values = points.map(p => p.value)
  let min = options.min ?? Math.min(...values)
  let max = options.max ?? Math.max(...values)
  if (max - min < 1e-9) {
    min -= 1
    max += 1
  }
  const pad = 2
  return points.map((p) => {
    const x = to > from ? ((p.time - from) / (to - from)) * width : 0
    const y = pad + (1 - (p.value - min) / (max - min)) * (height - 2 * pad)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

/** Adds a live sample to the history and drops what is older than the window. */
export function appendSample(history: HostHistoryPoint[], sample: HostSample, windowSeconds: number): HostHistoryPoint[] {
  if (sample.time === null) return history
  if (history.length && history[history.length - 1]!.time >= sample.time) return history
  const point: HostHistoryPoint = {
    time: sample.time,
    values: Object.fromEntries(sample.readings.map(r => [r.id, r.value])),
    vms: Object.fromEntries(Object.entries(sample.vms ?? {}).map(([k, v]) => [k, v.memory_bytes])),
  }
  const cutoff = sample.time - windowSeconds
  return [...history.filter(p => p.time >= cutoff), point]
}
