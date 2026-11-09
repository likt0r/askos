import { onScopeDispose } from 'vue'

/** Follows a live event (`alert`, `activity`, `github-token`, ...) while the component lives.
 * Bursts are gathered: fn runs once, `delayMs` after the last event of a burst. */
export function useLiveEvent(name: string, fn: (data: any) => void, delayMs = 300) {
  let timer: ReturnType<typeof setTimeout> | null = null
  let last: unknown = null
  const off = useLive().on(name, (data) => {
    last = data
    if (delayMs <= 0) return fn(data)
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      fn(last)
    }, delayMs)
  })
  onScopeDispose(() => {
    off()
    if (timer) clearTimeout(timer)
  })
}
