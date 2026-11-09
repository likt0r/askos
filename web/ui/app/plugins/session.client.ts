// A 401 from any request or the live stream means the session ended (expired, signed out
// elsewhere, backend restarted): forget it and go to the sign-in page, back here after.
export default defineNuxtPlugin(() => {
  const router = useRouter()
  setSessionEndedHandler(() => {
    const route = router.currentRoute.value
    if (route.path !== '/sign-in') {
      void router.replace({ path: '/sign-in', query: { redirect: route.fullPath, expired: '1' } })
    }
  })
  onUnauthenticated(() => useSession().ended())
})
