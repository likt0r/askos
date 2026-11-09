import { reactive, readonly } from 'vue'

interface SessionState {
  /** GET /session has answered (or failed) at least once */
  known: boolean
  authenticated: boolean
  expires: string | null
  /** `default_language` of the Settings as GET or POST /session gave it (or Settings saved it) */
  defaultLanguage: string | null
}

const state = reactive<SessionState>({ known: false, authenticated: false, expires: null, defaultLanguage: null })

type SessionAnswer = { authenticated: boolean, expires: string, default_language?: unknown }

let toSignIn: (() => void) | null = null

/** How to leave for the sign-in page when the session ends; set by plugins/session.client.ts. */
export function setSessionEndedHandler(handler: (() => void) | null) {
  toSignIn = handler
}

function language(value: unknown) {
  return typeof value === 'string' ? value : null
}

/** The sign-in state, shared by the whole page. */
export function useSession() {
  /** Ask the backend; a network error leaves the state unknown and throws. */
  async function refresh(): Promise<boolean> {
    try {
      const r = await api<SessionAnswer>('/session')
      state.authenticated = r.authenticated === true
      state.expires = r.expires ?? null
      state.defaultLanguage = language(r.default_language)
    }
    catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) throw e
      state.authenticated = false
      state.expires = null
      state.defaultLanguage = null
    }
    finally {
      state.known = true
    }
    return state.authenticated
  }

  async function signIn(password: string) {
    const r = await api<SessionAnswer>('/session', { method: 'POST', body: { password } })
    state.authenticated = true
    state.expires = r.expires ?? null
    state.defaultLanguage = language(r.default_language)
    state.known = true
  }

  async function signOut() {
    try {
      await api('/session', { method: 'DELETE' })
    }
    finally {
      markSignedOut()
    }
  }

  function markSignedOut() {
    state.authenticated = false
    state.expires = null
    state.defaultLanguage = null
    state.known = true
    useLive().reset()
  }

  /** The session ended on its own (expired, signed out elsewhere, backend restarted). */
  function ended() {
    markSignedOut()
    toSignIn?.()
  }

  /** Settings saved a new `default_language`. */
  function setDefaultLanguage(value: unknown) {
    state.defaultLanguage = language(value)
  }

  return { state: readonly(state), refresh, signIn, signOut, markSignedOut, ended, setDefaultLanguage }
}

/** Tests only. */
export function resetSessionForTests() {
  Object.assign(state, { known: false, authenticated: false, expires: null, defaultLanguage: null })
}
