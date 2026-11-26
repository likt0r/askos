import { onScopeDispose } from 'vue'

export interface ReloadOptions {
  /** Activity codes (`code ?? action` of the entry) that change what `load` reads */
  codes?: string[]
  /** Activity kinds that change what `load` reads */
  kinds?: string[]
  /** a further condition on the entry (e.g. it is about this VM) */
  match?: (entry: any) => boolean
  /** a burst is gathered into one call, this long after its last event */
  delayMs?: number
}

/** Calls `load` when an Activity entry that changes the view's data arrives (the entry's code
 * is in `codes` or its kind in `kinds`, and `match` agrees; entries of both outcomes count),
 * and after a `hello` with `resumed: false` that is not the view's first: a reconnect that
 * could not resume missed events. Bursts are gathered into one call. Stops with the scope. */
export function useReloadOnActivity(load: () => unknown, options: ReloadOptions = {}) {
  const live = useLive()
  const delayMs = options.delayMs ?? 300
  let timer: ReturnType<typeof setTimeout> | null = null

  function schedule() {
    if (delayMs <= 0) return void load()
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      void load()
    }, delayMs)
  }

  const offActivity = live.on('activity', (entry: any) => {
    if (!entry || typeof entry !== 'object') return
    const code = entry.code ?? entry.action
    const listed = (options.codes ?? []).includes(code) || (options.kinds ?? []).includes(entry.kind)
    if (listed && (!options.match || options.match(entry))) schedule()
  })

  let helloSeen = live.state.connection === 'live'    // already live when the view was created
  const offHello = live.on('hello', (data: { resumed?: boolean }) => {
    if (helloSeen && data?.resumed === false) schedule()
    helloSeen = true
  })

  onScopeDispose(() => {
    offActivity()
    offHello()
    if (timer) clearTimeout(timer)
  })
}
