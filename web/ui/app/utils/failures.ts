// Where a failure of the Control Room API appears (ADR 0026, "Where a failure appears"), decided
// here once by its kind, and what "Details" shows of it. Reads show theirs through ReadFailure,
// changes through useChange (utils/resources.ts); the text for a code is useErrorText's.
import { ApiError } from './api'

/** sign-in: the session ended (utils/api.ts sends the page to the sign-in page); connection: the
 * Control Room cannot be reached while the connection notice says the live connection dropped;
 * form: at the form or in the dialog that sent the change; toast: a toast that stays. */
export type FailurePlace = 'sign-in' | 'connection' | 'form' | 'toast'

/** Where a failed change appears: one sent from a form or a dialog, a refusal included, at that
 * form; one without, a refusal included, as a toast. `dropped`: the connection notice says the
 * live connection dropped (otherwise nothing else would say that the Control Room is out of
 * reach). */
export function failurePlace(error: unknown, where: { form: boolean, dropped: boolean }): FailurePlace {
  if (error instanceof ApiError && error.status === 401) return 'sign-in'
  if (unreachable(error) && where.dropped) return 'connection'
  return where.form ? 'form' : 'toast'
}

/** Whether a request failed only because the Control Room could not be reached. */
export function unreachable(error: unknown) {
  return error instanceof ApiError && error.code === 'network'
}

/** "Details" of a failure: its code and the Host's English `message`, which the backend always
 * words itself (web/agentbox-web, AGENTBOX_ERRORS and fixed texts). Never its `detail`: that is
 * agentbox's own text, which may pass on another program's output, from inside a VM too, and so
 * a VM's secret. Null for anything that is not an answer of the API. */
export function failureDetails(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.code === 'network') return null
  return error.message ? `${error.code}: ${error.message}` : error.code
}
