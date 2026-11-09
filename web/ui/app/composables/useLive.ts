let live: Live | null = null

/** The page's single live connection (created on first use, started by the app shell). */
export function useLive(): Live {
  if (!live) {
    live = createLive({
      checkSession: () => useSession().refresh(),
      fetchVms: () => api<VmList>('/vms'),
      onSignedOut: () => useSession().ended(),
    })
  }
  return live
}

/** Tests only: drop the instance (and its EventSource). */
export function resetLiveForTests() {
  live?.stop()
  live = null
}
