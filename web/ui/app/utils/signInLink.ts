// A sign-in link from the Host (`agentbox sign-in-link`): /sign-in#link=<token>. The token is
// in the fragment, which the browser never sends to a server; the sign-in page sends it in the
// body of POST /session, once.
const LINK = /^#link=([A-Za-z0-9_-]{1,128})$/

/** The token of a sign-in link in a page's fragment (`route.hash`), or null without one. */
export function linkFromHash(hash: string): string | null {
  return LINK.exec(hash)?.[1] ?? null
}
