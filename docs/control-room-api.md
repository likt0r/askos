# Control Room API v1

The backend (`web/agentbox-web`) serves the new Control Room UI and this API on
`https://<host>:8800`. Every path below starts with `/api/v1`. The unversioned endpoints
(`/api/status`, `/api/<vm>/<action>`, ...) stay for the old UI until the new one replaces
it; new code uses only v1.

Vocabulary as in [GLOSSARY.md](../GLOSSARY.md). How the backend works:
[ARCHITECTURE.md](../ARCHITECTURE.md#control-room).

## Conventions

- **JSON** in and out (`Content-Type: application/json`). Request bodies are JSON objects of
  at most 8 KiB. Times are ISO 8601 in UTC (`2026-11-03T12:00:00Z`) unless a field says
  otherwise.
- **Sign-in.** A session cookie `agentbox` (`Secure; HttpOnly; SameSite=Strict`), set by
  `POST /session`. Every endpoint except `POST /session` and `DELETE /session` needs it,
  otherwise `401 unauthenticated`. The session lasts `session_hours` of the Settings.
- **CSRF.** Every request other than `GET` must carry the header `X-Agentbox: 1`, otherwise
  `403 csrf_header_missing` (the body is not read).
- **Actor.** Each change is recorded in Activity with the browser's device (from its
  User-Agent, e.g. `Firefox/Linux`) and address; nothing to send for that.
- **Results.** Most endpoints return what the matching `agentbox <command> --json` prints,
  unchanged (named per endpoint below). Where that output has a `message` field, it is
  agentbox's own English text for people at a terminal, not translated; the UI should
  build its own text from the structured fields.
- **Secrets.** Claude tokens and the GitHub App key go only in request bodies, are passed
  to `agentbox` on stdin and never appear in a response, an error or a log. The Pairing code
  appears only in the response of `POST /vms/<vm>/pairing-code`. Events never carry any of
  them.

### Errors

Every error has the same shape and an HTTP status that fits:

```json
{"error": {"code": "invalid_setting", "message": "Invalid value for a setting",
           "field": "reset_time", "detail": "reset_time: invalid value 99:99 (allowed: ...)"}}
```

- `code`: stable, snake_case; the UI translates it. New codes may be added.
- `message`: short English text, a fallback when the UI has no translation.
- `field` (optional): the request field that is wrong.
- `params` (optional): the parameters the UI words a refusal with, for the codes that have
  them: `invalid_pattern` (`reason`) and `pattern_too_broad` (`host`), each only in the form
  agentbox gives it.
- `detail` (optional): agentbox's own message (English, untranslated, for diagnosis);
  never shown as the main text.

Codes from the backend itself:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_body` | body is not a JSON object |
| 413 | `body_too_large` | body over 8 KiB |
| 400 | `invalid_field` | a field has the wrong type or form (`field` names it) |
| 400 | `invalid_query` | a query parameter appears twice, or too many |
| 400 | `invalid_name` | VM name not `^[a-z][a-z0-9-]{1,15}$` |
| 400 | `confirmation_required` | deleting, revoking pairings or revealing the Pairing code without the confirmation |
| 401 | `unauthenticated` | no valid session |
| 401 | `wrong_password` | sign-in with a wrong password |
| 403 | `csrf_header_missing` | `X-Agentbox: 1` missing |
| 404 | `not_found` | no such endpoint |
| 405 | `method_not_allowed` | endpoint exists, other method (`Allow` header lists them) |
| 429 | `too_many_attempts` | login brake: five failures in 15 minutes from one address (`Retry-After` in seconds) |
| 503 | `too_many_streams` | eight live connections are open already (`Retry-After: 10`) |
| 503 | `readings_unavailable` | host readings are not available |
| 502 | `bad_agentbox_answer` | agentbox printed something that is not JSON |
| 504 | `timeout` | agentbox did not finish in time |
| 500 | `internal_error` | a bug in the backend |

Codes for agentbox's refusals. agentbox names the code itself in its `--json` failure
(below); the backend maps it to the status with a table, never by the wording
([ADR 0012](adr/0012-the-server-speaks-english-with-stable-codes.md)); a code it does not
know is `500 action_failed`. Without a code it goes by the exit code (2
`invalid_argument`, 3 `vm_not_running`, 4 `guest_agent_unavailable`, 5
`vm_query_timeout`, 6 `vm_journal_failed`); sudo's own refusal is `sudo_failed`, anything
else `500 action_failed`, with the message in `detail`:

| Status | Code | Meaning |
|---|---|---|
| 404 | `vm_not_found` | no such VM |
| 409 | `vm_exists` | a VM of that name exists already |
| 400 | `name_reserved` | name reserved for the CLI or the Control Room |
| 409 | `vm_being_deleted` / `vm_leftovers` | an unfinished delete, or leftovers of an earlier VM of that name |
| 409 | `no_free_network` | all 254 networks in use |
| 409 | `vm_not_running` | the VM is not running (logs) |
| 409 | `vm_paused` | the VM is paused: start it first (changing the memory limit) |
| 400 | `invalid_memory` | memory limit not a whole number from 1024 MiB to the Host's memory (`field`: `memory_mib`), when changing the limit or creating a VM (`POST /vms` formerly answered `invalid_argument` for this) |
| 500 | `memory_not_applied` | the new memory limit is saved, but the VM could not be defined again; `agentbox regenerate` finishes it |
| 503 | `guest_agent_unavailable` | the VM's guest agent does not answer |
| 504 | `vm_query_timeout` | a query inside the VM took too long |
| 502 | `vm_journal_failed` | journalctl failed inside the VM |
| 503 | `orca_unavailable` / `pairing_code_unavailable` | Orca in the VM does not answer / printed no Pairing code yet |
| 409 | `vm_not_off` | Reset: the VM was running again before its System disk was replaced; the disk is unchanged |
| 500 | `reset_start_failed` | Reset: the System disk is fresh, but the VM did not start |
| 500 | `build_mismatch` | Reset: the VM ran on another Build than its new System disk and was forced off; reset again |
| 501 | `unpair_unsupported` | Orca cannot remove a single device: revoke all pairings instead |
| 409 | `observer_not_removable` | the Observer cannot be removed |
| 404 | `device_not_found` | no such Paired device |
| 409 | `no_pairings` | nothing to revoke |
| 409 | `domain_suspicious` | strong Signals: only the CLI can allow it (`--force`) |
| 404 | `allowlist_entry_not_found` | no such Allowlist entry |
| 409 | `allowlist_entry_permanent` | extending a permanent entry |
| 409 | `force_required` | the change would lift strong Signals: only the CLI can make it (`--force`) |
| 409 | `watchlist_recreate_force_required` | adding an entry would create a missing Watchlist again, which reopens Open web: only the CLI can do it (`--force`) |
| 409 | `builtin_entry` | a built-in Legitimate domain cannot be changed |
| 404 | `watchlist_entry_not_found`, `legitimate_domain_not_found` | no such Watchlist entry or Legitimate domain |
| 409 | `legitimate_unreadable` | the record of the Legitimate domains cannot be read (`legitimate.json` broken, also in a single entry): the Legitimate-domain views and changes answer this until it is fixed on the Host; meanwhile the proxy counts its last good state, or only the built-in ones |
| 409 | `migration_required` | the text files from before hold lines the records cannot take (an unknown category, an invalid name): a change would drop them unseen, so `agentbox migrate-lists` on the Host has to take them over first (it names them) |
| 409 | `watchlist_unreadable` | the hand-maintained Watchlist cannot be read (`watchlist.json` broken, also in a single entry, or missing without the text file from before): the proxy keeps Open web closed. Watchlist changes, allowing without `--force` and opening the web answer this until it is fixed on the Host; recreating a missing one is `409 watchlist_recreate_force_required`. `GET /watchlist` answers with `unreadable` instead, and the Feeds' state |
| 400 | `invalid_domain`, `invalid_minutes`, `invalid_note`, `invalid_duration`, `invalid_category`, `invalid_source` | values agentbox refused |
| 400 | `pattern_confirmation_required` | adding a regex Allowlist entry without confirming what its preview showed: `POST /allowlist` without `confirm: true`, refused by the backend itself with `field`: `confirm`; the same code from agentbox (`allow --regex` without `--confirm-pattern`) comes without a `field` |
| 400 | `invalid_pattern` | the pattern of a Regex entry breaks a rule ([ADR 0013](adr/0013-regex-entries-in-every-list.md)): too long, not printable ASCII without spaces, no regular expression, an uppercase letter, a backreference, a lookaround, a quantifier or alternative inside a repeated group, or too many quantifiers in a row that can take the same characters (agentbox names which as the parameter `reason`: `too-long`, `characters`, `syntax`, `uppercase`, `backreference`, `lookaround`, `nested-quantifier`, `too-complex`) |
| 400 | `pattern_too_broad` | the pattern matches a host of the fixed sample of unrelated hosts (agentbox names it as the parameter `host`); every list but the Watchlist |
| 400 | `unknown_setting`, `invalid_setting` | Settings (`invalid_setting` has `field`: the key whose value is wrong) |
| 404 | `image_not_found`, `build_not_found` | no such Image or Build |
| 404 | `containerfile_not_found` | the installed Release has no Containerfile for this Image (it no longer defines it, or its files are not there) |
| 400 | `invalid_image`, `invalid_build` | malformed name or Build id |
| 409 | `image_has_no_build` | the Image has no Build yet |
| 409 | `build_running` | a Build of this target runs already |
| 409 | `update_running` | an update of every Image (Update now, or the automatic one) runs already |
| 409 | `vm_uses_other_image`, `vm_pinned` | an Update cannot reset this VM: it uses another Image, or is pinned to a Build (its Reset would not bring the new one) |
| 409 | `not_set_up` | something on the Host is not set up yet (`agentbox regenerate`) |
| 422 | `claude_token_rejected` | Anthropic rejected the token; the old one stays |
| 502 | `anthropic_unavailable` | Anthropic answered unexpectedly; nothing changed |
| 400 | `invalid_claude_token` | not a token from `claude setup-token` |
| 422 | `github_app_rejected` | GitHub rejected App ID or key; nothing changed |
| 502 | `github_unavailable` | GitHub not reachable or answered with an error |
| 400 | `invalid_private_key`, `invalid_app_id` | not the App's `.pem` / not an App ID |
| 409 | `github_app_missing` | no GitHub App set up |
| 404 | `github_app_not_found` | no GitHub App with this App ID set up (`DELETE /github-app/<id>`; parameter `app_id`) |
| 400 | `invalid_github_owner`, `github_owner_required` | GitHub owners |
| 502 | `journal_failed` | reading the Host's journal failed |
| 404 | `folder_not_found` | `agentbox import` (CLI only): no such folder on the Host |
| 400 | `not_a_folder` | `agentbox import`: the path is not a folder |
| 400 | `linked_worktree` | `agentbox import`: the folder is a linked worktree (`.git` is a file); its main repository can be imported |
| 409 | `target_exists` | `agentbox import`: the target under `~/code` exists in the VM; nothing was changed |
| 409 | `inbox_missing` | `agentbox import`: the VM's Build lacks the wrapper that unpacks it; a new Build and a Reset bring it |
| 500 | `import_failed` | `agentbox import`: packing, attaching or unpacking failed; the disk is detached and the tar file deleted |
| 409 | `container_image_cleanup_missing` | the Container image cleanup (hourly, `agentbox cleanup-container-images`, in Activity only): the VM's Build lacks its wrapper; a new Build and a Reset bring it |
| 502 | `container_image_cleanup_failed` | the Container image cleanup in the VM failed or answered something other than its two numbers (in Activity only) |
| 400 | `invalid_argument` | any other value or option agentbox refused |
| 500 | `sudo_failed` | the backend may not run agentbox (sudo rule missing, or agentbox did not run as root) |
| 500 | `action_failed` | anything else agentbox reported (e.g. libvirt failing, a broken file on the Host); also the code of every failure only the Host can cause (setup, regenerate, timers) |

### agentbox's failures

Called with `--json`, as the backend always calls it, a failing `agentbox` command
writes one JSON object as a line on stderr, instead of its text `agentbox: <message>`,
and exits with the same exit code as without `--json`:

```json
{"error": "vm_not_found", "message": "No such VM: lab", "params": {"vm": "lab"}}
```

- `error`: the code, set where the failure arises (`agentbox/errors.py`). Codes are
  groups, not one per place: those above, plus `action_failed` for everything else.
- `message`: English text for people, not translated. For a failure that is another
  program's own error output (`virsh`, `qemu-img`), that output.
- `params`: values for programs, such as `vm`, `domain`, `image`, `setting` or `option`;
  never a secret. The backend takes `field` from them (`setting` of `invalid_setting`).

Lines before it may come from programs agentbox ran, and a warning about journal entries
left out may follow it as text, so the backend reads the last line of stderr that is such
an object. A new failure the Control Room can trigger needs a code in this table, in the
backend's table (`AGENTBOX_ERRORS` in `web/agentbox-web`) and in both locale files of the
UI (`errors.*`); `tests/test_control_room_v1.py` checks that they agree.

## Session

| Method and path | Body | Response |
|---|---|---|
| `POST /session` | `{"password": "..."}` | `200 {"authenticated": true, "expires": "<time>", "default_language": "browser"\|"en"\|"de"}` and the cookie; `401 wrong_password`, `429 too_many_attempts` |
| `GET /session` | | `200 {"authenticated": true, "expires": "<time>", "default_language": "browser"\|"en"\|"de"}`, else `401 unauthenticated` |
| `DELETE /session` | | `200 {"authenticated": false}`, cookie cleared, live streams of the session end |
| `GET /version` | | `200 {"api": 1, "version": "<16 hex>", "ui": "nuxt"\|"legacy", "installed": "<time>"\|null, "release": "<MAJOR.MINOR.PATCH>"\|null}` (`version` is the UI build, which open pages compare to offer a reload; `release` is the Release of Agent Box, the same as `agentbox version`) |

`default_language` is the Setting as the backend reads `/etc/agentbox/settings.json` at
that request (`browser` when the file has no valid value), so the UI knows it right after
sign-in and on every page load without `GET /settings`.

Sign-ins, failures, refusals by the brake and sign-outs are Activity entries (codes
`signin`, `signin.failed`, `signin.locked`, `signout`).

## VMs

### The VM object

`GET /vms`, `GET /vms/<vm>` and the `state` and `vm` events use the same object
(`agentbox describe --json`):

```json
{
  "name": "work",
  "state": "running",
  "libvirt_state": "running",
  "network": 1, "orca_port": 6801, "address": "10.77.1.2",
  "memory_mib": 26624, "data_disk_gib": 300,
  "github_owners": ["likt0r"], "created": "2026-11-03T12:00:00Z",
  "image": "base", "build_pin": null,
  "image_build": "20261101-120000", "next_build": "20261101-120000",
  "system_disk": {"since": "2026-10-31T09:12:00Z", "age_days": 3, "file": "base-20261020.qcow2",
                  "image": "base", "build": "20261020"},
  "reset_changes_build": true,
  "reset_due": false,
  "agents": {"working": 1, "waiting": 2},
  "responding": {"guest_agent": null, "orca": true, "since": null},
  "failed_wakes": {"count": 0, "last": null, "address": null},
  "open_web_until": null,
  "claude_token_since": "2026-10-01T08:00:00Z",
  "devices": [{"address": "192.0.2.36", "connections": 2}]
}
```

- `state`: `running`, `starting`, `not_responding`, `paused` (a Pause: memory saved to
  disk), `stopped`, `suspended` (paused by libvirt, not by Agent Box), `shutting_down`,
  `crashed` or `unknown`; `libvirt_state` is virsh's word, `null` when libvirt does not
  know the VM.
- `starting` and `not_responding` are VMs libvirt reports as running whose Orca does not
  answer (accept a connection on its port in the VM). `starting`: the VM's QEMU process
  started less than the wake time limit (120 s) ago and Orca has not answered since; it
  stays `starting` past the limit until a check after it failed too (the stream checks
  then). `not_responding`: the VM started longer ago than that and its Orca has not
  answered for at least as long: since the start, or, once Orca answered, from the first
  failed check after that until a later failed check, or since a wake that gave up began
  to wait (it waited the whole time limit). Once Orca answered, a single failed check does
  not make a VM `not_responding`. A VM whose Orca has not answered since its start counts
  from its start when it was found silent within the time limit after it (the stream looks
  at a VM as it starts; a wake that started it waits from then); one failed look at a VM
  nobody watched start (it ran before an update) is not enough. It stays `not_responding`
  until Orca answers again or the VM starts again. Restart first; Reset if it stays like this. Both states
  offer Restart and Reset, not Start.
- `responding`: what answers while libvirt reports the VM as running; `null` when the VM
  is not running or was not asked yet. `orca`: whether Orca answers. `guest_agent`:
  whether the guest agent answers a `guest-ping`, asked only when Orca does not answer
  (`null`: not asked); `false` means the system did not boot, `true` that it runs but
  Orca does not. `since`: since when Orca does not answer as far as known, `null` while
  it answers. `agentbox describe` (and so `GET /vms` and `GET /vms/<vm>`) asks once;
  `agentbox watch` (the stream) asks every 15 s. Both take what the wakes and the stream
  found in the VM's start (when Orca last answered, when it fell silent after that, whether
  the VM was found not responding) from the VM's wakes file, so they judge alike and differ
  only in how fresh their last look is; a restarted `agentbox watch` knows what the one
  before found.
- `failed_wakes`: connections to the VM's Orca port (and `agentbox observe`) whose wake
  gave up because Orca did not answer within the time limit, counted since the VM's last
  start: `count`, `last` (time of the last one) and `address` (its remote address, `null`
  when unknown). A wake that waited while the VM stopped or started again, or while Orca
  answered another wake or the stream, is not counted.
  A wake that reaches Orca, a start, and Restart, Reset, Pause and delete once the VM is
  off set `count` back to 0; a VM that stopped on its own keeps the count of its last
  start until it starts.
- `image_build`: the Image's current Build; `build_pin`: the Build the VM is pinned to;
  `next_build`: what the next Reset uses (the pin, else the current Build).
- `system_disk`: when the System disk was created (its last Reset) and from which Build;
  all `null` without a disk. `reset_changes_build`: the next Reset brings a different
  Build or Image. `reset_due`: the daily Reset check would reset the VM: its System disk
  is at least `reset_age_days` old, or, with `auto_update` on, its Image has a newer current
  Build than the one its System disk came from and the VM has no Build pin (the automatic
  Reset happens when the VM is idle).
- `agents`: agent states from Orca with counts (`working`, `waiting`, `done`, ...); `{}`
  when no agent runs or the VM is not running; `null` when unknown (Orca not answering, no
  Observer).
- `devices`: open connections to the VM's Orca port by remote address; `null` when unknown.
- `memory_mib`: the memory limit in the registry record; after a change a running VM uses
  it from its next Restart or Reset (`PUT /vms/<vm>/memory`).
- Memory in use is not part of it: `vms.<name>.memory_bytes` of the `host` event and of
  `GET /host`.

### Endpoints

| Method and path | Body / query | agentbox call, response |
|---|---|---|
| `GET /vms` | | `describe --json`: `{"vms": [<VM>...], "problems": [{"vm", "problem"}]}` (VMs in network order; `problems`: broken records) |
| `GET /vms/<vm>` | | `describe <vm> --json`: `<VM>` |
| `POST /vms` | `{"name", "image"?, "memory_mib"?, "data_disk_gib"?, "github_owners"?: [..], "copy_allowlist_from"?}` | `create`: `201 {"vm", "network", "orca_port", "image", "memory_mib", "data_disk_gib", "github_owners", "allowlist_from", "message"}`; omitted or `null` fields take the defaults of the Settings |
| `DELETE /vms/<vm>` | `{"confirm": "<vm>", "delete_data_disk"?: false}` | `delete`: `{"vm", "data_disk": "archived"\|"deleted", "archive", "resumed", "message"}` |
| `POST /vms/<vm>/start` | | `start`: `{"vm", "state", "message"}` (`state` here is the CLI's: `running`, `paused`, `shut off`, ...) |
| `POST /vms/<vm>/pause` | | `pause`: same shape |
| `POST /vms/<vm>/restart` | | `restart`: same shape |
| `POST /vms/<vm>/reset` | `{"start"?: true}` | `reset` (`--no-start` with `start: false`): same shape. The UI asks for confirmation first |
| `POST /vms/<vm>/focus` | | `focus <vm>`: `{"focus", "others": [{"vm", "state"}], "message"}` |
| `DELETE /focus` | | `focus off` |
| `PUT /vms/<vm>/memory` | `{"memory_mib": 16384}` | `set-memory`: `{"vm", "memory_mib", "previous_mib", "changed", "state", "applies": "next_start"\|"next_restart"\|null, "message"}`; `next_start`: the VM is off and starts with it, `next_restart`: it keeps running with its memory until its next Restart or Reset; `null`: nothing changed. A paused VM is refused (`409 vm_paused`) |
| `PUT /vms/<vm>/image` | `{"image", "build": null\|"<build>"}` | `set-image`: `{"vm", "image", "build", "message"}`; applies at the next Reset |
| `PUT /vms/<vm>/github-owners` | `{"owners": ["likt0r", ...]}` | `set-owners`: `{"vm", "owners"}` |
| `PUT /vms/<vm>/claude-token` | `{"token": "sk-ant-oat..."}` | `set-claude-token` (token on stdin, checked with Anthropic before it replaces the old one): `{"vm", "stored": true, "length"}` |
| `POST /vms/<vm>/open-web` | `{"minutes": 30}` | `open-web`; while the Watchlist cannot be read: `409 watchlist_unreadable` |
| `DELETE /vms/<vm>/open-web` | | `close-web` |
| `GET /vms/<vm>/logs` | `?unit&priority&since&until&boot&grep&lines&after_cursor` | `logs`: `{"vm", "entries": [{"time", "unit", "identifier", "priority", "message", "boot", "cursor"}], "cursor", "truncated"}`; poll with `after_cursor=<cursor>` for new lines |
| `GET /vms/<vm>/blocked` | | `blocked`: `[{"domain", "count", "first", "last", "reason", "code", "params", "suspicious"}]` (last 24 hours, epoch seconds). `reason`: why the proxy blocked it, English; `code`: the same as a stable code, `null` for a reason agentbox does not know: `not-allowed` (not on the Allowlist), `suspicious` (strong Signals, where only Open web or a regex Allowlist entry would let it through; `params.signals`, joined with `", "`), `watchlist-unreadable` (Open web, or a regex Allowlist entry, closed while the Watchlist cannot be read), `not-public` (no public address), `not-tls` and `no-server-name` (a connection the proxy took no destination from: what the VM sent was no TLS handshake, or a ClientHello without a server name; `domain` is the client's address then, in effect the VM's); `suspicious`: the Signals of its last Alert |
| `GET /vms/<vm>/alerts` | | `alerts`: `[{"domain", "tags", "count", "first", "last", "seen", ...}]` |
| `POST /vms/<vm>/alerts/seen` | `{"domain"?}` | `mark-seen`: all Alerts of the VM, or those of one destination |
| `GET /vms/<vm>/allowlist` | | `allowlist <vm>`: the entries that apply to the VM (see Allowlist) |
| `GET /vms/<vm>/github-tokens` | `?since&until&lines` | `github-tokens`: `{"vm", "repositories": [{"repository", "tokens", "last_token", "refused", "last_refused"}], "requests": [{"time", "outcome": "issued"\|"refused"\|"error", "repository", "request", "reason", "reason_code", "reason_params", "expires_at"}], "truncated", "retention_days"}`: repositories the VM got tokens for and refused requests. `reason`: why, as the token service said it (English); `reason_code`: a refusal's reason as a stable code, else `null`, and `reason_params` its parameters, `{}` without (see [GitHub tokens in Activity](#github-tokens-in-activity)) |
| `GET /vms/<vm>/pairings` | | `pairings`: `{"vm", "devices": [...], "observer_known", "single_removal": false}` |
| `POST /vms/<vm>/pairing-code` | `{"confirm": true}` | `pair`: `{"vm", "code": "orca://pair?code=..."}`; starts the VM if needed |
| `POST /vms/<vm>/pairings/remove` | `{"device"}` | `unpair`; today always `501 unpair_unsupported` (Orca cannot) |
| `POST /vms/<vm>/pairings/revoke` | `{"confirm": "<vm>"}` | `revoke-pairings`: `{"vm", "observer_repaired", "message"}` |

Create and delete enforce the same rules as the CLI: agentbox checks the name, the
reserved words, the Image and the sizes; deleting needs the typed name.

## Allowlist

`<scope>` is `all` (every VM) or a VM's name. Durations: `15min`, `1h`, `8h`, `7d`;
`permanent` (or omitted) for a permanent entry, `default` for the default duration of
temporary entries (Settings).

| Method and path | Body / query | agentbox call, response |
|---|---|---|
| `GET /allowlist` | `?vm=<vm>\|all` | `allowlist`: `[{"domain", "regex", "scope", "expires", "note", "added_by", "added", "expired", "remaining", "uses_24h", "refused"}]` (`domain` or, for a Regex entry, `regex`: the pattern as stored; the other `null`. `refused`: why the rules refuse a stored pattern, which then allows nothing until it is removed, else `null`. `expires` and `added` epoch seconds, `expires` `null` when permanent, `remaining` seconds, `uses_24h` `null` when unknown) |
| `POST /allowlist` | `{"scope", "domain", "duration"?, "note"?}` or `{"scope", "regex", "confirm": true, "duration"?, "note"?}` | `allow`: `201`; destinations with strong Signals: `409 domain_suspicious`; any destination while the Watchlist cannot be read: `409 watchlist_unreadable` (also for `extend` and `make-permanent`). With `regex` a Regex entry (`allow --regex --confirm-pattern`): only with `confirm: true`, after the preview of `GET /regex/check` (else `400 pattern_confirmation_required`, `field`: `confirm`); a pattern the rules refuse: `400 invalid_pattern` or `400 pattern_too_broad`. It never allows a destination with strong Signals (the proxy checks every connection), so it is never `domain_suspicious` |
| `DELETE /allowlist/<scope>/<domain>` | | `disallow` (open tunnels to it are closed) |
| `POST /allowlist/<scope>/<domain>/extend` | `{"duration": "1h"}` | `extend` |
| `POST /allowlist/<scope>/<domain>/make-permanent` | | `make-permanent` |
| `PUT /allowlist/<scope>/<domain>/note` | `{"note"}` | `set-note` |
| `DELETE /allowlist/<scope>/regex?regex=<pattern>` | | `disallow --regex`: the Regex entry with exactly this pattern (open tunnels it allowed are closed) |
| `POST /allowlist/<scope>/regex/extend?regex=<pattern>` | `{"duration": "1h"}` | `extend --regex` |
| `POST /allowlist/<scope>/regex/make-permanent?regex=<pattern>` | | `make-permanent --regex` |
| `PUT /allowlist/<scope>/regex/note?regex=<pattern>` | `{"note"}` | `set-note --regex` |
| `GET /regex/check?list=<list>&regex=<pattern>&host=<host>&scope=<vm\|all>` | `list`: `allowlist`, `watchlist`, `legitimate` or `harmless`; `host` and `scope` (`allowlist` only, default `all`) optional | `check-regex`: `{"list", "regex", "scope", "matches": {"<host>": bool}, "preview": null \| {"hosts": [{"host", "allowed", "blocked", "signals", "strong"}], "more", "watchlist_unreadable", "truncated", "reaches_back", "log_unreadable"}}`: whether the pattern can be an entry of that list (else `400 invalid_pattern` or `400 pattern_too_broad`, as adding it would answer), whether `host` matches it, and for the Allowlist the preview: the hosts of the scope's proxy log of the last 7 days (`ALLOWED` and `BLOCKED` lines) it matches, how often each was allowed and blocked, its Signals and whether a strong one keeps it blocked; `more`: hosts beyond the 100 shown; `watchlist_unreadable`: why the Watchlist cannot be read (then the pattern allows nothing), else `null`; `truncated`: the log had more than the 200,000 lines read (a VM can fill its log), and `reaches_back` (epoch seconds) how far back these go, else the start of the 7 days; `log_unreadable`: why the proxy log could not be read (`hosts` then empty), else `null`. Changes nothing |

A Regex entry ([ADR 0013](adr/0013-regex-entries-in-every-list.md)) is a pattern in place of the
domain, in every list: in a body `regex` instead of `domain`, in a path the segment `regex` with
the pattern as the query parameter `regex` (percent-encoded; a domain is never the bare label
`regex`). The pattern is taken as given, not lowercased; only leading and trailing spaces, tabs, CR and LF are dropped, and anything but printable ASCII without spaces is `400 invalid_field` (`field`: `regex`). It matches the whole host name, no
subdomains of its own accord; the rules it has to follow are in the ADR.

## Watchlist

| Method and path | Body / query | agentbox call, response |
|---|---|---|
| `GET /watchlist` | | `watchlist`: `{"entries": [{"domain", "regex", "category", "strong", "source", "note", "added", "added_by", "refused"}], "unreadable", "feeds": {"threat-intelligence"\|"newly-registered": {"present", "entries", "updated", "attempted", "error", "days"}}, "feed_interval_hours", "young_domain_days"}`: the hand-maintained Watchlist (`domain`, or `regex` for a Regex entry, the other `null`; `refused`: why the rules refuse a stored pattern, else `null`: it raises no Signal, so `unreadable` says why Open web stays closed until it is removed, while the entries still show; `source`: the incident or `""`; `strong`: the category keeps it blocked in Open web; `unreadable`: why it cannot be read, `entries` then empty, else `null`) and the Feeds' state (`updated`/`attempted` epoch seconds or `null`, `error` of the last fetch or `null`) |
| `GET /watchlist/check?domain=<domain>` | | `check-domain`: `{"domain", "signals": [{"signal", "strong", "suppressed", "reason", "code", "params"}], "alert", "strong", "harmless", "harmless_regex", "legitimate", "legitimate_regex", "feeds"}`: the Signals of a destination, each with why it applies (below); `harmless` and `legitimate`: the Harmless mark and the Legitimate domain that cover it, or `null`, each a domain or, with `*_regex` `true`, the pattern of a Regex entry (a Legitimate pattern lifts only weak Signals) |
| `GET /watchlist/harmless` | | `harmless`: `[{"domain", "regex", "refused"}]`, the Harmless marks: a domain, or the pattern of a Regex entry (the other `null`); `refused`: why the rules refuse a stored pattern, which then silences nothing until it is removed, else `null` |
| `PUT /watchlist/harmless/<domain>` | | `mark-harmless` |
| `DELETE /watchlist/harmless/<domain>` | | `unmark-harmless` |
| `PUT /watchlist/harmless/regex?regex=<pattern>` | | `mark-harmless --regex`: a pattern the rules refuse is `400 invalid_pattern` or `400 pattern_too_broad` |
| `DELETE /watchlist/harmless/regex?regex=<pattern>` | | `unmark-harmless --regex` |
| `POST /watchlist/entries` | `{"domain", "category", "source"?, "note"?}` or, for a Regex entry, `{"regex", "category", "source"?, "note"?}` | `watch-add` (`--regex` for a pattern): `201` with the entry; an existing entry is changed. `category`: `exfil`, `c2`, `tunnel`, `paste`, `phishing`, `blockchain` (strong) or `payload`. Lowering a strong entry to `payload`: `409 force_required`; adding one while `watchlist.json` is missing (it would recreate the Watchlist and open Open web again): `409 watchlist_recreate_force_required`. A pattern gets no breadth check here (a broad one only raises false Alerts); one the other rules refuse is `400 invalid_pattern` |
| `DELETE /watchlist/entries/<domain>` | | `watch-remove`: `{"domain", "regex", "removed"}`; an entry of a strong category: `409 force_required` |
| `PUT /watchlist/entries/<domain>/note` | `{"note"}` | `watch-set-note` |
| `DELETE /watchlist/entries/regex?regex=<pattern>` | | `watch-remove --regex`: `{"domain", "regex", "removed"}`; one of a strong category: `409 force_required` |
| `PUT /watchlist/entries/regex/note?regex=<pattern>` | `{"note"}` | `watch-set-note --regex` |
| `GET /watchlist/legitimate` | | `legitimate`: `[{"domain", "regex", "note", "added", "added_by", "builtin", "refused"}]` (`domain`, or `regex` for a Regex entry, the other `null`; `refused`: why the rules refuse a stored pattern, which then lifts nothing until it is removed, else `null`), built-in ones with `added` and `added_by` `null` |
| `POST /watchlist/legitimate` | `{"domain", "note"?}` or, for a Regex entry, `{"regex", "note"?}` | `legitimate-add` (`--regex` for a pattern): `201`; a domain with strong Signals, or with threat-intelligence entries at or below it (also ones another Legitimate domain lifts now), or any new one while the threat-intelligence list has not been fetched: `409 force_required`. A pattern lifts only weak Signals, so it is never `force_required`; one the rules refuse is `400 invalid_pattern` or `400 pattern_too_broad` |
| `DELETE /watchlist/legitimate/<domain>` | | `legitimate-remove`: `{"domain", "regex", "removed"}`; built-in ones: `409 builtin_entry` |
| `PUT /watchlist/legitimate/<domain>/note` | `{"note"}` | `legitimate-set-note` |
| `DELETE /watchlist/legitimate/regex?regex=<pattern>` | | `legitimate-remove --regex`: `{"domain", "regex", "removed"}` |
| `PUT /watchlist/legitimate/regex/note?regex=<pattern>` | `{"note"}` | `legitimate-set-note --regex` |

Changes that would lift strong Signals need `--force`, which only the CLI on the Host passes
([ADR 0009](adr/0009-watchlist-and-legitimate-domains-live-on-the-host.md)).

### Why a Signal applies

Each Signal of `check-domain` names why it applies three ways (ADR 0012): `reason`, the
classifier's English text, also what the CLI shows; `code`, the kind of reason, stable; and
`params`, its parameters, all text. The UI words the reason from `code` and `params` and shows
`reason` for a code or Signal it does not know, or when a parameter is missing. One Signal
can come from several kinds of reason (a Watchlist category, the blockchain pattern).

| `signal` | `code` | `params` | `reason` (English) |
|---|---|---|---|
| `metadata` | `metadata` | none | `address of a cloud metadata service` |
| `raw-ip` | `raw-ip` | none | `IP address instead of a domain` |
| the entry's category (`exfil`, `c2`, `tunnel`, `paste`, `phishing`, `blockchain`, `payload`) | `watchlist` | `domain` (the Watchlist entry), `source` (when it has one) | `Watchlist: webhook.site (Shai-Hulud 1 (09/2025))` |
| the entry's category | `watchlist-regex` | `pattern` (the Watchlist's Regex entry), `source` (when it has one) | `Watchlist: pattern ^npm-[a-z]+\.(com\|net)$ (lookalikes)` |
| `threat` | `threat-feed` | `domain` (the Feed's entry) | `HaGeZi Threat Intelligence Feeds: bad.example` |
| `young` | `young-feed` | `domain` (the Feed's entry) | `newly registered (HaGeZi NRD): fresh.example` |
| `lookalike` | `brand` | `brand`, `domain` (the registrable domain) | `brand name npm in the foreign domain npm-cache.com` |
| `lookalike` | `typo` | `target` (the imitated name), `distance` | `typo of github (distance 2)` |
| `tld` | `tld` | `tld` | `unusual TLD .xyz` |
| `random-label` | `random-label` | `label` | `random-looking or very long label 0123456789abcdef0123` |
| `many-labels` | `many-labels` | none | `more than six labels` |
| `blockchain` | `blockchain-pattern` | none | `pattern of a blockchain RPC service` |

The Signals `raw-ip`, `metadata`, `random-label` and `many-labels` were `roh-ip`, `metadaten`,
`zufallsname` and `viele-labels` before Release 0.3.0; Alerts and proxy lines from before keep
the old names, which the UI shows as they are.

## Activity

`GET /activity?kind=<k,k>&vm&since&until&search&lines` runs `agentbox activity --json`:
`{"entries": [{"time", "vm", "actor", "kind", "action", "outcome", "description", "code"?,
"params"?, "error"?, "error_params"?, "reason"?}], "truncated", "retention_days"}`, newest
first:

```json
{"time": "2026-11-09T12:00:00.000000Z", "vm": "work", "actor": "Firefox/Linux (192.0.2.23)",
 "kind": "allowlist", "action": "allow", "outcome": "failed",
 "description": "Allow evil.example temporarily: evil.example is suspicious (exfil). Allowing it only with --force",
 "code": "allow", "params": {"domain": "evil.example", "scope": "work", "temporary": "true"},
 "error": "domain_suspicious", "error_params": {"domain": "evil.example"}}
```

- `code`: the entry's stable code, the same as `action` (ADR 0012). The UI words the entry
  from it and `params` in its language. Entries recorded before Release 0.3.0 have no code
  and German text; the UI shows their `description`, as for any code it does not know.
  The token service's lines from before are not read at all.
- `params`: the action's parameters (table below), all text: a flag is `"true"` when set and
  missing otherwise, a list is joined with `", "`. Some come from what was asked for (a
  domain, the GitHub owners); a few only when they are a known name: the App ID only when
  it is one, a known Watchlist category, the known keys of Settings.
- `error`, `error_params`: a failure's error code and that error's parameters, as the CLI's
  `--json` failure names them ([agentbox's failures](#agentboxs-failures)), except a
  `category` or `setting` that is not a known one; also `interrupted` (the command was
  interrupted) and `unexpected_error` (`type`: an error of agentbox itself), which only
  Activity has. A failure whose step failed without an error code (a Build, a VM not
  responding) has none; its action says what failed, and `params.message` how, where
  agentbox said it (English).
- `description`: agentbox's English text, with a failure's message; the fallback for people.
- Every value, parameters and error parameters too, is one clean line with known token, key
  and Pairing-code formats hidden, as the description is; that is a safety net, not a
  promise that what was passed contains no secret.

| `code` | `params` |
|---|---|
| `start`, `wake`, `pause`, `restart`, `close-web`, `migrate-allowlist`, `set-claude-token`, `pair`, `observe`, `unpair`, `revoke-pairings`, `migrate`, `regenerate`, `setup` | none |
| `reset` | `no_start` |
| `focus` | `target` (a VM), or `off` |
| `create` | `image` (when given) |
| `delete` | `delete_data_disk` |
| `set-memory` | `mib` |
| `allow` | `domain` or, for a Regex entry, `regex` (the pattern; so for every command below that names `domain`), `scope` (a VM or `all`), `duration` (`15min`, `1h`, `8h`, `7d`) or `temporary`, `force` |
| `extend` | `domain`, `scope`, `duration`, `force` |
| `make-permanent` | `domain`, `scope`, `force` |
| `disallow`, `set-note`, `allowlist.expired` | `domain`, `scope` |
| `open-web` | `minutes` |
| `mark-seen` | `domain` (when one) |
| `mark-harmless`, `unmark-harmless`, `watch-set-note`, `legitimate-remove`, `legitimate-set-note` | `domain` |
| `watch-add` | `domain`, `category`, `force` |
| `watch-remove`, `legitimate-add` | `domain`, `force` |
| `migrate-lists` | the command none; each list taken over: `list` (`watchlist`, `legitimate`), `legacy` (the old file), `migrated` and `skipped` (numbers), or `created` |
| `set-owners` | `owners` |
| `set-github-app`, `remove-github-app` | `app_id` |
| `set-settings` | `settings` (the keys) |
| `build` | `image` or `all`, `reset` (the VMs of an Update) |
| `update-now` | `reset` |
| `set-image` | `image`, `build` (a Build pin) |
| `import` | `folder` (absolute, on the Host), `target` (under `~/code` in the VM, when valid); once it succeeded `files` and `bytes` (what the VM reports) |
| `signin`, `signin.failed`, `signin.locked`, `signout` | none (address and device are no parameters) |
| `alert` | `domain`, `signals` |
| `github.token` | `repository`, `expires` (GitHub's expiry, ISO 8601), `app_id` (the App that issued it; not in lines from before there were several Apps) |
| `github.refused` | `request` (as the VM sent it), `repository` (when the request names one), `reason` (a stable code, below) and its parameters (`status`, `app_id`) |
| `github.error` | `request`, `repository`, `message` (the token service's error, English) |
| `wake.not-responding` | `reason` |
| `reset.automatic`, `reset.postponed`, `reset.failed` | `reason`; onto a newer Build also `image`, `build` |
| `reset.update` | `image`; done: `build`, `started`; failed: `running`; never begun: `skipped`, `failed_builds` |
| `build.ok` | `image`, `build`, `release`, `removed` (old Builds), `reason` |
| `build.failed` | `image`, `build`, `all`, `reason`; `message` without an error code |
| `build.rejected` | `image`, `build`, `reason`, `message` (the first Safeguard problem) |
| `build.unneeded` | `reason` |
| `build.skipped`, `build.automatic` | `images`, `reason` |
| `container-image-cleanup.removed` | `removed`, `freed_bytes` (the VM's two numbers), `days` |
| `container-image-cleanup.failed` | `days` |

`reason` (also in `params`) is a stable code where one was
recorded, e.g. why an automatic Reset was postponed (`reset.postponed`: `gui-connected`,
`gui-unknown`, `agents-working`, `agents-unknown`), `update` for an automatic Reset onto a
newer Build of the VM's Image (`reset.automatic`) and for the start and the Builds of an
update (`build.automatic`, `build.ok`, `build.failed`, `build.rejected`, `build.unneeded`),
or what does not answer when a VM stopped responding (`wake.not-responding`, kind `action`,
outcome `failed`:
`system-not-booted`, `orca-not-answering`). That entry is recorded once per incident, not
for every connection, by whichever finds the VM `not_responding` first: a wake that gave
up (actor `Orca (<address>)`) or `agentbox watch` (actor `watch`), also when no Orca
client connects. An incident lasts until Orca answers again or the VM starts again. A wake
that started the VM and gave up has its own failed `wake` entry unless it recorded the
incident's entry itself, so Activity says who caused the start. Kinds:
`signin`, `action`, `allowlist`, `alert`, `github`, `pairing`, `build`, `reset`, `settings`,
`credentials`. `since`/`until`:
`today`, `yesterday`, `now`, `-2h`, `-7d`, `2026-11-03`, `2026-11-03 12:00`. New entries
arrive as `activity` events.

### GitHub tokens in Activity

The token service writes one English line per request, never the token
(`github-app/agentbox-gh-tokend`):

```text
[<vm>] token for <owner>/<repo> until <expires_at> by App <app-id>
[<vm>] refused: '<request>': <reason>[ <name>=<value>...]: <English text>
[<vm>] error: '<request>': <error>
```

Activity reads them as `github.token` (outcome `ok`), `github.refused` and `github.error`
(outcome `failed`), with the parameters in the table above; a refusal's `reason` is also the
entry's `reason`, and `github-tokens` gives it as `reason_code`; its parameters are in `params`
(`reason_params` in `github-tokens` and the `github-token` event). The UI words a refusal by its
reason and an error by its `message`.

| `reason` | Why the token service refused |
|---|---|
| `invalid-request` | the request is not `<owner>/<repo>` |
| `vm-not-registered` | the VM has no record in the VM registry |
| `owner-not-allowed` | the repository's owner is not one of the VM's GitHub owners |
| `not-installed` | no GitHub App is installed on the repository (a missing Installation): every App answered 404; with the parameter `app_id` when that App's installation was gone by the time it asked for the token |
| `github-status` | GitHub answered with another error; parameters `status`, GitHub's HTTP status, and `app_id`, the App that got it. The Apps after it are not asked |
| `app-key` | the key of the App `app_id` (a parameter) cannot sign: its file is missing or broken. The Apps after it are not asked |

## Host and System

| Method and path | Response |
|---|---|
| `GET /host` | readings, thresholds and 30 minutes of history: `{"interval", "history_seconds", "thresholds", "time", "readings": [{"id", "kind", "device", "source", "unit", "value", "threshold", "over", ...}], "vms": {"<vm>": {"memory_bytes"}}, "history": [{"time", "values", "vms"}]}` |
| `GET /system` | `{"watch": <watch event>, "host_reboot": <host-reboot event>\|null, "services": [<unit>]\|null, "ui": <version event>}` |

## Images and Builds

| Method and path | Body / query | agentbox call, response |
|---|---|---|
| `GET /images` | | `images`: `{"images": [{"name", "base", "in_release", "current", "vms", "running", "package_updates", "updates", "update", "containerfile", "containerfile_url", "builds": [{"build", "status", "step", "started", "finished", "release", "commit", "base_build", "package_min_age_days", "orca_version", "agents", "tools", "updates", "error", "problems", "actor", "current", "file", "size", "used_by", "pinned_by"}]}], "builds_kept", "release", "repository", "repository_url", "disks_readable", "available", "updating"}`: every Image with its Builds, which VMs use and pin them, the running Build. A Build's `orca_version` is the Orca version the Settings pinned when it was made (`null`: the newest release old enough; a derived Image has its Base Build's). `release`: the installed Release the next Build takes (`null` when its `VERSION` cannot be read); a Build's `release` is the Release it came from, and Builds from before ADR 0010 have `release: null` and the commit of `main` they came from in `commit` (`null` for every newer one). `in_release`: the installed Release defines the Image (`false`: only its Builds are left; `null`: the Release's files are not there). `containerfile`: the path of the Containerfile that defines the Image, in the repository and in the Release (`vm/Containerfile` for the Base image, `vm/images/<name>/Containerfile` for the others); `containerfile_url`: that file on GitHub at what the Image's current Build came from, the tag `v<release>` of its Release or, for a Build from before ADR 0010, its commit in the repository its record names (`null` without a current Build, for a checkout's Release `0.0.0`, or when neither is recorded). `repository`: the repository the Releases come from (`owner/name`), `repository_url` its page on GitHub. Links only: nothing is fetched from GitHub and no token is involved. `updating`: an update of every Image runs (Update now or the automatic one, unit `agentbox-build@update.service`, also while it still checks the versions; the `services` event shows the unit too). Tool versions (below) |
| `POST /builds` | `{"image": "<image>"\|"all", "reset"?: ["<vm>", ...]}` | `build`: `202 {"image", "unit", "started": true, "reset", "message"}`. With `reset` (one Image only) an Update: when the Build becomes the Image's current one, its unit resets these VMs onto it (each must use the Image without a Build pin) and starts those that ran; a failed Build resets nothing. Each Reset, done or not, is an Activity entry of kind `reset`, action `reset.update` |
| `POST /updates` | `{"reset"?: ["<vm>", ...]}` | `update-now`: `202 {"unit", "started": true, "reset", "message"}`. Update now (#114), in the unit `agentbox-build@update.service`: the version check at once (the fetch as `agentbox-feeds`, then the root part), a Build of every Image a new Build would update (every Image when the Base image has an update; none when nothing is new), then, when every Build succeeded, a Reset of the VMs in `reset` onto the newest Build of their Image, started again if they ran. Each VM must have no Build pin; any Image. A failed Build resets nothing. The Builds are Activity entries with `reason` `update` (and `build.unneeded` when none was needed), each Reset `reset.update` as for `POST /builds`. `409 update_running` while one runs |
| `GET /images/<image>/log` | `?build&after_cursor&lines` | `build-log`: `{"image", "build", "status", "step", "error", "problems", "entries", "cursor", "truncated"}`; without `build` the running Build, else the newest |
| `GET /images/<image>/containerfile` | | `containerfile`: `{"image", "path", "release", "url", "text", "size", "truncated"}`: the Containerfile that defines the Image in the installed Release, which its next Build takes (#67); the current Build may have come from an earlier Release. Read on the Host from the installed files (a regular file that is not itself a link; the directories above it are the package's, owned by root), never fetched; `text` is at most 64 KiB, cut after its last whole line (`truncated`; `size`: bytes of the whole file), without control characters, and anything that looks like a token or key is replaced by `[hidden]`. `release`: the installed Release; `url`: the file on GitHub at its tag (`null` for `0.0.0`). `404 containerfile_not_found` when the Release has no Containerfile for the Image, `400 invalid_image` for `all` and `update` |

Tool versions (#104), every version a string or `null` (unknown):
- a Build's `tools`: `{"orca", "claude-code", "codex", "opencode", "fedora"}`, what the Build
  contains (Builds made before have only the npm tools); `updates`: the tools a new Build would
  change, by `available` (an npm tool with a newer version, `orca` with another version, which
  a pinned `orca_version` may make an older one); `[]` for a Build that cannot be used.
- `available`: `{"checked": "<time>"|null, "package_min_age_days", "tools": {...},
  "orca_version"}`, what the daily check (`agentbox fetch-versions` as `agentbox-feeds`, then
  `agentbox check-versions`) found: in `tools` the npm tools and Orca as a Build would install
  them now, each the newest version at least `package_min_age_days` old (Orca: from its GitHub
  releases), or for Orca the release `orca_version` of the Settings pins (`fedora` is always
  `null`); `orca_version` that pin, `null` when there is none. Orca is `null` while the last
  check took it with another pin than the Settings have now (changing the pin starts a check).
  All `null` before the first check.
- an Image's `package_updates`: how many Fedora package updates are available for its current
  Build (`null`: not counted, which is the case for every Image but the Base image); `updates`:
  the current Build's `updates` and `"packages"` when there are package updates; `update`:
  whether `updates` is not empty, `null` when there is nothing to tell it by.

Progress arrives live as `build` and `build-log` events; the log endpoint fills gaps
(after a reconnect, or when a `build-log` event says `truncated`).

## GitHub App and Settings

| Method and path | Body | agentbox call, response |
|---|---|---|
| `GET /github-app` | | `github-app`: `{"apps": [{"id", "slug", "name", "owner", "html_url", "install_url", "installations": [{"id", "account", "type", "repository_selection", "suspended", "html_url", "vms"}], "error"}], "missing": [{"owner", "vms"}]}`, every App ascending by App ID; `error` is `null`, or `{"error", "message", "params"}` for an App whose key or GitHub's answer for it is no use (`invalid_private_key`, `github_app_rejected`, `github_unavailable`), listed without installations and counting as installed nowhere (its owners are in `missing` until it is fixed; the token service refuses there as well); `missing`: GitHub owners of VMs that no App is installed on; the old UI's unversioned `GET /api/github-app` answers with the same; `409 github_app_missing` without any App, `502 github_unavailable` when GitHub cannot be reached |
| `PUT /github-app` | `{"app_id": 123456, "private_key": "-----BEGIN ..."}` | `set-github-app` (key on stdin, checked with GitHub first): adds the App, or replaces the key of the App with this ID; the other Apps stay. The App as in `GET`, without installations, plus `"stored": true` and `"added"` (whether it is a new App) |
| `DELETE /github-app/<app_id>` | | `remove-github-app`: `{"id", "removed": true}`; the token service no longer asks the App, its key is deleted; `404 github_app_not_found` |
| `GET /settings` | | `settings`: `{"settings": {"<key>": {"value", "default", "is_default", "applies_later", "allowed"}}}` |
| `PATCH /settings` | `{"<key>": <value>, ...}` | `set-settings`: `{"changed": {"<key>": {"from", "to"}}, "applies_later": {"<key>": "<text>"}, "regenerated": ...}`; all or nothing; `"default"` restores the default and removes the key from settings.json; an `invalid_setting` error has `field`, the key whose value is wrong |

Settings keys the Control Room may change: `reset_age_days`, `reset_time`, `rebuild_day`,
`rebuild_time`, `builds_kept`, `default_memory_mib`, `default_data_disk_gib`,
`default_image`, `open_web_max_minutes`, `temporary_entry_default`, `young_domain_days`,
`feed_interval_hours`, `package_min_age_days`, `orca_version`, `activity_retention_days`,
`session_hours`, `default_language`, `auto_update`. The password, home network and ports are
not among them.
`auto_update` is `true` (the default) or `false` (JSON booleans; the text `"true"`/`"false"` is
taken too): with it on, the daily version check (two hours before `reset_time`) starts an
update of every Image with an update at once, and the daily Reset check also moves idle VMs
onto a newer Build of their Image.
`orca_version` is text: `""` (the default) lets every Build take the newest Orca release at
least `package_min_age_days` old, a version such as `"1.4.250"` pins that release.

## Live events

`GET /events` (the old `/api/events` is the same stream) is a Server-Sent Events stream
(`text/event-stream`). Use `EventSource("/api/v1/events")`; the browser sends the cookie
and, after a drop, `Last-Event-ID`.

- The first event is `hello` (no id): `{"stream": "<8 hex>", "resumed": true|false}`, and
  `retry: 3000`.
- `resumed: true`: the events missed since `Last-Event-ID` follow (the last 256 are kept).
  `resumed: false` (first connect, backend restarted, or too far behind): the current state
  follows, in this order and as far as known: `host`, `version`, `watch`, `state`,
  `host-reboot`, `services`. Replace what you hold with them.
- Every other event has an id `<stream>-<n>`. Events are whole objects, never patches, so
  getting one twice does no harm.
- A comment line (`: ping`) every 15 s while nothing happens; no event for 30 s means the
  connection is dead. The stream ends when the session ends; at most eight streams run.
- Event names not listed here may appear later; ignore them.

| Event | Data | When |
|---|---|---|
| `hello` | `{"stream", "resumed"}` | first, on every connect |
| `host` | as `GET /host` without `history`, `interval`, `thresholds`: `{"time", "readings", "vms"}` | every sample (5 s) |
| `version` | `{"version": "<16 hex>", "ui": "nuxt"\|"legacy", "installed": "<time>"\|null}` | on connect, and when a new UI build is installed: if `version` differs from the page's own (`GET /version` when it loaded), offer to reload |
| `watch` | `{"running": bool, "since": "<time>"\|null, "restarts": n, "last_exit": n\|null}` | the root helper `agentbox watch` started or ended; while `running` is false, VM state and the events below may be stale |
| `state` | `{"vms": [<VM>...], "problems": [{"vm", "problem"}]}` | on connect, and when a VM appears or goes (render the VM list from it) |
| `vm` | `<VM>` | one VM's object changed (state, agents, devices, System disk, Image, ...); replace that VM |
| `activity` | an Activity entry as in `GET /activity`, with its `code` and `params` | a new entry (actions, sign-ins, Allowlist changes, Alerts, GitHub tokens, Builds, Resets, ...) |
| `alert` | `{"time", "vm", "domain", "signals": [..]}` | a VM tried to reach a suspicious destination |
| `github-token` | `{"time", "vm", "action": "issued"\|"refused"\|"error", "outcome", "repository", "request", "reason", "reason_code", "reason_params", "expires"}` | the token service issued or refused a GitHub token (the token itself never); `reason` English, `reason_code` and `reason_params` as in `github-tokens` |
| `build` | `{"image", "build", "status": "running"\|"ok"\|"failed"\|"rejected", "step", "started", "finished", "release", "commit", "base_build", "error", "problems", "actor", "unit"}` (`release` and `commit` as in `GET /images`) | a Build started, moved to its next step (`build`, `check`, `disk`, `done`) or ended |
| `build-log` | `{"unit", "target", "image", "build", "entries": [{"time", "priority", "message", "cursor"}], "cursor", "truncated"}` | new lines of a running Build, gathered for half a second; `truncated`: lines were skipped (fetch them with the log endpoint and `after_cursor`) |
| `host-reboot` | `{"needed": true\|false\|null, "packages": [..], "checked": "<time>"}` | on connect and when it changes (checked every six hours with `dnf needs-restarting`; `null`: unknown) |
| `services` | `{"units": [{"unit", "load", "active", "sub", "description", "triggered_by": ["<unit>", ...]\|null}]}` | on connect and when one changes: the Host's agentbox units (proxies, token services, Orca ports, timers, Builds) and `ollama.service`. `triggered_by`: the timers and sockets that start the unit (systemd's `TriggeredBy`, e.g. `agentbox-feeds.timer` for `agentbox-feeds.service`, `agentbox-proxy-<vm>.socket` for the proxy); `[]` for none, `null` when unknown. Such a unit is `inactive` between its runs, which is no fault |
| `notification` | see below | something worth a browser notification |

### Notifications

```json
{"time": "2026-11-03T12:00:00Z", "kind": "alert", "severity": "critical", "vm": "work",
 "text": "work tried to reach paste.example:443 (paste, young)",
 "domain": "paste.example:443", "signals": ["paste", "young"]}
```

`kind`, `severity` (`warning` or `critical`), `vm` (or `null`), `text` (short English) and
`time` are always there; the other fields depend on the kind, so the UI can word its own
translation. Which kinds a browser shows is a per-browser preference kept by the UI.

| `kind` | `severity` | Extra fields | When |
|---|---|---|---|
| `alert` | critical | `domain`, `signals` | every Alert |
| `github-refused` | warning | `request`, `detail` (the token service's reason, English) | a GitHub token request was refused |
| `build-failed` | warning | `image`, `build`, `detail` | a Build failed; the previous one stays in use |
| `build-rejected` | critical | `image`, `build`, `detail` | a Build was discarded because a Safeguard was missing or weakened |
| `host-reboot` | warning | `packages` | updates started to need a Host reboot (the disk passphrase must be entered at boot) |
| `temperature` | warning | `reading`, `device`, `value`, `threshold` | a CPU, GPU or NVMe temperature reached its threshold (again after it was below) |

## Serving the UI

The UI is a Nuxt app generated client-side only (`ssr: false`, `nuxt generate`), built and
tested in a VM or in CI, never on the Host ([ADR 0003](adr/0003-no-node-on-the-host.md)). Its
output is `web/ui/dist/` (`index.html`, `_nuxt/`, fonts and other public files) as real
files, not as the `dist` link to `.output/public` that `nuxt generate` leaves (e.g. set
`nitro.output.publicDir` to `dist`). Neither is committed: CI builds `web/ui/dist/` from the
lockfile ([ADR 0011](adr/0011-the-control-room-ui-is-built-in-ci.md)), and the RPM of a
Release ships it in `/usr/share/agentbox/web/ui/` (symbolic links in the build are dropped).
A changed `index.html` reaches open pages as a `version` event.

- `/` and every path that is neither under `/api/` nor an existing file get `index.html`
  (`Cache-Control: no-cache`, with `ETag`), so client-side routes like `/vms/work` load.
- Files under `/_nuxt/` and `/_fonts/` must have content hashes in their names: they get
  `Cache-Control: public, max-age=31536000, immutable`. Other files get `no-cache`.
  A missing file there (or any missing path ending in a known file extension) is a 404, not
  `index.html`.
- Fonts are self-hosted (e.g. `@nuxt/fonts`, which writes `/_fonts/`); nothing may load from
  another origin.
- Content-Security-Policy: `default-src 'self'; style-src 'self' <hashes>; script-src 'self'
  <hashes>; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors
  'none'; base-uri 'none'; form-action 'none'`. The hashes are the sha256 of every inline
  `<script>` (executable types only, not `application/json` data) and `<style>` element in
  the installed `index.html`, computed when the backend loads it. Anything the app adds at
  runtime as an inline `<script>`, `<style>` element or `style="..."` attribute in HTML is
  blocked; styles set through the DOM (`el.style`, Vue's `:style`) are fine. Configure Nuxt
  UI and modules so their CSS ends up in files (or in `index.html` at build time).
- The version is the first 16 hex characters of the sha256 of `index.html`.
- Until a build is installed, the old UI is served as before; once one is, the old UI stays
  reachable at `/legacy/`.
