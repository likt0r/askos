import { defineVitestConfig } from '@nuxt/test-utils/config'

// Component and unit tests in a Nuxt environment on happy-dom; they run in the VM
// (npm test), never on the Host (ADR 0003).
export default defineVitestConfig({
  test: {
    environment: 'nuxt',
    environmentOptions: {
      nuxt: {
        domEnvironment: 'happy-dom',
        overrides: { ssr: false },
      },
    },
    include: ['test/**/*.test.ts'],
    restoreMocks: true,
    unstubGlobals: true,
  },
})
