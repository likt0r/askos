// Every page except /sign-in needs a session; the first navigation asks GET /session. The
// language is set before the page shows: signed in, the default language of the Settings (it
// came with the session), after a page load as after a sign-in; on the sign-in page, also after
// signing out, the order without it.
export default defineNuxtRouteMiddleware(async (to) => {
  const session = useSession()
  if (!session.state.known) {
    try {
      await session.refresh()
    }
    catch {
      // backend unreachable: the sign-in page says so when the password is sent
    }
  }
  if (to.path === '/sign-in') {
    if (session.state.authenticated) return navigateTo(safeRedirect(to.query.redirect), { replace: true })
    await applySignedOutLanguage()
    return
  }
  if (!session.state.authenticated) {
    return navigateTo({ path: '/sign-in', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }, { replace: true })
  }
  await applyDefaultLanguage()
})
