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
  error.vue                the whole window when the shell itself fails (Nuxt's own error page
                           never shows)
  layouts/default.vue      shell: sidebar navigation, reload prompt, host bar, connection notice,
                           page area (components/fault/FaultBoundary)
  layouts/bare.vue         sign-in
  middleware/auth.global   every page but /sign-in needs a session (GET /session)
  pages/                   /sign-in, /vms, /vms/<vm>/{web,access,log,pair}, /allowlist,
                           /watchlist, /activity, /system, /settings; [...path] for every other
                           address (this page does not exist)
  components/              ConnectionStatus, ConnectionNotice, HostBar, LanguageSwitch,
                           SignOutButton, VmList, VmStateBadge, PlaceholderView, ReadFailure
                           (where a failed read of a resource shows), ChangeFailure (a failed
                           change at its form), FailureText (a failure's text with Details)
  components/fault/        FaultBoundary (a fault that stops the page shows in the page area,
                           cleared by going elsewhere), FaultView (unknown address, stopped view,
                           failed shell), FaultDetails (the message, folded away, copyable)
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
  components/access/       GithubOwners, ClaudeToken, SshAccess, GithubTokens (Access tab), ConfirmDialog
  components/settings/     SettingsForm, SettingsField
  components/notifications/  NotificationsPreferences, NotificationsReloadPrompt
  composables/             useSession (with `default_language` from GET/POST /session), useLive
                           (the one live connection), useLanguage (the switch with Automatic;
                           the sign-in page's order and `default_language`, both applied by the
                           auth middleware), useErrorText (API error code -> translated text),
                           useNow, useActivity, useHost, useImages (the resource `images`,
                           rebuilds, Update now, the log of a Build), useSettings,
                           useNotifications (this browser's preference, the `notification`
                           listener), useReloadPrompt (own version vs. `version` event),
                           useLiveEvent (live.on() for a component, bursts gathered),
                           useReloadOnActivity (a view's load on the Activity entries that change its data),
                           useWhen (times in the UI language), useChangeToast (a failed change
                           without a form: a toast that stays, with Details), useFaultToast (a fault the UI can
                           go on from: a toast that stays, with Details)
  plugins/faults.client.ts Vue's error handler (a fault outside the page area: a toast for one in an
                           event handler, else error.vue) and unhandled promise rejections (a toast);
                           nothing about a fault is sent to the Host
  utils/api.ts             fetch for /api/v1: X-Agentbox: 1 on changes, ApiError with the code
  utils/resources.ts       the resources module: every resource declared once, the cache
                           (TanStack Query), the table of what an event does, useResource, change
  utils/live.ts            EventSource state: snapshot + vm events, reconnect, dead stream;
                           `live.on(name, fn)` for the other events
  utils/language.ts        the choice in this browser (localStorage; none: Automatic), else
                           `default_language` of the Settings if en or de (once signed in), else
                           the browser's language, else English; the sign-in page skips the Settings
  utils/signInLink.ts      the token of a sign-in link in the sign-in page's fragment (#link=)
  utils/failures.ts        where a failure of the API appears, by its kind; Details of it
  utils/faults.ts          a fault's message for Details (never its stack), which faults leave the
                           view usable
  utils/activity, clock, host, images, notifications, settings, vmRules, access
                           plain functions for the views above (formatting, filters, merging
                           events, validation); Vitest tests them without a component
public/notification-sw.js  service worker that only shows notifications (Android's Chrome
                           has no `new Notification`); served by the backend as a file
i18n/locales/{en,de}.ts    UI strings of the shell; every API error code has a translation
i18n/locales/{en,de}/<area>.ts   strings per area (access, activity, change, fault, notifications, read,
                           settings, system, vm), spread into en.ts and de.ts (vm.ts sits under `vm`)
test/                      Vitest
```

### Server data: the resources module

Server data lives in one cache, in `app/utils/resources.ts` ([ADR
0026](../../docs/adr/0026-the-control-room-ui-holds-server-data-in-one-cache.md)). The resources
it holds so far: `images` (`GET /images`). Every other view still reads on its own, as the next
section describes, until it moves into the module (#212).

- **Declaring a resource**: one line in `RESOURCES`, its name, its path and the type of its
  answer (`images: declare<ImagesOverview>('/images')`). The key in the cache is `[name]`.
- **Asking for one**: `const images = useResource('images')` gives `data` (undefined until an
  answer came), `loading` (nothing yet and a read on the way), `fetching` (a read on the way,
  the first or a read again), `error` (why the last read failed) and `retry()`. Every view that asks shares one copy and one read. `{ enabled: open }` reads
  only while a dialog is open; `{ extra: true }` declares a read whose failure the view does not
  show. A view does not know the path, when to read, or how to drop a late answer.
- **Showing a failed read**: `<ReadFailure :resource="images" />`, in place of the section.
  When nothing was read yet, it says what could not be read and why (`useErrorText`), with Retry.
  When data is shown, the data stays and a small notice says it could not be refreshed, with
  Retry. While the live connection is down it shows nothing: the connection notice says that.
  It also shows nothing for a read that could not reach the Control Room (`ApiError` code
  `network`) before the stream is live, which the next `hello` reads again. Once the stream is
  live, such a read shows like any other. For an extra it shows nothing. It offers no "Details"
  yet: the Host's text is shown only once #215 has checked that it cannot carry a secret.
- **What an event does** (`ON_EVENT`, `ON_ACTIVITY`): an event that carries the whole object
  is written into the cache (`build`, through `applyBuild`). An event that only says something
  changed marks the resource stale: `images` after a `build` event whose status is not `running`
  or that `applyBuild` cannot place, after `updating` of the `services` event goes from true to
  false, when the version check (`agentbox-versions.service` in the `services` event) stops
  running (started by hand, it records no Activity entry), and after the Activity codes
  `maintenance.versions`, `update-now`, `recipe.set`, `recipe.restore`, `recipe.remove` (a Recipe's Image comes or goes) and
  `create`, `delete`, `set-image`, `reset`, `reset.automatic`, `reset.update` (which VMs use
  and pin each Build). A write keeps a stale mark and a failed read's error. A
  stale resource that a view shows is read again; one that nothing shows is read when a view
  next asks for it. A new Activity code that changes a resource needs a row here; without one,
  the view shows old data until the next reconnect or reload.
- **An event during a read**: an event written while a read of its resource is on the way is
  applied to that read's answer as well, and a resource that turns stale during a read is read
  once more right after it (once for a whole burst). The older answer never undoes an event.
- **When data is read again**: only for a reason the stream or the owner gives: a row of the
  table, a change made in this browser, a `hello` with `resumed: false` that is not the first
  (everything is stale, and `onMissedEvents` listeners run for what lives outside the cache,
  such as a Build's log), any `hello` for the reads that could not reach the Control Room, or
  Retry. Never by the clock, never on window focus, never as an automatic retry.
- **A change made in this browser**: every change is declared once in `CHANGES`, its request
  from the view's arguments and what it makes stale (`rebuild: declareChange((image: string) =>
  ({ method: 'POST', path: '/builds', body: { image } }))`). A view asks for it by name:
  `const start = useChange('rebuild', { form, own, failed })` gives `run(args)` (an `{ ok, data }`
  or `{ ok: false, error }`; nothing while one runs), `busy`, `error`, `shown`, `text` (`shown`
  in the UI's words, for a view that puts it at one field) and `reset()`; it is a TanStack Query
  mutation. After it, the resources it changes are read again once: through the change's own
  Activity entry and its row in `ON_ACTIVITY`, or, for a change that records no entry, through
  the `stale` of its declaration (none so far). A view does not read again itself. The
  request (a secret's value, a key) is not kept once the change is done: the mutation is reset
  and dropped from the cache at once.
- **Where a failed change appears** (`utils/failures.ts`, decided once by its kind, when the
  change fails): one sent from a form or a dialog (`form: true`), a refusal included, at that
  form, with `<ChangeFailure :error="start.shown.value" />`; one without a form, a refusal
  included, as a toast that stays until dismissed (title from `failed`); an ended session on the
  sign-in page; a Control Room out of reach while the connection notice says the live connection
  dropped in that notice alone (otherwise nothing else would say it, so the change's own place
  does). A view names in `own` only the codes it words itself: `build_running` and
  `update_running` beside the Images' buttons, `domain_suspicious` of an Allowlist entry for a
  domain (`AllowlistRefusal`, with its Signals and how to lift them), `force_required` of the
  Watchlist and the Legitimate domains and `watchlist_recreate_force_required` of the Watchlist
  (with the command for the Host). The module presents none of them, and the view reads them from `error`.
- **Details** of a failure: its code and the Host's `message`, which the backend always words
  itself; never its `detail`, which may pass on another program's output, from inside a VM too
  (docs/control-room-api.md, "Errors").
- **Timing**: `notifyManager.setScheduler(queueMicrotask)` at the top of the module. Answers
  reach the views in a microtask, not in the `setTimeout(0)` of query-core's default. The app
  and the tests take the same path, and a test waits with `flushPromises()`.
- **Tests**: `resetLiveForTests()` also clears the cache. Signing out clears it as well
  (`clearResources`).

### Subscribing to the live stream

`useLive()` owns the one `EventSource`. State events (`state`, `vm`, `host`, ...) update
`live.state`. Everything else is delivered to subscribers: `const off = live.on('activity',
(data) => ...)` (names in `STREAM_EVENTS` in `utils/live.ts`; state events and `hello` work
too, and the listener runs after the state was updated). Events are whole objects, so a
repeat does no harm. Subscribe in a composable and call `off()` in `onScopeDispose`; refetch
on `hello` with `resumed: false`, because events may have been missed. A throwing listener
does not stop the others. In a component that only reloads a list on an event,
`useLiveEvent(name, fn, delayMs)` does the subscribing and unsubscribing and runs `fn` once
per burst. A view that shows data an Activity entry can change uses
`useReloadOnActivity(load, { codes, kinds, match })`: it calls `load` once per burst when an
`activity` entry's `code ?? action` is in `codes` (or its `kind` in `kinds`, and `match(entry)` agrees),
and after a `hello` with `resumed: false` that is not the view's first.

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
