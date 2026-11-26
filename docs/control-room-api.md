# Control Room API v1

The backend (`web/agentbox-web`) serves the Control Room UI and this API on
`https://<host>:8800`. Every path below starts with `/api/v1`; any other path under `/api/`
is `404 not_found` (`401 unauthenticated` without a session).

Vocabulary as in [GLOSSARY.md](../GLOSSARY.md). How the backend works:
[ARCHITECTURE.md](../ARCHITECTURE.md#control-room).

## Conventions

- **JSON** in and out (`Content-Type: application/json`). Request bodies are JSON objects of
  at most 8 KiB. Times are ISO 8601 in UTC (`2026-11-03T12:00:00Z`) unless a field says
  otherwise.
- **Sign-in.** A session cookie `agentbox` (`Secure; HttpOnly; SameSite=Strict`), set by
  `POST /session` with the password or the token of a sign-in link (Session). Every endpoint
  except `POST /session` and `DELETE /session` needs it, otherwise `401 unauthenticated`. The session lasts `session_hours` of the Settings.
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
           "field": "maintenance_time", "detail": "maintenance_time: invalid value 99:99 (allowed: ...)"}}
```

- `code`: stable, snake_case; the UI translates it. New codes may be added.
- `message`: short English text, a fallback when the UI has no translation.
- `field` (optional): the request field that is wrong.
- `params` (optional): the parameters the UI words a refusal with, for the codes that have
  them: `invalid_pattern` (`reason`), `pattern_too_broad` (`host`), the VM variable
  `env_name_reserved`, `invalid_env_value`, `env_too_large` and `env_name_is_secret` name and
  the secret `secret_name_reserved`, `invalid_secret_value`, `secret_too_large` and
  `secret_name_is_env` name (`name`, never a value), and the command
  that finishes a change of SSH access (`finish` of `ssh_not_applied`), each only in the form
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
| 401 | `sign_in_link_refused` | sign-in with the token of a sign-in link that was used, has expired or is unknown |
| 403 | `csrf_header_missing` | `X-Agentbox: 1` missing |
| 404 | `not_found` | no such endpoint |
| 405 | `method_not_allowed` | endpoint exists, other method (`Allow` header lists them) |
| 429 | `too_many_attempts` | login brake: five failures (wrong passwords and refused sign-in links together) in 15 minutes from one address (`Retry-After` in seconds) |
| 503 | `too_many_streams` | eight live connections are open already (`Retry-After: 10`) |
| 503 | `readings_unavailable` | host readings are not available |
| 503 | `settings_unreadable` | agentbox refuses `/etc/agentbox/settings.json` (sign-in, `GET /session`; its reason in `detail`): the backend never falls back to the defaults, it answers this, logs the reason and then stops (exit status 78, which its unit does not restart) until the file is fixed on the Host |
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
| 400 | `invalid_cleanup_days` | the days of the Container image cleanup are not a whole number from 1 to 365 (`field`: `days`) |
| 400 | `invalid_memory` | memory limit not a whole number from 1024 MiB to the Host's memory (`field`: `memory_mib`), when changing the limit or creating a VM |
| 500 | `memory_not_applied` | the new memory limit is saved, but the VM could not be defined again; `agentbox regenerate` finishes it |
| 500 | `env_not_applied` | the VM variables are saved, but the VM could not be defined again (a domain from before VM variables lacks their fw_cfg entry); `agentbox regenerate` finishes it |
| 400 | `invalid_env_name` | the name of a VM variable is not `^[A-Za-z_][A-Za-z0-9_]*$` of at most 64 characters (`field`: `env`); the backend refuses such a name itself, before agentbox |
| 400 | `env_name_reserved` | a Safeguard or Agent Box itself sets or reads this variable, so it cannot be a VM variable (parameter `name`, `field`: `env`): the patterns of the safeguard check apart from proxy names, the names Agent Box sets for the VM, every variable of the Image's `DefaultEnvironment` and the variables bash sets itself. Proxy variables (`HTTPS_PROXY`, ...) are accepted |
| 400 | `invalid_env_value` | the value of a VM variable has a control character other than tab (a line break, NUL, ...) or is not UTF-8, such as a lone UTF-16 surrogate (`"\ud800"`), which the backend refuses itself, before agentbox (parameter `name`, `field`: `env`) |
| 400 | `env_too_large` | more than 64 VM variables and secrets together, or a value over 4 KiB in UTF-8 (parameter `name` for a value, `field`: `env`) |
| 409 | `env_name_is_secret` | the name is a secret of the VM; a name cannot be both a VM variable and a secret (parameter `name`, `field`: `env`) |
| 500 | `secret_not_applied` | the secrets are saved, but the file the VM reads was not written or the VM could not be defined again; `agentbox regenerate` finishes it |
| 400 | `invalid_secret_name` | the name of a secret is not `^[A-Za-z_][A-Za-z0-9_]*$` of at most 64 characters (`field`: `name`); the backend refuses such a name in the path itself, before agentbox |
| 400 | `secret_name_reserved` | a Safeguard or Agent Box itself sets or reads this variable, so it cannot be a secret: the same names as for `env_name_reserved` (parameter `name`, `field`: `name`) |
| 400 | `invalid_secret_value` | the value of a secret has a control character other than tab (a line break, NUL, ...) or is not UTF-8, which the backend refuses itself, before agentbox (parameter `name`, `field`: `value`) |
| 400 | `secret_too_large` | more than 64 VM variables and secrets together, or a value over 4 KiB in UTF-8 (parameter `name` for a value, `field`: `value`) |
| 409 | `secret_name_is_env` | the name is a VM variable of the VM; a name cannot be both (parameter `name`, `field`: `name`) |
| 400 | `ssh_key_invalid` | not one valid SSH public key: no key type first, its base64 does not decode, or decodes to a key of another type or form (an RSA number not in its shortest form, an RSA key over 16384 bits, an ECDSA point not on its curve); a key with a NUL or without a UTF-8 form (a lone UTF-16 surrogate) the backend refuses itself, before agentbox (`field`: `key`) |
| 400 | `ssh_key_type_refused` | an SSH key of a type other than Ed25519, ECDSA (P-256, P-384, P-521), their security-key forms or RSA |
| 400 | `ssh_key_too_weak` | an RSA key of fewer than 3072 bits |
| 400 | `ssh_key_options_refused` | an SSH key line with options before the key (`command=`, `from=`, `restrict`, ...) |
| 409 | `ssh_key_duplicate` | an SSH key with this SHA256 fingerprint is entered for the VM already |
| 409 | `ssh_keys_too_many` | the VM has 16 SSH keys already |
| 409 | `ssh_keys_too_large` | with this key, the lines of the VM's SSH keys would take more than 64512 bytes together, more than the file the VM reads holds (64 KiB with the host key) |
| 404 | `ssh_key_not_found` | the VM has no SSH key with this SHA256 fingerprint |
| 409 | `ssh_missing` | the change of SSH access is saved, but the VM runs on a Build without the wrapper that applies it in a running VM (`agentbox-ssh`): the VM keeps the SSH keys it started with until a Reset onto a newer Build |
| 503 | `ssh_pending` | the change of SSH access is saved, but the running VM has not taken it (its guest agent does not answer, it is still starting, or `agentbox-ssh` failed): each connection to its SSH port hands it over first and is refused while that fails |
| 500 | `ssh_not_applied` | the change of SSH access is saved, but a step on the Host that applies it failed (the host key, the file the VM reads, its definition, its SSH port or ending its SSH sessions); the parameter `finish` is the command that finishes it: the same call again (`agentbox set-ssh <vm> off`, `agentbox remove-ssh-key <vm> <fingerprint>`), or `agentbox regenerate` |
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
| 409 | `domain_suspicious` | strong Signals, which block the destination whatever the Allowlist says ([ADR 0021](adr/0021-strong-signals-always-block.md)): no entry, also not one with `--force`, lets it through; only lifting the Signal on the Host does |
| 404 | `allowlist_entry_not_found` | no such Allowlist entry |
| 409 | `allowlist_entry_permanent` | extending a permanent entry |
| 404 | `forbid_not_found` | no such Forbid |
| 409 | `forbid_permanent` | extending a permanent Forbid |
| 409 | `force_required` | the change would lift strong Signals: only the CLI can make it (`--force`) |
| 409 | `watchlist_recreate_force_required` | adding an entry would create a missing Watchlist again, which lets the proxy pass connections again: only the CLI can do it (`--force`) |
| 409 | `setting_cli_only` | `PATCH /settings` with `build_vm` `false`: only root on the CLI at the Host turns the build VMs off ([ADR 0025](adr/0025-builds-run-in-a-throwaway-build-vm.md)); the backend refuses it before calling agentbox, and agentbox refuses it from the Control Room's sudo rule too; `field` is `build_vm` |
| 409 | `build_vm_no_kvm`, `build_vm_no_image` | in a Build's Activity entry (`build.failed`), with `build_vm` on: the Host has no KVM (`/dev/kvm`), or there is no checked build VM image even after the version check ran for it (the Build fails at the step `pins`); the Build never falls back to the Host |
| 503 | `build_vm_memory` | likewise: less memory available (`MemAvailable`) than a build VM's; `needed_mib`, `available_mib` (left out when `/proc/meminfo` cannot be read: the Control Room then words it without the number) |
| 409 | `builtin_entry` | a built-in Legitimate domain cannot be changed |
| 404 | `watchlist_entry_not_found`, `legitimate_domain_not_found` | no such Watchlist entry or Legitimate domain |
| 409 | `legitimate_unreadable` | the record of the Legitimate domains cannot be read (`legitimate.json` broken, also in a single entry): the Legitimate-domain views and changes answer this until it is fixed on the Host; meanwhile the proxy counts its last good state, or only the built-in ones |
| 409 | `migration_required` | the text files from before hold lines the records cannot take (an unknown category, an invalid name): a change would drop them unseen, so `agentbox migrate-lists` on the Host has to take them over first (it names them) |
| 409 | `watchlist_unreadable` | the hand-maintained Watchlist cannot be read (`watchlist.json` broken, also in a single entry, or missing without the text file from before): the proxy lets nothing through, Allowlist entries included. Watchlist changes, allowing without `--force` and opening the web answer this until it is fixed on the Host; recreating a missing one is `409 watchlist_recreate_force_required`. `GET /watchlist` answers with `unreadable` instead, and the Feeds' state |
| 400 | `invalid_domain`, `invalid_minutes`, `invalid_note`, `invalid_duration`, `invalid_category`, `invalid_source` | values agentbox refused |
| 400 | `pattern_confirmation_required` | adding a regex Allowlist entry without confirming what its preview showed: `POST /allowlist` without `confirm: true`, refused by the backend itself with `field`: `confirm`; the same code from agentbox (`allow --regex` without `--confirm-pattern`) comes without a `field` |
| 400 | `invalid_pattern` | the pattern of a Regex entry breaks a rule ([ADR 0013](adr/0013-regex-entries-in-every-list.md)): too long, not printable ASCII without spaces, no regular expression, an uppercase letter, a backreference, a lookaround, a quantifier or alternative inside a repeated group, or too many quantifiers in a row that can take the same characters (agentbox names which as the parameter `reason`: `too-long`, `characters`, `syntax`, `uppercase`, `backreference`, `lookaround`, `nested-quantifier`, `too-complex`) |
| 400 | `pattern_too_broad` | the pattern matches a host of the fixed sample of unrelated hosts (agentbox names it as the parameter `host`); every list but the Watchlist |
| 400 | `unknown_setting`, `invalid_setting` | Settings (`invalid_setting` has `field`: the key whose value is wrong) |
| 404 | `image_not_found`, `build_not_found` | no such Image or Build |
| 404 | `containerfile_not_found` | the installed Release has no Containerfile for this Image (it no longer defines it, or its files are not there; a Recipe has its own read errors) |
| 400 | `invalid_image`, `invalid_build` | malformed name or Build id |
| 409 | `image_has_no_build` | the Image has no Build yet |
| 400 | `invalid_recipe` | a Recipe is not a valid plain tar or breaks one of its member or Containerfile rules |
| 409 | `image_exists` | an Image of the installed Release already has this Recipe's name |
| 409 | `image_in_use` | a Recipe cannot be removed while a VM uses its Image |
| 404 | `recipe_not_found` | no such Recipe |
| 404 | `version_not_found` | no such kept Recipe version |
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
| 500 | `sudo_failed` | sudo refused to run agentbox for the backend (its sudo rule is missing or does not allow the call), or agentbox did not run as root |
| 500 | `control_room_refused` | agentbox refused a command, `--force`, or an option or argument that only the CLI on the Host may pass, not the Control Room (the command declarations in `agentbox/`; `add-ssh-key` takes only the VM, `--name` and `--from` from it: no file, `--mine` or `--yes`) |
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
  program's own error output (`virsh`), that output; for another program that fails, `<program>
  failed: ` and the end of its error output on one line.
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
| `POST /session` | `{"password": "..."}` or `{"link": "<token>"}` | `200 {"authenticated": true, "expires": "<time>", "default_language": "browser"\|"en"\|"de"}` and the cookie; `401 wrong_password`, `401 sign_in_link_refused`, `429 too_many_attempts`, `503 settings_unreadable`; both fields: `400 invalid_field` (`field`: `link`) |
| `GET /session` | | `200 {"authenticated": true, "expires": "<time>", "default_language": "browser"\|"en"\|"de"}`, else `401 unauthenticated`; `503 settings_unreadable` |
| `DELETE /session` | | `200 {"authenticated": false}`, cookie cleared, live streams of the session end |
| `GET /version` | | `200 {"api": 1, "version": "<16 hex>"\|null, "ui": "nuxt"\|null, "installed": "<time>"\|null, "release": "<MAJOR.MINOR.PATCH>"\|null}` (`version` is the UI build, which open pages compare to offer a reload; `version`, `ui` and `installed` are `null` while no UI build is installed; `release` is the Release of Agent Box, the same as `agentbox version`) |

`default_language` is the Setting as the backend reads `/etc/agentbox/settings.json` at
that request (`browser` without the file or the key), so the UI knows it right after
sign-in and on every page load without `GET /settings`. A file agentbox refuses answers
`503 settings_unreadable` and stops the backend (Errors); it does not start with one either.

`link` is the token of a sign-in link, which `agentbox sign-in-link` prints on the Host (only
root and the members of `libvirt` can run it; the Control Room cannot) as
`https://<address>:<port>/sign-in#link=<token>`. The token stays in the fragment, which the
browser never sends; the sign-in page takes it from there and sends it here, once. It works once
and within two minutes, and gives the same session as the password. A token that was used, has
expired or is unknown answers `401 sign_in_link_refused` after a second, like a wrong password,
and counts towards the same brake; while the brake holds, a link is not taken (`429`) and stays
valid until it expires.

Sign-ins, failures, refusals by the brake and sign-outs are Activity entries (codes
`signin`, `signin.failed`, `signin.link` (with a sign-in link), `signin.link-refused`,
`signin.locked`, `signout`), with address and device, never the password or the token. Making a
sign-in link is an entry of the CLI (code `sign-in-link`, kind `credentials`, the Host user as
the Actor); without it no link is made.

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
  "container_image_cleanup": true, "container_image_cleanup_days": 7,
  "github_owners": ["likt0r"], "created": "2026-11-03T12:00:00Z",
  "env": {"DATABASE_URL": "postgres://db.example/app"}, "env_refused": [],
  "secrets": ["STAGING_API_KEY"], "secrets_refused": [],
  "image": "base", "build_pin": null,
  "image_build": "20261101-120000", "next_build": "20261101-120000",
  "system_disk": {"since": "2026-10-31T09:12:00Z", "age_days": 3, "file": "base-20261020.qcow2",
                  "image": "base", "build": "20261020"},
  "reset_changes_build": true,
  "reset_due": false,
  "agents": {"working": 1, "waiting": 2},
  "responding": {"guest_agent": null, "orca": true, "since": null},
  "failed_wakes": {"count": 0, "last": null, "address": null},
  "paused_by_alert": null,
  "open_web_until": null,
  "claude_token_since": "2026-10-01T08:00:00Z",
  "ssh": {"enabled": true, "port": 2201,
          "host_key_fingerprint": "SHA256:Hn0s7Kd1pqVpQq1yH9p4b2N0q3hZ2yWm5o8XxQyGk2c",
          "keys": [{"name": "laptop", "type": "ssh-ed25519",
                    "fingerprint": "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA",
                    "added": "2026-11-02T08:00:00Z"}],
          "pending": false},
  "devices": [{"address": "192.0.2.36", "kind": "orca", "connections": 2},
              {"address": "192.0.2.36", "kind": "ssh", "connections": 1}]
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
- `paused_by_alert`: while the VM is `paused` by an Alert with a strong Signal (`alert_action`
  `pause`, `agentbox alert-watch`), `{"time", "domain", "signals": [..]}`: when it was paused,
  the destination and the Alert's Signals; `null` otherwise. Any start, Restart, Reset or
  delete ends that Pause, and with it this field.
- `image_build`: the Image's current Build; `build_pin`: the Build the VM is pinned to;
  `next_build`: what the next Reset uses (the pin, else the current Build).
- `system_disk`: when the System disk was created (its last Reset) and from which Build;
  all `null` without a disk. `reset_changes_build`: the next Reset brings a different
  Build or Image. `reset_due`: the Reset check of the Maintenance window would reset the VM: its System disk
  is at least `reset_age_days` old, or, with `auto_update` on, its Image has a newer current
  Build than the one its System disk came from and the VM has no Build pin (the automatic
  Reset happens when the VM is idle).
- `agents`: agent states from Orca with counts (`working`, `waiting`, `done`, ...); `{}`
  when no agent runs or the VM is not running; `null` when unknown (Orca not answering, no
  Observer).
- `ssh`: the VM's SSH access, named as in `agentbox ssh <vm> --json`: `enabled` (on or off),
  `port` (its SSH port, also while off), `host_key_fingerprint` (`null` before the Host made a
  host key), `keys` (name, type, fingerprint and when added; never the key itself) and
  `pending` (the VM has not taken the last change yet).
- `devices`: open connections by remote address and `kind`: `orca` to the VM's Orca port,
  `ssh` to its SSH port (an SSH session); one entry per address and kind, so an address with
  both has two. `null` when unknown.
- `memory_mib`: the memory limit in the registry record; after a change a running VM uses
  it from its next Restart or Reset (`PUT /vms/<vm>/memory`).
- `container_image_cleanup`, `container_image_cleanup_days`: the VM's Container image
  cleanup, on or off and its number of days (1 to 365), as `describe --json` gives them, two
  separate values (turning it off keeps the days); the Control Room takes a missing key as
  `true` and `7` (`PUT /vms/<vm>/container-image-cleanup`).
- `env`: the VM variables in the registry record, names and values (they are not secret);
  `{}` without any. A running or paused VM has the ones it started with until its next
  Restart or Reset (`PUT /vms/<vm>/env`).
- `env_refused`: the names in `env` that a later release refuses (a Safeguard or Agent Box
  reads them now): the record keeps them, the VM never gets them; `[]` usually. Saving the
  list with such a name fails (`400 env_name_reserved`); leave it out to remove it.
- `secrets`: the names of the VM's secrets, never their values: the registry record holds
  only the names, the values are only in `/etc/agentbox/secrets/env-<vm>` on the Host, and no
  endpoint returns one. `[]` without any. A running or paused VM has the ones it started with
  until its next Restart or Reset (`PUT`/`DELETE /vms/<vm>/secrets/<name>`).
- `secrets_refused`: the names in `secrets` that a later release refuses, as `env_refused`;
  `DELETE /vms/<vm>/secrets/<name>` removes one.
- Memory in use is not part of it: `vms.<name>.memory_bytes` of the `host` event and of
  `GET /host`.

### Endpoints

| Method and path | Body / query | agentbox call, response |
|---|---|---|
| `GET /vms` | | `describe --json`: `{"vms": [<VM>...], "problems": [{"vm", "problem"}]}` (VMs in network order; `problems`: broken records) |
| `GET /vms/<vm>` | | `describe <vm> --json`: `<VM>` |
| `POST /vms` | `{"name", "image"?, "memory_mib"?, "data_disk_gib"?, "github_owners"?: [..], "copy_allowlist_from"?, "copy_ssh_keys_from"?}` | `create`: `201 {"vm", "network", "orca_port", "image", "memory_mib", "data_disk_gib", "github_owners", "allowlist_from", "ssh_keys_from", "ssh_keys": [{"fingerprint", "name", "type"}], "message"}`; omitted or `null` fields take the defaults of the Settings. `copy_ssh_keys_from` (`--copy-ssh-keys-from=<vm>`) copies that VM's SSH keys once, SSH access stays off; `ssh_keys` are the keys copied, `message` names any left out and why. A `copy_ssh_keys_from` that is no VM name: `404 vm_not_found` (`field` `copy_ssh_keys_from`) before agentbox runs |
| `DELETE /vms/<vm>` | `{"confirm": "<vm>", "delete_data_disk"?: false}` | `delete`: `{"vm", "data_disk": "archived"\|"deleted", "archive", "resumed", "message"}` |
| `POST /vms/<vm>/start` | | `start`: `{"vm", "state", "message"}` (`state` here is the CLI's: `running`, `paused`, `shut off`, ...) |
| `POST /vms/<vm>/pause` | | `pause`: same shape |
| `POST /vms/<vm>/restart` | | `restart`: same shape |
| `POST /vms/<vm>/reset` | `{"start"?: true}` | `reset` (`--no-start` with `start: false`): same shape. The UI asks for confirmation first |
| `POST /vms/<vm>/focus` | | `focus <vm>`: `{"focus", "others": [{"vm", "state"}], "message"}` |
| `DELETE /focus` | | `focus off` |
| `PUT /vms/<vm>/container-image-cleanup` | `{"enabled": true, "days": 14}` | `set-container-image-cleanup <vm> [on\|off] [--days N] --json`: `{"vm", "container_image_cleanup", "container_image_cleanup_days", "previous": {"container_image_cleanup", "container_image_cleanup_days"}, "changed", "message"}`; both keys optional, at least one (`enabled` a boolean, `days` a whole number, else `400 invalid_field` with `field` `enabled` or `days`); `400 invalid_cleanup_days` (`field` `days`), `404 vm_not_found`; works in every state of the VM and takes effect at the next hourly run |
| `PUT /vms/<vm>/memory` | `{"memory_mib": 16384}` | `set-memory`: `{"vm", "memory_mib", "previous_mib", "changed", "state", "applies": "next_start"\|"next_restart"\|null, "message"}`; `next_start`: the VM is off and starts with it, `next_restart`: it keeps running with its memory until its next Restart or Reset; `null`: nothing changed. A paused VM is refused (`409 vm_paused`) |
| `PUT /vms/<vm>/env` | `{"env": {"NAME": "value", ...}}` | `set-env --replace NAME=value ...`: the whole list in one step, `{}` removes every variable. `{"vm", "env", "changed", "state", "applies": "next_start"\|"next_restart"\|null, "message"}`; `next_start`: the VM is off and starts with them, `next_restart`: it runs or is paused and keeps the variables it started with until its next Restart or Reset; `null`: nothing changed. A domain defined before VM variables is defined again (a paused VM's once it has no Pause: the message says so); if that fails, the variables stay saved (`500 env_not_applied`). Names a Safeguard or Agent Box reads are refused (`400 env_name_reserved`, nothing changes). The body limit of 8 KiB applies (the UI says so before it sends a longer list); `agentbox set-env` on the Host takes the whole 64 variables of up to 4 KiB |
| `PUT /vms/<vm>/secrets/<name>` | `{"value": "..."}` | `set-secret <vm> <name> --no-prompt` with the value on stdin, never as an argument: adds the secret or replaces its value. `{"vm", "secrets": [names], "changed": true, "state", "applies": "next_start"\|"next_restart", "message"}`, as `PUT /vms/<vm>/env`; setting a value always counts as a change, so no answer tells whether it was the stored one. The value never comes back, neither in the answer, an error, the backend's log nor Activity (`secret` field: should agentbox ever echo it, the backend hides it). `value` not text: `400 invalid_field`; with a NUL, a line break or no UTF-8 form: `400 invalid_secret_value` before agentbox; a name not of the form of one: `400 invalid_secret_name` before agentbox; a reserved name: `400 secret_name_reserved`; a VM variable's name: `409 secret_name_is_env`; more than 64 together with the VM variables: `400 secret_too_large`; saved but not applied: `500 secret_not_applied` |
| `DELETE /vms/<vm>/secrets/<name>` | | `unset-secret <vm> <name>`: removes the secret, name and value. `{"vm", "secrets", "changed", "state", "applies", "message"}`; a name the VM does not have changes nothing (`applies: null`) |
| `PUT /vms/<vm>/ssh` | `{"enabled": true}` | `set-ssh <vm> on\|off`: `{"vm", "enabled", "port", "keys", "changed", "state", "applies": "now"\|"on_resume"\|"next_start"\|null, "message"}` (`keys`: how many the VM has); `now`: the running VM has it, `on_resume`: a paused VM gets it when it resumes, `next_start`: the VM is off and gets it at its next start; `null`: nothing changed. Off closes the SSH port and ends every SSH session of the VM, the keys stay. `enabled` not a boolean: `400 invalid_field` |
| `POST /vms/<vm>/ssh-keys` | `{"key": "ssh-ed25519 AAAA… laptop", "name"?: "laptop"}` | `add-ssh-key <vm> [--name=<name>]` with the key, one line of a `.pub` file, on stdin: `201 {"vm", "fingerprint", "name", "type", "enabled", "keys", "state", "applies", "message"}` (`applies` as above). `name` trimmed; empty or missing: the key's comment is its name. The key's checks and limits are agentbox's (`ssh_key_*`, `ssh_keys_*`). The Control Room passes a key only this way: agentbox refuses a file, `--mine` and `--yes` from it (`control_room_refused`). The body limit of 8 KiB holds any key agentbox takes (RSA of 16384 bits is ~2.8 KiB) |
| `POST /vms/<vm>/ssh-keys` | `{"from": "<other vm>"}` | `add-ssh-key <vm> --from=<other>`: a one-time copy of the other VM's SSH keys, no link stays. `200 {"vm", "from", "added": [{"type", "fingerprint", "name"}], "skipped": [{"type"?, "fingerprint"?, "name"?, "error", "message"}], "enabled", "keys", "state", "applies", "message"}`: each key goes through the checks and limits of one key, a key refused or entered already is in `skipped` with its code (`ssh_key_duplicate`, `ssh_keys_too_many`, ...) and agentbox's English `message`, the others go in; neither list need have an entry. `from` with `key` or `name`, or naming the VM itself: `400 invalid_field`; not a VM name: `404 vm_not_found` (`field` `from`) before agentbox runs; a VM that does not exist: `404 vm_not_found` from agentbox. A copy saved but not taken by a running VM fails like one key (`ssh_missing`, `ssh_pending`, `ssh_not_applied`), without the lists |
| `POST /vms/<vm>/ssh-keys/remove` | `{"fingerprint": "SHA256:…"}` | `remove-ssh-key`: `{"vm", "fingerprint", "name", "type", "enabled", "keys", "state", "applies", "message"}`; every SSH session of the VM ends. A `fingerprint` not of the form `SHA256:` and 43 base64 characters: `400 invalid_field`; one the VM does not have: `404 ssh_key_not_found` |
| `GET /vms/<vm>/ssh-config` | | `ssh-config <vm> --json`: `{"mode": "local"\|"network", "hosts": [{"vm", "host", "options", "config", "known_hosts", "command"}]}`, the connection help: `config` the `~/.ssh/config` block (`host` is `<vm>.agentbox`, `options` its settings by name, among them `UserKnownHostsFile`), `known_hosts` the line of its host key under that alias (`null` before the Host made one), `command` `ssh <vm>.agentbox`; `hosts` is `[]` while the VM has no SSH access. `mode` is the Host setup's: in Local-only `HostName` is `127.0.0.1`, so the block works on the Host itself |
| `PUT /vms/<vm>/image` | `{"image", "build": null\|"<build>"}` | `set-image`: `{"vm", "image", "build", "message"}`; applies at the next Reset |
| `PUT /vms/<vm>/github-owners` | `{"owners": ["likt0r", ...]}` | `set-owners`: `{"vm", "owners"}` |
| `PUT /vms/<vm>/claude-token` | `{"token": "sk-ant-oat..."}` | `set-claude-token` (token on stdin, checked with Anthropic before it replaces the old one): `{"vm", "stored": true, "length"}`. The token stays on the Host; the VM's Anthropic route uses it from the VM's next request on, without a restart |
| `DELETE /vms/<vm>/claude-token` | | `remove-claude-token`: `{"vm", "removed"}` (`false`: no token was stored); the route refuses the VM's next request (`no-token`), a request already streaming finishes. Anthropic is not asked: the token stays valid there until it is revoked |
| `POST /vms/<vm>/open-web` | `{"minutes": 30}` | `open-web`; while the Watchlist cannot be read: `409 watchlist_unreadable` |
| `DELETE /vms/<vm>/open-web` | | `close-web` |
| `GET /vms/<vm>/logs` | `?unit&priority&since&until&boot&grep&lines&after_cursor` | `logs`: `{"vm", "entries": [{"time", "unit", "identifier", "priority", "message", "boot", "cursor"}], "cursor", "truncated"}`; poll with `after_cursor=<cursor>` for new lines |
| `GET /vms/<vm>/blocked` | | `blocked`: `[{"domain", "count", "first", "last", "reason", "code", "params", "suspicious", "held_until"}]` (last 24 hours, epoch seconds). `reason`: why the proxy blocked it, English; `code`: the same as a stable code, `null` for a reason agentbox does not know: `not-allowed` (not on the Allowlist), `suspicious` (strong Signals, which block it whatever would allow it; `params.signals`, joined with `", "`), `forbidden` (a Forbid of the VM wins for it: refused at once, never held), `forbids-unreadable` (the proxy has not read the Forbids since it started: what no entry allows is refused at once), `watchlist-unreadable` (nothing passes while the Watchlist cannot be read), `not-public` (no public address), `not-tls` and `no-server-name` (a connection the proxy took no destination from: what the VM sent was no TLS handshake, or a ClientHello without a server name; `domain` is the client's address then, in effect the VM's); `suspicious`: the Signals of its last Alert; `held_until`: when the newest Hold of it ends at the latest (epoch seconds; a time in the past: it ended), `null` when it was not held. A destination the proxy holds is listed from the moment it is held (`not-allowed`), with `count` 0 until a connection to it is refused |
| `GET /vms/<vm>/alerts` | | `alerts`: `[{"domain", "tags", "count", "first", "last", "seen", ...}]` |
| `POST /vms/<vm>/alerts/seen` | `{"domain"?}` | `mark-seen`: all Alerts of the VM, or those of one destination |
| `GET /vms/<vm>/allowlist` | | `allowlist <vm>`: the entries that apply to the VM (see Allowlist) |
| `GET /vms/<vm>/forbids` | | `forbids <vm>`: the VM's Forbids (see Forbids) |
| `POST /vms/<vm>/forbids` | `{"domain", "duration"?, "note"?}` | `forbid`: `201` (see Forbids) |
| `DELETE /vms/<vm>/forbids/<domain>` | | `unforbid` |
| `POST /vms/<vm>/forbids/<domain>/extend` | `{"duration": "1h"}` | `forbid-extend` |
| `POST /vms/<vm>/forbids/<domain>/make-permanent` | | `forbid-make-permanent` |
| `PUT /vms/<vm>/forbids/<domain>/note` | `{"note"}` | `forbid-set-note` |
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

### Forbids

A Forbid ([ADR 0024](adr/0024-the-more-specific-of-an-allowlist-entry-and-a-forbid-wins.md)) is a domain, with its
subdomains, that the proxy refuses one VM at once: no Hold, so no `held` event, also in Open web
(`BLOCKED <host> (forbidden)`, code `forbidden`). Durations as for Allowlist entries. When it
expires or is removed, the destination is held and notified again. Of a Forbid and an Allowlist
entry that match a destination the more specific one wins: a named domain over a pattern, a longer
domain over its parent, at the same domain one VM's over all VMs'.

- `GET /vms/<vm>/forbids`: `[{"domain", "scope", "expires", "note", "added_by", "added", "expired",
  "remaining", "refused_24h"}]` (`scope` is the VM; `refused_24h`: connections the proxy refused for
  it in the last 24 hours, `null` when unknown).
- `POST /vms/<vm>/forbids`: the Forbid's fields, `allowlist_removed` (whether the VM's own
  Allowlist entry of the domain gave way to it) and `allowlist_unreadable` (why the Allowlist could
  not be read, else `null`: the Forbid is stored all the same, and an entry of the domain for the VM
  stays, losing the tie to it). An existing Forbid is widened, never shortened. Entries of other
  domains, for all VMs or patterns stay: the more specific one wins.
- The same domain for the same VM is never both allowed and forbidden: `POST /allowlist`, `extend`
  and `make-permanent` answer with `forbids_lifted`, `[{"domain", "scope"}]`: for an entry for one
  VM its Forbid of the same domain, which it removed; empty for an entry for all VMs or a pattern.
  They also answer with `forbidden_for`, `[{"vm", "domain"}]`: the Forbids in force that still win
  over the entry, being more specific (of a subdomain, of the domain itself for an entry for all
  VMs, of a domain a pattern matches), so the entry does not reach those VMs there; the add dialog
  names them.

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
| `POST /watchlist/entries` | `{"domain", "category", "source"?, "note"?}` or, for a Regex entry, `{"regex", "category", "source"?, "note"?}` | `watch-add` (`--regex` for a pattern): `201` with the entry; an existing entry is changed. `category`: `exfil`, `c2`, `tunnel`, `paste`, `phishing`, `blockchain` (strong) or `payload`. Lowering a strong entry to `payload`: `409 force_required`; adding one while `watchlist.json` is missing (it would recreate the Watchlist and let the proxy pass connections again): `409 watchlist_recreate_force_required`. A pattern gets no breadth check here (a broad one only raises false Alerts); one the other rules refuse is `400 invalid_pattern` |
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
 "description": "Allow evil.example temporarily: evil.example has strong Signals (exfil), which block it whatever the Allowlist says, also with --force. ...",
 "code": "allow", "params": {"domain": "evil.example", "scope": "work", "temporary": "true"},
 "error": "domain_suspicious", "error_params": {"domain": "evil.example"}}
```

- `code`: the entry's stable code, the same as `action` (ADR 0012). The UI words the entry
  from it and `params` in its language. Entries recorded before Release 0.3.0 have no code
  and German text; the UI shows their `description`, as for any code it does not know.
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
| `start`, `wake`, `pause`, `restart`, `close-web`, `migrate-allowlist`, `set-claude-token`, `remove-claude-token`, `pair`, `observe`, `unpair`, `revoke-pairings`, `migrate`, `regenerate`, `setup`, `sign-in-link` | none |
| `reset` | `no_start` |
| `focus` | `target` (a VM), or `off` |
| `create` | `image` (when given) |
| `delete` | `delete_data_disk` |
| `set-memory` | `mib` |
| `set-container-image-cleanup` | `enabled` (`true`/`false`), `days` (the values after the call) |
| `container-image-cleanup.removed` | `removed` (Container images), `freed_bytes`, `days`; recorded by the hourly run only when it removed something (actor `timer`) |
| `container-image-cleanup.failed` | `days`; outcome `failed` with an error code (`guest_agent_unavailable`, `vm_query_timeout`, `container_image_cleanup_missing`, `container_image_cleanup_failed`); actor `timer` |
| `set-env` | `env` (the variables as `NAME=value`, joined with `", "`; only those whose name has the form of one), `replace`. Like every parameter at most 500 characters: a longer list keeps the whole variables that fit and ends in `, … +<n>`, the number left out |
| `unset-env` | `names`, cut like `env` |
| `set-secret`, `unset-secret` | `name` (the secret's name, when it has the form of one); never its value, which comes on stdin and reaches no entry |
| `allow` | `domain` or, for a Regex entry, `regex` (the pattern; so for every command below that names `domain`), `scope` (a VM or `all`), `duration` (`15min`, `1h`, `8h`, `7d`) or `temporary`, `force` |
| `extend` | `domain`, `scope`, `duration`, `force` |
| `make-permanent` | `domain`, `scope`, `force` |
| `disallow`, `set-note`, `allowlist.expired` | `domain`, `scope` |
| `forbid` | `domain`, `scope` (the VM), `duration` (`15min`, `1h`, `8h`, `7d`) or `temporary`, `allowlist_unreadable` (`true` when it was stored while the Allowlist could not be read) |
| `forbid-extend` | `domain`, `scope`, `duration` |
| `unforbid`, `forbid-make-permanent`, `forbid-set-note`, `allowlist.forbid-expired` | `domain`, `scope` |
| `allowlist.forbid-lifted` | `domain`, `scope` of the Forbid an Allowlist entry for its VM and domain lifted |
| `allowlist.entry-forbidden` | `domain`, `scope` of the VM's Allowlist entry a Forbid took the place of |
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
| `signin`, `signin.failed`, `signin.link`, `signin.link-refused`, `signin.locked`, `signout` | none (address and device are no parameters) |
| `alert` | `domain`, `signals` |
| `github.token` | `repository`, `expires` (GitHub's expiry, ISO 8601), `app_id` (the App that issued it; not in lines from before there were several Apps) |
| `github.refused` | `request` (as the VM sent it), `repository` (when the request names one), `reason` (a stable code, below) and its parameters (`status`, `app_id`) |
| `github.error` | `request`, `repository`, `message` (the token service's error, English) |
| `claude.request` | `method`, `path`, `status` (Anthropic's), `model` (`-` when the body names none), `seconds`, `bytes` (of the answer the VM got), `request_id` (Anthropic's `request-id`, `-` without), `interrupted` (`yes`: the answer broke off on either side) |
| `claude.refused` | `reason` (a stable code, below), `method` and `path` (`path-not-allowed`), `message` (the route's text, English) |
| `claude.error` | `message` (the route's own error, English) |
| `wake.not-responding` | `reason` |
| `reset.automatic`, `reset.postponed`, `reset.failed` | `reason`; onto a newer Build also `image`, `build` |
| `reset.update` | `image`; done: `build`, `started`; failed: `running`; never begun: `skipped`, `failed_builds` |
| `build.ok` | `image`, `build`, `release`, `recipe_sha256` (for a Recipe Build), `removed` (old Builds), `reason` |
| `build.failed` | `image`, `build`, `all`, `reason`, `recipe_sha256` (for a Recipe Build); `message` without an error code |
| `build.rejected` | `image`, `build`, `reason`, `message` (the first Safeguard problem), `recipe_sha256` (for a Recipe Build) |
| `recipe.set` | `name`, `sha256`, `created` when a new Recipe was created (no entry for an unchanged tar) |
| `recipe.restore` | `name`, `version`, `sha256` |
| `recipe.remove` | `name`, `sha256`; its Builds stay |
| `build.unneeded` | `reason` |
| `build.skipped`, `build.automatic` | `images`, `reason` |
| `maintenance.feeds`, `maintenance.versions`, `maintenance.rebuild`, `maintenance.reset-check` | none: one entry per step of the Maintenance window (kind `action`, actor `timer`), in that order of the steps, written when each step ends (so the Builds' `build.*` entries come before `maintenance.versions` and `maintenance.rebuild`, the Resets' `reset.*` before `maintenance.reset-check`; the weekly rebuild has no start entry), outcome `ok` or `failed` with the error code, its message in `description`: `action_failed` for a failure the step reports (a unit that did not start or failed, a failed Build, a VM the Reset check could not handle; a weekly rebuild that cannot start carries the code of `agentbox build --all` instead, e.g. `build_running` or `not_set_up`), `unexpected_error` for a fault of the step itself (`params.type`: the kind of fault); a rebuild that is not due records none |
| `maintenance.skipped` | `reason` (`already-ran`), `time` (`maintenance_time`, `HH:MM`): a window the timer started did nothing, because the timer's window of the latest turn of `maintenance_time` began already (kind `action`, actor `timer`); a window by hand is never skipped. The UI shows the stored text for a reason it does not know |

`reason` (also in `params`) is a stable code where one was
recorded, e.g. why an automatic Reset was postponed (`reset.postponed`: `gui-connected`,
`ssh-connected`, `gui-unknown`, `agents-working`, `agents-unknown`), `update` for an automatic Reset onto a
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
`signin`, `action`, `allowlist`, `alert`, `github`, `claude`, `pairing`, `build`, `reset`, `settings`,
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

### Claude requests in Activity

The Anthropic route of the token service (ADR 0023) writes one English line per request,
never the token, the placeholder, a header value or a body (`github-app/agentbox-claude-route`);
a successful `HEAD /api/hello` is not logged (its refusals are):

```text
[<vm>] claude <method> <path> <status> model=<model> <seconds>s <bytes>B request-id=<id>[ interrupted]
[<vm>] refused: <reason>[ <name>=<value>...]: <English text>
[<vm>] error: <error>
```

Activity reads them as `claude.request` (outcome `ok`, `failed` for a status of 400 or more or
an answer that broke off), `claude.refused` and `claude.error` (outcome `failed`), kind
`claude`, actor `VM <vm>`, with the parameters in the table above; a refusal's `reason` is also
the entry's `reason`. The UI words a refusal by its reason.

| `reason` | Why the route refused (status the VM got) |
|---|---|
| `no-token` | no Claude token is stored for the VM (401) |
| `wrong-placeholder` | `Authorization` is not `Bearer` and the VM's placeholder, or `x-api-key` was sent (401) |
| `path-not-allowed` | anything but `POST /v1/messages`, `POST /v1/messages/count_tokens` and `HEAD /api/hello`; parameters `method` and `path` (404) |
| `upgrade` | `CONNECT` or an `Upgrade` header (400) |
| `too-large` | a body over 32 MB (400) |
| `invalid-request` | not HTTP/1.1, malformed, `Expect`, or ambiguous: both `Content-Length` and `Transfer-Encoding`, a repeated `Host`, `Authorization` or `Content-Length`, a target that is not a canonical path (400) |
| `upstream-tls` | the TLS connection to `api.anthropic.com` could not be verified (502, never retried) |
| `upstream-error` | `api.anthropic.com` could not be reached or did not answer (502) |
| `too-many-connections` | the VM already has 64 connections to the route (503 with `x-should-retry: true`) |

## Host and System

| Method and path | Response |
|---|---|
| `GET /host` | readings, thresholds and 30 minutes of history: `{"interval", "history_seconds", "thresholds", "time", "readings": [{"id", "kind", "device", "source", "unit", "value", "threshold", "over", ...}], "vms": {"<vm>": {"memory_bytes"}}, "history": [{"time", "values", "vms"}]}` |
| `GET /system` | `{"watch": <watch event>, "host_reboot": <host-reboot event>\|null, "services": [<unit>]\|null, "warnings": [<warning>]\|null, "ui": <version event>}`; `services` and `warnings` as in the last `services` event (`null` before the first) |

## Images and Builds

| Method and path | Body / query | agentbox call, response |
|---|---|---|
| `GET /images` | | `images`: `{"images": [{"name", "base", "recipe", "in_release", "problem", "current", "vms", "running": {"build", "step", "progress", "build_vm", "started"}, "package_updates", "updates", "update", "containerfile", "containerfile_url", "builds": [{"build", "status", "step", "progress", "started", "finished", "release", "commit", "base_build", "recipe_sha256", "build_vm", "package_min_age_days", "orca_version", "playwright_version", "agents", "tools", "updates", "error", "problems", "actor", "current", "file", "size", "used_by", "pinned_by"}]}], "builds_kept", "release", "repository", "repository_url", "disks_readable", "available", "updating"}`: every Image with its Builds, which VMs use and pin them, the running Build (`null`: none). `progress`: while a Build runs its step `build`, where its `podman build` is, from the latest `STEP n/m: <command>` line podman wrote, taken only as the next step of the same count (a step's own output that looks like one moves nothing) (the Base image's, a derived Image's and a Recipe's Containerfile count alike; a multi-stage one per stage): `{"step": n, "steps": m, "command"}`, `command` the text after `STEP n/m: ` as Build output is shown: one line, control characters removed, tokens, keys and Pairing codes hidden as in the log, at most 200 characters (a longer one ends in `…`); `null` in every other step, before podman's first `STEP` line and for a Build that does not run. A Build's `orca_version` is the Orca version the Settings pinned when it was made (`null`: the newest release old enough; a derived Image has its Base Build's), `playwright_version` likewise Playwright's (`null`: the newest version old enough). `release`: the installed Release the next Build takes (`null` when its `VERSION` cannot be read); a Build's `release` is the Release it came from, and Builds from before ADR 0010 have `release: null` and the commit of `main` they came from in `commit` (`null` for every newer one). `recipe`: a Recipe the owner stored on the Host defines the Image (`false` for a Release Image, and when a later Release takes a Recipe's name); `in_release`: the installed Release defines the Image (`false` for a Recipe or when only its Builds are left; `null`: the Release's files are not there). `problem` names a same-name Recipe the Release now shadows (the Release wins and that Recipe is not built), else `null`. A Build's `build_vm`: whether it ran in build VMs (the Setting `build_vm` when its run started; `null` for the failed Build a run records at `source` when the Settings cannot be read; a run that fails at `pins` records the Setting it read), so the step `import` (taking the first build VM's image archive into the Host's storage, between `build` and `check`) is one of its steps. A Recipe Build's `recipe_sha256` is its tar's SHA-256 (`null` for a Release Image's Build), while its `release` stays the installed Release. `containerfile`: the path of the Containerfile that defines a Release Image, in the repository and in the Release (`vm/Containerfile` for the Base image, `vm/images/<name>/Containerfile` for the others), or `Recipe <name>: Containerfile (current recipe.tar)` for a Recipe; `containerfile_url`: that file on GitHub at what the Image's current Build came from, the tag `v<release>` of its Release or, for a Build from before ADR 0010, its commit in the repository its record names (`null` for every Recipe, without a current Build, for a checkout's Release `0.0.0`, or when neither is recorded). `repository`: the repository the Releases come from (`owner/name`), `repository_url` its page on GitHub. Links only: nothing is fetched from GitHub and no token is involved. `updating`: an update of every Image runs (Update now or the automatic one, unit `agentbox-build@update.service`, also while it still checks the versions or its unit stops; the `services` event says it too, `updating`). Tool versions (below) |
| `POST /builds` | `{"image": "<image>"\|"all", "reset"?: ["<vm>", ...]}` | `build`: `202 {"image", "unit", "started": true, "reset", "message"}`. With `reset` (one Image only) an Update: when the Build becomes the Image's current one, its unit resets these VMs onto it (each must use the Image without a Build pin) and starts those that ran; a failed Build resets nothing. Each Reset, done or not, is an Activity entry of kind `reset`, action `reset.update` |
| `POST /updates` | `{"reset"?: ["<vm>", ...]}` | `update-now`: `202 {"unit", "started": true, "reset", "message"}`. Update now (#114), in the unit `agentbox-build@update.service`: the version check at once (the fetch as `agentbox-feeds`, then the root part), a Build of every Image a new Build would update (every Image when the Base image has an update; none when nothing is new), then, when every Build succeeded, a Reset of the VMs in `reset` onto the newest Build of their Image, started again if they ran. Each VM must have no Build pin; any Image. A failed Build resets nothing. The Builds are Activity entries with `reason` `update` (and `build.unneeded` when none was needed), each Reset `reset.update` as for `POST /builds`. `409 update_running` while one runs |
| `GET /images/<image>/log` | `?build&after_cursor&lines` | `build-log`: `{"image", "build", "status", "step", "progress", "error", "problems", "entries", "cursor", "truncated"}` (`progress` as in `GET /images`); without `build` the running Build, else the newest |
| `GET /images/<image>/containerfile` | | `containerfile`: `{"image", "path", "release", "url", "text", "size", "truncated"}`: the Containerfile that defines the Image in the installed Release, which its next Build takes (#67); the current Build may have come from an earlier Release. Read on the Host from the installed files (a regular file that is not itself a link; the directories above it are the package's, owned by root), never fetched; `text` is at most 64 KiB, cut after its last whole line (`truncated`; `size`: bytes of the whole file), without control characters, and anything that looks like a token or key is replaced by `[hidden]`. `release`: the installed Release; `url`: the file on GitHub at its tag (`null` for `0.0.0`). `404 containerfile_not_found` when the Release has no Containerfile for the Image, `400 invalid_image` for `all` and `update` |

For a Recipe, `GET /images/<image>/containerfile` instead reads the `Containerfile` from its
current checked `recipe.tar` on the Host. `path` names that Recipe, `url` is `null` and `recipe_sha256` is
the SHA-256 of that tar (`null` for an Image of the Release), to compare with a Build's; the
same 64 KiB display cap, sanitizing and `release` field apply. `recipe-show --file` can show
any one UTF-8 Recipe member up to 256 KiB on the Host.

Tool versions (#104), every version a string or `null` (unknown):
- a Build's `tools`: `{"orca", "claude-code", "codex", "opencode", "playwright", "fedora"}`, what
  the Build contains (Builds made before have only the agents); `updates`: the tools a new Build would
  change, by `available` (an npm tool with a newer version, `orca` with another version, which
  a pinned `orca_version` may make an older one, and `playwright` with another version while
  `playwright_version` pins it, so nothing beyond the pin); `[]` for a Build that cannot be used.
- `available`: `{"checked": "<time>"|null, "package_min_age_days", "tools": {...},
  "orca_version", "playwright_version"}`, what the daily check (`agentbox fetch-versions` as
  `agentbox-feeds`, then `agentbox check-versions`) found: in `tools` the npm tools and Orca as
  a Build would install them now, each the newest version at least `package_min_age_days` old
  (Orca: from its GitHub releases), or for Orca the release `orca_version` of the Settings pins
  and for Playwright the version `playwright_version` pins (`fedora` is always `null`);
  `orca_version` and `playwright_version` those pins, `null` when there is none. Orca or
  Playwright is `null` while the last check took it with another pin than the Settings have
  now (changing a pin starts a check). All `null` before the first check.
- an Image's `package_updates`: how many Fedora package updates are available for its current
  Build (`null`: not counted, which is the case for every Image but the Base image); `updates`:
  the current Build's `updates` and `"packages"` when there are package updates; `update`:
  whether `updates` is not empty, `null` when there is nothing to tell it by.

Progress arrives live as `build` and `build-log` events; the log endpoint fills gaps
(after a reconnect, or when a `build-log` event says `truncated`).

## GitHub App and Settings

| Method and path | Body | agentbox call, response |
|---|---|---|
| `GET /github-app` | | `github-app`: `{"apps": [{"id", "slug", "name", "owner", "html_url", "install_url", "installations": [{"id", "account", "type", "repository_selection", "suspended", "html_url", "vms"}], "error"}], "missing": [{"owner", "vms"}]}`, every App ascending by App ID; `error` is `null`, or `{"error", "message", "params"}` for an App whose key or GitHub's answer for it is no use (`invalid_private_key`, `github_app_rejected`, `github_unavailable`), listed without installations and counting as installed nowhere (its owners are in `missing` until it is fixed; the token service refuses there as well); `missing`: GitHub owners of VMs that no App is installed on; `409 github_app_missing` without any App, `502 github_unavailable` when GitHub cannot be reached |
| `PUT /github-app` | `{"app_id": 123456, "private_key": "-----BEGIN ..."}` | `set-github-app` (key on stdin, checked with GitHub first): adds the App, or replaces the key of the App with this ID; the other Apps stay. The App as in `GET`, without installations, plus `"stored": true` and `"added"` (whether it is a new App) |
| `DELETE /github-app/<app_id>` | | `remove-github-app`: `{"id", "removed": true}`; the token service no longer asks the App, its key is deleted; `404 github_app_not_found` |
| `GET /settings` | | `settings`: `{"settings": {"<key>": {"value", "default", "is_default", "applies_later", "allowed"}}}` |
| `PATCH /settings` | `{"<key>": <value>, ...}` | `set-settings`: `{"changed": {"<key>": {"from", "to"}}, "applies_later": {"<key>": "<text>"}, "regenerated": ...}`; all or nothing; `"default"` restores the default and removes the key from settings.json; an `invalid_setting` error has `field`, the key whose value is wrong |

Settings keys the Control Room may change: `reset_age_days`, `maintenance_time`, `rebuild_day`,
`builds_kept`, `default_memory_mib`, `default_data_disk_gib`,
`default_image`, `default_container_image_cleanup`, `default_container_image_cleanup_days`,
`open_web_max_minutes`, `hold_seconds`, `alert_action`, `temporary_entry_default`, `young_domain_days`,
`feed_interval_hours`, `package_min_age_days`, `orca_version`, `playwright_version`, `activity_retention_days`,
`session_hours`, `default_language`, `auto_update`, `build_vm` (only to on). The password,
home network and ports are not among them.
`build_vm` (`true`, the default, or `false`, JSON booleans): with it on every Build runs in two
throwaway build VMs and the version check counts package updates in one
([ADR 0025](adr/0025-builds-run-in-a-throwaway-build-vm.md)); off, both run as root on the Host.
It applies from the next Build. `PATCH` turns it on (`true` or `"default"`), never off: `false`
is `409 setting_cli_only`, as only root on the CLI turns it off (`sudo agentbox set-settings
build_vm=false`). `agentbox setup` writes it off on a Host without KVM or 16 GiB of memory; while
it is off the `services` event's `warnings` carry `builds_on_host`.
`hold_seconds` (0 to 120, default 30): how long the proxy holds a connection to a destination
that is not allowed, so an entry added meanwhile lets it through; 0 refuses at once. Each proxy
reads it when it changes, so it applies at once.
`alert_action` (`pause`, the default, or `none`): whether an Alert with a strong Signal pauses
its VM (`agentbox alert-watch`, [ADR 0022](adr/0022-a-root-watcher-pauses-a-vm-on-a-strong-alert.md)).
`maintenance_time` (`HH:MM`, default `"02:00"`) is when the daily **Maintenance window** starts:
it fetches the Feeds, checks the versions (with `auto_update` it builds every Image with an
update and waits for those Builds), on `rebuild_day` rebuilds every Image, and then runs the
Reset check, each step once the one before it has finished. It replaced `reset_time` and
`rebuild_time`: `PATCH` refuses them as `unknown_setting`, and a Host whose
`settings.json` still holds `reset_time` shows `maintenance_time` two hours earlier.
`auto_update` is `true` (the default) or `false` (JSON booleans; the text `"true"`/`"false"` is
taken too): with it on, the version check of the Maintenance window starts an update of every
Image with an update at once, and its Reset check also moves idle VMs onto a newer Build of
their Image once those Builds have finished.
`orca_version` is text: `""` (the default) lets every Build take the newest Orca release at
least `package_min_age_days` old, a version such as `"1.4.250"` pins that release.
`playwright_version` is text too: `""` (the default) lets every Base Build take the newest
version of the npm package `playwright` at least `package_min_age_days` old, a version such as
`"1.65.1"` pins that version, which has to be that old as well; a project pins
`@playwright/test` to the same version.

## Live events

`GET /events` is a Server-Sent Events stream
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
| `version` | `{"version": "<16 hex>"\|null, "ui": "nuxt"\|null, "installed": "<time>"\|null}` (as in `GET /version`) | on connect, and when a UI build is installed or removed: if `ui` is `nuxt` and `version` differs from the page's own (`GET /version` when it loaded), offer to reload |
| `watch` | `{"running": bool, "since": "<time>"\|null, "restarts": n, "last_exit": n\|null}` | the root helper `agentbox watch` started or ended; while `running` is false, VM state and the events below may be stale |
| `state` | `{"vms": [<VM>...], "problems": [{"vm", "problem"}]}` | on connect, and when a VM appears or goes (render the VM list from it) |
| `vm` | `<VM>` | one VM's object changed (state, agents, devices, System disk, Image, ...); replace that VM |
| `activity` | an Activity entry as in `GET /activity`, with its `code` and `params` | a new entry (actions, sign-ins, Allowlist changes, Alerts, GitHub tokens, Builds, Resets, ...) |
| `alert` | `{"time", "vm", "domain", "signals": [..]}` | a VM tried to reach a suspicious destination |
| `held` | `{"time", "vm", "domain", "seconds"}` | the proxy holds a connection of the VM to a destination that is not allowed, for up to `seconds` (`hold_seconds`): an Allowlist entry added meanwhile lets it through. Once per destination while connections to it are held, and the proxy announces at most 10 new holds a minute per VM (more are held without an event); reload the VM's `blocked` list, at most once a second |
| `github-token` | `{"time", "vm", "action": "issued"\|"refused"\|"error", "outcome", "repository", "request", "reason", "reason_code", "reason_params", "expires"}` | the token service issued or refused a GitHub token (the token itself never); `reason` English, `reason_code` and `reason_params` as in `github-tokens` |
| `build` | `{"image", "build", "status": "running"\|"ok"\|"failed"\|"rejected", "step", "progress", "started", "finished", "release", "commit", "base_build", "recipe_sha256", "error", "problems", "actor", "unit", "build_vm"}` (`release`, `commit`, `progress` and `build_vm` as in `GET /images`) | a Build started, moved to its next step (`build`, `import` in build VMs, `check`, `disk`, `done`; a run that fails before its Builds records one at `pins` or `source`), began the next step of its Containerfile (`progress`, read every few seconds) or ended |
| `build-log` | `{"unit", "target", "image", "build", "entries": [{"time", "priority", "message", "cursor"}], "cursor", "truncated"}` | new lines of a running Build, gathered for half a second; `truncated`: lines were skipped (fetch them with the log endpoint and `after_cursor`) |
| `host-reboot` | `{"needed": true\|false\|null, "packages": [..], "checked": "<time>"}` | on connect and when it changes (checked every six hours with `dnf needs-restarting`; `null`: unknown) |
| `services` | `{"units": [{"unit", "load", "active", "sub", "description", "state", "text", "triggered_by": ["<unit>", ...]\|null}], "warnings": [{"code", "message"}], "updating"}` | on connect and when one changes: the Host's agentbox units (proxies, token services, Orca ports, timers, Builds) and `ollama.service`. `load`, `active` and `sub` are systemd's words, `text` them as the UI shows them (`active (running)`, or `active` alone when `sub` says nothing more); `state` is what they mean, for a client to decide on: `running` (also while it starts or reloads), `stopping`, `stopped` or `failed` (also a unit systemd does not find or cannot load). `updating`: an update of every Image runs, as `updating` of `GET /images` (its unit `agentbox-build@update.service` counts while it still stops, as a Build's does). `triggered_by`: the timers and sockets that start the unit (systemd's `TriggeredBy`, e.g. `agentbox-feeds.timer` for `agentbox-feeds.service`, `agentbox-proxy-<vm>.socket` for the proxy); `[]` for none, `null` when unknown. Such a unit is `stopped` between its runs, which is no fault. `warnings`: what the Host lacks for its maintenance and safety, `[]` for nothing, each with a stable `code` and the English `message` (the UI shows the message for a code it does not know); the same list as `host_warnings` of `agentbox status --json`. Codes: `maintenance_timer_missing`, the Maintenance window's timer `agentbox-maintenance.timer` is missing on a Host whose timers `agentbox regenerate` wrote before (after an update from a Release before the Maintenance window; until then the old Reset timer runs the window at the old Reset time), and `sudo agentbox regenerate` creates it; `builds_on_host`, the Setting `build_vm` is off, so Builds run as root on the Host, for as long as it is off (the System page shows it as a lasting notice, with the command that turns the build VMs on) |
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
| `alert` | critical | `domain`, `signals`; `paused: true` when the Alert paused the VM | every Alert, and again when it paused its VM (`alert_action`) |
| `held` | warning | `domain`, `seconds` | the proxy holds a connection to a destination that is not allowed (`held` event); once a minute per VM and destination, at most 3 a minute per VM. The VM's blocked list has its "Allow…" |
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
- Without an installed build every path outside `/api/` is `503` with a plain-text note
  that the UI is not installed; the API works as usual.
