// The Control Room API v1 (docs/control-room-api.md): JSON in and out, the session cookie
// is sent by the browser, every request other than GET carries `X-Agentbox: 1` (CSRF).
// Errors become ApiError with the backend's stable code; the UI translates the code.

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly field?: string
  readonly detail?: string
  /** what the UI words some refusals with (`reason` of invalid_pattern, `host` of pattern_too_broad) */
  readonly params?: Record<string, string>
  /** seconds from a Retry-After header (login brake, too many streams) */
  readonly retryAfter?: number

  constructor(status: number, code: string, message: string,
    extra: { field?: string, detail?: string, params?: Record<string, string>, retryAfter?: number } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.field = extra.field
    this.detail = extra.detail
    this.params = extra.params
    this.retryAfter = extra.retryAfter
  }
}

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

let unauthenticatedHandler: (() => void) | null = null

/** Called when a request (other than the session endpoints) answers 401: the session ended. */
export function onUnauthenticated(handler: (() => void) | null) {
  unauthenticatedHandler = handler
}

/** An error's parameters: only text values. */
function stringParams(p: unknown): Record<string, string> | undefined {
  if (!p || typeof p !== 'object' || Array.isArray(p)) return undefined
  const out = Object.fromEntries(Object.entries(p).filter(([, v]) => typeof v === 'string')) as Record<string, string>
  return Object.keys(out).length ? out : undefined
}

function retryAfterSeconds(header: string | null): number | undefined {
  if (!header) return undefined
  const n = Number(header)
  if (Number.isFinite(n) && n >= 0) return Math.ceil(n)
  const date = Date.parse(header)
  return Number.isNaN(date) ? undefined : Math.max(0, Math.ceil((date - Date.now()) / 1000))
}

export async function api<T = unknown>(path: string, options: { method?: Method, body?: unknown } = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (method !== 'GET') headers['X-Agentbox'] = '1'
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  let response: Response
  try {
    response = await fetch(API_BASE + path, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: 'same-origin',
      cache: 'no-store',
    })
  }
  catch {
    throw new ApiError(0, 'network', 'The Control Room cannot be reached')
  }
  let data: unknown = null
  const text = await response.text()
  if (text) {
    try {
      data = JSON.parse(text)
    }
    catch {
      data = null
    }
  }
  if (response.ok) return data as T
  const error = (data as { error?: { code?: unknown, message?: unknown, field?: unknown, detail?: unknown, params?: unknown } } | null)?.error
  const code = typeof error?.code === 'string' ? error.code : `http_${response.status}`
  const err = new ApiError(response.status, code,
    typeof error?.message === 'string' ? error.message : response.statusText || 'Request failed', {
      field: typeof error?.field === 'string' ? error.field : undefined,
      detail: typeof error?.detail === 'string' ? error.detail : undefined,
      params: stringParams(error?.params),
      retryAfter: retryAfterSeconds(response.headers.get('Retry-After')),
    })
  if (response.status === 401 && code === 'unauthenticated' && !path.startsWith('/session')) {
    unauthenticatedHandler?.()
  }
  throw err
}
