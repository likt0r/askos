/** A path inside the app to return to after sign-in; anything else (another origin,
 * `//host`, the sign-in page itself) becomes the VM list. */
export function safeRedirect(value: unknown): string {
  const path = Array.isArray(value) ? value[0] : value
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') || path.includes('\\')) return '/vms'
  if (path === '/sign-in' || path.startsWith('/sign-in?') || path.startsWith('/api/')) return '/vms'
  return path
}
