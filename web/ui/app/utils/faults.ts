// Faults of the UI itself (#213): what the owner sees of one, and whether the view can go on.
// Nothing about a fault leaves the browser.
import { ApiError } from './api'

/** The text behind "Details": the error's message, never its stack. A request failure that
 * reached a fault catcher shows its status and code, not the Host's text. */
export function faultMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.name} ${error.status} ${error.code}`
  if (error instanceof Error) return error.message || error.name
  if (typeof error === 'string') return error
  try {
    return JSON.stringify(error) ?? String(error)
  }
  catch {
    return String(error)
  }
}

/** Vue's `info` for a fault in an event handler: the view is still there and can go on.
 * Vue names the place in development and gives its error code in a production build
 * (runtime-5: native event handler, runtime-6: component event handler). */
export function inEventHandler(info: string): boolean {
  return /event handler|#runtime-[56]$/.test(info)
}

/** A rejection that other parts of the UI already show: an ended session (the sign-in page)
 * and a Control Room that cannot be reached (the connection notice). */
export function shownElsewhere(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.code === 'network')
}
