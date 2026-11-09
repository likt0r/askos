import { fileURLToPath } from 'node:url'

// The Control Room UI: a client-side only Nuxt app, generated to static files in dist/
// (built in CI, not committed, ADR 0011; installed on the Host without Node, ADR 0003)
// and served by web/agentbox-web with a Content-Security-Policy of 'self' plus the hashes of the
// inline <script>/<style> in index.html. Everything here keeps it that way: no
// runtime-injected <style> or <script>, no request to another origin, fonts and icons
// bundled. See docs/control-room-api.md "Serving the UI".
export default defineNuxtConfig({
  compatibilityDate: '2026-10-01',
  ssr: false,
  devtools: { enabled: false },
  telemetry: false,

  modules: ['@nuxt/ui', '@nuxtjs/i18n'],
  css: ['~/assets/css/main.css'],

  app: {
    head: {
      title: 'Control Room',
      htmlAttrs: { lang: 'en' },
      meta: [{ name: 'robots', content: 'noindex' }],
      link: [{ rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }],
    },
  },
  // the default loading screen carries inline styles; the app mounts fast enough
  spaLoadingTemplate: false,

  // the same source gives the same index.html (its hash is the UI's version)
  buildId: 'control-room',

  ui: {
    // @nuxt/fonts would fetch from Google Fonts at build time; the fonts come from
    // @fontsource packages in main.css instead
    fonts: false,
    theme: { colors: ['primary', 'secondary', 'success', 'info', 'warning', 'error', 'neutral'] },
  },

  colorMode: {
    // the default (set by Nuxt UI) adds a <style> element on every switch
    disableTransition: false,
  },

  icon: {
    // 'css' mode adds <style> elements at runtime (blocked by the CSP); 'svg' renders
    // the icons as elements. Only icons from the bundled collection, never the
    // Iconify API.
    mode: 'svg',
    provider: 'none',
    fallbackToApi: false,
    serverBundle: false,
    clientBundle: { scan: true, sizeLimitKb: 256 },
  },

  i18n: {
    strategy: 'no_prefix',
    defaultLocale: 'en',
    locales: [
      { code: 'en', language: 'en', name: 'English' },
      { code: 'de', language: 'de', name: 'Deutsch' },
    ],
    // the language follows a choice remembered in localStorage, else (once signed in)
    // `default_language` of the Settings when it is en or de, else the browser
    // (app/utils/language.ts, plugins/language.client.ts, composables/useLanguage.ts and
    // middleware/auth.global.ts, with the value from GET or POST /session),
    // not the module's cookie
    detectBrowserLanguage: false,
    vueI18n: './i18n.config.ts',
  },

  experimental: {
    // the app manifest (/_nuxt/builds/latest.json) would be cached as immutable by the
    // backend; new builds are announced by the `version` event instead
    appManifest: false,
  },

  nitro: {
    // write the static site straight into dist/ as real files (what the Host installs)
    output: { publicDir: fileURLToPath(new URL('./dist', import.meta.url)) },
    // only index.html: the backend answers every other app path with it
    prerender: { crawlLinks: false, routes: ['/'], ignore: ['/200.html', '/404.html'] },
    hooks: {
      // the payload's prerender time would change index.html on every build
      'prerender:generate'(route) {
        if (route.contents) route.contents = route.contents.replace(/("prerenderedAt":\d+,"serverRendered":\d+\},)\d+/, '$10')
      },
    },
  },

  hooks: {
    // Nuxt UI's colors plugin appends a <style> element at runtime, which the CSP
    // blocks; the same CSS variables are in main.css.
    'app:resolve'(app) {
      app.plugins = app.plugins.filter(p => !/@nuxt[\\/]ui[\\/].*plugins[\\/]colors/.test(p.src))
    },
  },
})
