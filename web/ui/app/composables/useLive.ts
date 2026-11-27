let live: Live | null = null

/** The page's single live connection (created on first use, started by the app shell). */
export function useLive(): Live {
  if (!live) {
    live = createLive({
      checkSession: () => useSession().refresh(),
      // the cache is there to take what the stream pushes from its first event
      beforeStart: () => startResources(),
      onSignedOut: () => useSession().ended(),
    })
  }
  return live
}

/** Tests only: drop the instance (and its EventSource) and the cache of the resources. */
export function resetLiveForTests() {
  live?.stop()
  live = null
  clearResources()
}
