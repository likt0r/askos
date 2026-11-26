// The catchers for faults of the UI itself (#213). A fault the page area does not take
// (components/fault/FaultBoundary.vue) reaches Vue's error handler: one in an event handler
// leaves the shell usable and shows a toast; any other means the shell failed, and
// app/error.vue takes the whole window. An unhandled promise rejection shows the toast.
// Faults stay in the browser (the console); nothing is sent to the Host.
export default defineNuxtPlugin({
  name: 'agentbox-faults',
  dependsOn: ['i18n:plugin'],
  setup(nuxtApp) {
    const showToast = useFaultToast()

    nuxtApp.vueApp.config.errorHandler = (error, _instance, info) => {
      console.error(error)
      if (inEventHandler(info)) showToast(error)
      else void nuxtApp.runWithContext(() => showError(error as Error))
    }

    window.addEventListener('unhandledrejection', (event) => {
      if (shownElsewhere(event.reason)) return
      console.error(event.reason)
      showToast(event.reason)
    })
  },
})
