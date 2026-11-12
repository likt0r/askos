# Control Room UI

The browser side of the Control Room: a Nuxt app with Nuxt UI, client-side only, generated to
static files in `dist/` and served by the backend `web/agentbox-web`. npm runs **in a VM or
in CI only**, never on the Host ([ADR 0003](../../docs/adr/0003-no-node-on-the-host.md));
the Host installs the generated files without Node. `dist/` is **not committed**: CI builds
it from the lockfile ([ADR 0011](../../docs/adr/0011-the-control-room-ui-is-built-in-ci.md)).
The API it uses: [docs/control-room-api.md](../../docs/control-room-api.md).

## Working on it (in a VM)

```sh
cd web/ui
npm ci                   # exactly the lockfile; install scripts do not run (ignore-scripts),
                         # and none are needed
npm run prepare:nuxt     # what Nuxt's postinstall would do: types and .nuxt/
npm test                 # Vitest: components and units on happy-dom
npm run dev              # dev server with hot reload; /api/v1 is not proxied to a backend
npm run generate         # writes dist/ and checks it (scripts/check-dist.mjs)
```

Commit only the source (and `package-lock.json` when dependencies change); `dist/` is in
`.gitignore`. The same source always gives the same `dist/` (fixed `buildId`, no build time
in the page), so an unchanged UI keeps its version.

To check a build with the real backend, run `python3 -m unittest tests.test_control_room_ui`
from the repo root after `npm run generate`: it serves `dist/` against the fake agentbox and
checks the page, deep links, every file it loads and its CSP. Without `dist/` these tests are
skipped (so on the Host, which has no Node and no build, they are).

`scripts/node-icu-workaround.mjs` is preloaded by the npm scripts: the VM image's Node
(Fedora `nodejs22` 22.23.1, built for ICU 78 but running against libicu 77) crashes in
`Intl.Segmenter`, which the build tools use only to measure terminal output. On a working
Node it does nothing.

## In CI

The `ui` job of `.github/workflows/ci.yml` runs `npm ci` (which fails when the lockfile and
`package.json` disagree), `npm run prepare:nuxt`, `npm test` and `npm run generate`, then
`tests/test_control_room_ui.py` against that `dist/`; there `AGENTBOX_REQUIRE_UI_BUILD=1`
makes a missing build a failure instead of a skip. The build is not uploaded: an artifact
exists only for a Release, whose workflow builds the UI for its RPM the same way (ADR 0011,
#81, #83). The `python-tests` job has no build, so those tests skip there.

## On the Host

The RPM of a Release brings the UI build with it, in `/usr/share/agentbox/web/ui/` (symbolic
links in the build are dropped). A Git checkout on the Host has no `dist/`.

## Layout

```
app/
  app.vue                  UApp, the layout, the page
  layouts/default.vue      shell: sidebar navigation, reload prompt, host bar, connection notice, page
  layouts/bare.vue         sign-in
  middleware/auth.global   every page but /sign-in needs a session (GET /session)
  pages/                   /sign-in, /vms, /vms/<vm>/{web,access,log,pair}, /allowlist,
                           /watchlist, /activity, /system, /settings
  components/              ConnectionStatus, ConnectionNotice, HostBar, LanguageSwitch,
                           SignOutButton, VmList, VmStateBadge, PlaceholderView
  components/activity/     ActivityFilters, ActivityList
  components/host/         HostTiles, Sparkline (host bar details and the System page)
  components/system/       SystemHostReboot, SystemImages, SystemImageCard, SystemImageDefinition
                           (the Containerfile: link, text), SystemBuildLog,
                           GithubAppSection (App, Installations, replace App ID/key), SystemServices
  components/vm/           VM detail: Overview, Actions, NotResponding, ConfirmDialog, DeleteDialog,
                           ImageDialog, MemoryDialog, ContainerImageCleanupDialog, CreateDialog (VM list), LogView (Log tab),
                           PairView (Pair tab)
  components/allowlist/    EntryList, AddDialog, Refusal (/allowlist and the Web tab),
                           OpenWeb, BlockedList (Web tab)
  components/watchlist/    AlertsPanel, DomainCheck, HarmlessList, HandList (Watchlist entries),
                           LegitimateList, Feeds, SignalList, SignalBadges
  components/access/       GithubOwners, ClaudeToken, GithubTokens (Access tab), ConfirmDialog
  components/settings/     SettingsForm, SettingsField
  components/notifications/  NotificationsPreferences, NotificationsReloadPrompt
  composables/             useSession (with `default_language` from GET/POST /session), useLive
                           (the one live connection), useLanguage (the switch with Automatic;
                           the sign-in page's order and `default_language`, both applied by the
                           auth middleware), useErrorText (API error code -> translated text),
                           useNow, useActivity, useHost, useImages, useSettings,
                           useNotifications (this browser's preference, the `notification`
                           listener), useReloadPrompt (own version vs. `version` event),
                           useLiveEvent (live.on() for a component, bursts gathered),
                           useWhen (times in the UI language)
  utils/api.ts             fetch for /api/v1: X-Agentbox: 1 on changes, ApiError with the code
  utils/live.ts            EventSource state: snapshot + vm events, reconnect, dead stream;
                           `live.on(name, fn)` for the other events
  utils/language.ts        the choice in this browser (localStorage; none: Automatic), else
                           `default_language` of the Settings if en or de (once signed in), else
                           the browser's language, else English; the sign-in page skips the Settings
  utils/activity, clock, host, images, notifications, settings, vmRules, access
                           plain functions for the views above (formatting, filters, merging
                           events, validation); Vitest tests them without a component
public/notification-sw.js  service worker that only shows notifications (Android's Chrome
                           has no `new Notification`); served by the backend as a file
i18n/locales/{en,de}.ts    UI strings of the shell; every API error code has a translation
i18n/locales/{en,de}/<area>.ts   strings per area (access, activity, notifications, settings,
                           system, vm), spread into en.ts and de.ts (vm.ts sits under `vm`)
test/                      Vitest
```

### Subscribing to the live stream

`useLive()` owns the one `EventSource`. State events (`state`, `vm`, `host`, ...) update
`live.state`. Everything else is delivered to subscribers: `const off = live.on('activity',
(data) => ...)` (names in `STREAM_EVENTS` in `utils/live.ts`; state events and `hello` work
too, and the listener runs after the state was updated). Events are whole objects, so a
repeat does no harm. Subscribe in a composable and call `off()` in `onScopeDispose`; refetch
on `hello` with `resumed: false`, because events may have been missed. A throwing listener
does not stop the others. In a component that only reloads a list on an event,
`useLiveEvent(name, fn, delayMs)` does the subscribing and unsubscribing and runs `fn` once
per burst.

### Adding an i18n area

Create `i18n/locales/en/<area>.ts` and `i18n/locales/de/<area>.ts`, each
`export default { <area>: { ... } }` with the same keys, import both in `en.ts` and `de.ts`
and spread them into the exported object (as `activity` is). `test/i18n.test.ts` fails when
the two languages differ. Keep a new area's strings out of the shared files otherwise, so
parallel work on different views does not conflict.

## Keeping the Content-Security-Policy at 'self'

The backend allows only files from the same origin plus the inline `<script>`/`<style>` of
`index.html` by hash. Anything added at runtime as a `<style>` or `<script>` element, or a
`style="..."` attribute in HTML, is blocked (Vue's `:style` is fine). Hence, in `nuxt.config.ts`:

- Nuxt UI's colors plugin is removed; its CSS variables are in `app/assets/css/colors.css`
  (`node scripts/colors-css.mjs` after changing the colors in `app/app.config.ts`).
- Icons render as SVG elements from the bundled `@iconify-json/lucide`, never the Iconify API.
- Fonts come from `@fontsource-variable/*` files via `main.css` (`@nuxt/fonts` is off: it
  would fetch from Google Fonts).
- color-mode does not add a transition-blocking `<style>`; no loading template.

Avoid components that inject styles: `UDrawer` (vaul) and the sidebar's `mode="drawer"`,
and `UBanner` with an `id` (persistence script).
