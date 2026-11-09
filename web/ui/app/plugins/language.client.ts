// The language when the page loads: one chosen here (remembered per browser), else the
// browser's, else English. Once signed in, `default_language` of the Settings comes before the
// browser's language when it is en or de (applyDefaultLanguage, called by the auth middleware).
export default defineNuxtPlugin({
  name: 'agentbox-language',
  dependsOn: ['i18n:plugin'],
  async setup(nuxtApp) {
    await nuxtApp.$i18n.setLocale(pickLanguage(storedLanguage(), browserLanguages()))
  },
})
