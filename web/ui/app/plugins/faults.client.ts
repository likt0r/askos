// The catchers for faults of the UI itself (#213). A fault the page area does not take
// (components/fault/FaultBoundary.vue) reaches Vue's error handler: one in an event handler
// leaves the shell usable and shows a toast; any other means the shell failed, and
// app/error.vue takes the whole window. An unhandled promise rejection shows the toast. An ended
// session or a Control Room that cannot be reached is the sign-in page's and the connection
// notice's to say.
// Faults stay in the browser (the console); nothing is sent to the Host.
export default defineNuxtPlugin({
  name: 'agentbox-faults',
  dependsOn: ['i18n:plugin'],
  setup(nuxtApp) {
    const showToast = useFaultToast()

    nuxtApp.vueApp.config.errorHandler = (error, _instance, info) => {
      console.error(error)
      if (shownElsewhere(error)) return
      if (inEventHandler(info)) showToast(error)
      // the fault itself as the cause: its message for Details, and no status of its own (an
      // ApiError's 404 would read as an address that does not exist)
      else void nuxtApp.runWithContext(() => showError({ status: 500, message: faultMessage(error), cause: error }))
    }

    window.addEventListener('unhandledrejection', (event) => {
      console.error(event.reason)
      showToast(event.reason)
    })
  },
})
