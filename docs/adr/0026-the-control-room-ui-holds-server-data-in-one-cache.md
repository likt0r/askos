# The Control Room UI holds all server data in one cache; an event writes into it or marks it stale

Status: Accepted (#212), by the owner on 2026-11-26. Built so far: the resources module with its first resource, `images`, its rows of the table and the read-failure element (#214, `web/ui/app/utils/resources.ts`, `web/ui/app/components/ReadFailure.vue`); every change of the UI but signing in and out through it, with its failure placed by kind (#215, `useChange`, `web/ui/app/utils/failures.ts`). Details show a failure's code and message, not its `detail` (#224). Reads move in one page at a time (#216): so far a VM's Paired devices (a resource with parameters, a VM's), the GitHub Apps, and a VM's GitHub tokens and SSH connection help; the Paired devices and the GitHub Apps are read again when a view of them opens, as the Host hears nothing of some of their changes (a device paired inside Orca, an App installed on GitHub). A code the Host answers for a state, not a failure, is read as data (`states`: no GitHub App set up). The other steps are the children of #212.

## Context

The Control Room UI (`web/ui`) shows what the Control Room API says and follows the live events of `GET /events`. On 2026-11-26 an architecture review counted how it does that:

- **Every view fetches and holds its own copy.** 23 `load()` functions in 19 files, each with its own loading and error state; 7 of them count requests by hand so that a late answer does not replace a newer one. `GET /images` is read from four places (the System page, a VM's versions, the dialogs to create a VM and to change its Image), `GET /settings` from three, `GET /github-app` from two, each holding a separate copy.
- **Which Activity entry changes which data is written into each view.** 15 call sites in 11 files list the Activity codes, kinds or live events after which a view reads again. Two views of the same data follow different entries, and some follow none: the GitHub owners of a VM and the longest duration of Open web are read once when the view opens and never again.
- **Pushed and fetched data live in two places.** The VM list, the Host's readings, the services, the version and the state of `agentbox watch` sit in the state of `app/utils/live.ts`; everything else in the views. Three composables (Activity, the log of a Build, the Host's history) each merge live events into a fetched snapshot with their own handling of events that arrive during the fetch and of a reconnect that could not resume.
- **Where a failure appears is decided in each catch block.** 95 catch blocks in 49 files, 38 hand-placed error alerts, one error toast. While the Control Room cannot be reached, every open section says so beside the connection notice. Six reads fail without a word. The UI has no error page, no catch-all route and no handler for a fault in a view or an unhandled rejection, and the `detail` of a failure is parsed and then shown nowhere.

The owner decided on 2026-11-26 to put all of this behind one module, with TanStack Query as its implementation.

## Decision

### One resources module

- **Every resource of the Control Room API is declared once**, in one module of `web/ui/app`: its key, its path and the type of its answer. A view asks for a resource by name and gets its data, whether it is loading and its failure. Paths, answer types and the handling of late answers leave the views.
- **TanStack Query (`@tanstack/vue-query`) is the module's implementation**, pinned to an exact version like the UI's other dependencies. It keeps one shared copy per resource, so every reader of a resource shows the same data. Its devtools package is not added.
- **All server data lives in this cache, pushed data included.** `state`, `vm`, `host`, `services`, `version`, `watch` and `host-reboot` are written into it; `GET /vms`, `GET /host`, `GET /system` and `GET /version` are the reads of the same resources. `app/utils/live.ts` keeps only the connection: the `EventSource`, reconnecting, the dead stream, and the connection's state.
- **Changes run through the module too** (`POST`, `PUT`, `PATCH`, `DELETE`): it runs the request, marks what the change makes stale and hands a failure to failure presentation (below).
- **`app/utils/api.ts` stays the one fetch** underneath, with `ApiError` and the stable codes ([ADR 0012](0012-the-server-speaks-english-with-stable-codes.md)).

### What an event does

One table in the module says, for every live event and Activity entry, which of two things happens:

| The event carries the whole object: it is written into the cache | The event only says that something changed: the resource is marked stale and read again |
|---|---|
| `state`, `vm`, `host`, `services`, `version`, `watch`, `host-reboot` | Allowlist, Forbids, Watchlist, Legitimate domains, Harmless marks |
| `build` (one Build), `build-log` (lines), `activity` (one entry, for the Activity timeline) | Settings, GitHub App, pairings, Alerts, the blocked list, GitHub tokens, a VM's SSH connection help, the Images overview once a Build ends |

- **The UI never computes server data from an Activity entry.** An entry of kind `allowlist` carries a code and parameters, not the Allowlist entry; working out the new list from it would repeat the backend's rules in the UI, such as which of an Allowlist entry and a Forbid wins ([ADR 0024](0024-the-more-specific-of-an-allowlist-entry-and-a-forbid-wins.md)).
- **An event that arrives while a read of the same resource is on the way is not lost to the older answer.** The module handles this once, for every resource.
- **Data is read again only for a reason the stream or the owner gives**: a row of the table, a change made in this browser, a `hello` with `resumed: false`, or the owner's "Retry". A resource whose changes the Host sends no event about, such as a VM's Paired devices, is read again whenever a view of it opens (the owner on #216, 2026-11-27). No reading again by the clock, none when the window gets the focus, and no automatic retry of a read that failed; when the connection comes back, the reads that failed because the Control Room could not be reached are read again (the owner on #223, 2026-11-26), and the others wait for "Retry" or a reason above.

### Where a failure appears

One place names the kind of every failure and picks where it appears. The text for an error code is worded there too.

| What failed | Where it appears |
|---|---|
| The session ended | the sign-in page |
| The Control Room cannot be reached | the connection notice alone while it says the live connection dropped; sections keep what they showed. With the stream live, the failure shows in its own place (the owner on #223) |
| A read failed and nothing was loaded yet | in place of the section, with "Retry" and "Details" |
| A read failed and the section shows data | the data stays, with a notice that it could not be refreshed and "Retry" |
| A change from a form or a dialog was refused or failed | at the form or in the dialog, never a toast |
| A change without a form or dialog failed, a refusal included | a toast that stays until it is dismissed, with "Details" (the owner on #215, 2026-11-26) |
| An address that does not exist | the error page |
| A fault in a view | the error page, with "Reload" and the message behind "Details", without a stack trace |

- **The error page replaces the page area only.** The navigation and the host bar stay, so the owner keeps the way out and still sees the VMs. A full-screen error page appears only when the shell itself fails. Both are in English and German.
- **A view names only the refusals it words itself**, such as `domain_suspicious`, `force_required` or the field of a Setting; everything else takes the default of its kind.
- **A view may declare a read an extra.** Its failure then shows nothing there: the mark for a missing Installation beside a VM's GitHub owners, the longest duration offered for Open web.
- **"Details" shows the Host's own English text of a failure**, folded away and copyable, once it is checked that this text cannot carry a secret (#215). The check found that the backend words `message` itself, while `detail` may pass on another program's output, from inside a VM too; Details shows the code and `message`, not `detail` (#224, `docs/control-room-api.md`, "Errors").
- **A fault of the UI stays in the browser.** Nothing is reported to the Host.

### Tests

The UI's tests keep driving it as the backend would, through the stand-ins for `fetch` and `EventSource` (`web/ui/test/fakes.ts`). TanStack Query hands results to views through a timer by default, where the tests wait for promises; that is settled in one place, in the app itself, so tests and production take the same path, and a single test changes only where that is not enough.

## Consequences

- A new view of data that another view already shows asks for the resource by name and gets its freshness and its failure handling with it.
- A new Activity code that changes a resource needs a row in the module's table, entered by hand. A row that is missing shows old data until the next reconnect or reload. Whether the Host should name what an entry makes stale is open (#219).
- A change on the Host costs one `GET` per stale resource that a view shows. The backend's events stay as they are.
- `@tanstack/vue-query` and the packages it brings enter `web/ui/package-lock.json`. They run where the UI is built and tested, in CI and in a VM ([ADR 0011](0011-the-control-room-ui-is-built-in-ci.md)); the Host still has no Node ([ADR 0003](0003-no-node-on-the-host.md)).
- The Content-Security-Policy stays as it is, `'self'` plus the hashes of the inline `<script>` and `<style>` of `index.html`: nothing in this adds a `<style>` or `<script>` element at runtime.
- The views lose their own load functions, request counters, code lists and the events that tell a page to load again; `useReloadOnActivity` and `useLiveEvent` go when their last callers do.

## Considered Options

- **The same module without the dependency**, or TanStack Query only after a spike had shown the existing tests stay quiet with it: rejected by the owner. TanStack Query is set, and what the tests need is fixed on the way.
- **TanStack Query in each view, without a module**: rejected. Paths, answer types and the lists of Activity codes would stay spread over the views, which is the friction this record removes.
- **Pushed data stays in `app/utils/live.ts`**: rejected by the owner. Server data would keep two homes, and a view would have to know which of the two a piece of data comes from, although each pushed state also has a `GET`.
- **Patching fetched lists from Activity entries**: rejected, see "What an event does".
- **Events that carry whole objects for more resources**: not now, and not rejected. Settings, Forbids, the Watchlist kept by hand, Legitimate domains and Harmless marks look like plain records that `agentbox watch` could compare and send as it does a VM; the GitHub App overview asks GitHub, pairings are read inside the VM, and the blocked list, Alerts, GitHub tokens, Activity and a VM's log are computed from logs or depend on what the view asked for. It would shorten the table by about half and keep both mechanisms. Moving a resource from one column to the other later changes its row and no view.
- **Reading again by the clock or on window focus**: rejected. The stream already says what changed; a clock would add requests and a second, vaguer rule.
- **A toast for every failure**: rejected. A refusal of a form belongs beside the field that can put it right.
- **A refusal without a form beside the button that sent it**: rejected by the owner on #215. It would need a place of its own in every view; the toast that stays says it with Details.
- **Reporting faults of the UI to the Host**: rejected. It would be a new endpoint that every signed-in page can write to.
