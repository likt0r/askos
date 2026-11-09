import { onScopeDispose, ref } from 'vue'

/** The current time in ms, updated every second while the component lives. */
export function useNow(intervalMs = 1000) {
  const now = ref(Date.now())
  const timer = setInterval(() => (now.value = Date.now()), intervalMs)
  onScopeDispose(() => clearInterval(timer))
  return now
}
