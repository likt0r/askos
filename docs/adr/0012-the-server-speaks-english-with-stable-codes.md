# The server speaks English and names stable codes; only the Control Room translates

#2 decided that code, logs, API messages and the docs are English and that the UI translates. The CLI, the Activity texts and the error messages were still German, and the Control Room's backend turned agentbox's German errors into codes by matching their wording with regular expressions, so rewording a message broke the API without any test of the CLI noticing (#69).

Only the Control Room translates; it stays bilingual (de, en). Everything else the server produces is English and untranslated: the CLI's output and usage text, logs and journal lines, the messages agents see inside a VM (pre-push hook, `gh` wrapper, package Safeguards, proxy responses) and free-text details such as the API's `detail`. Where the Control Room shows something in the user's language, the server hands it a stable code and parameters set where the thing arises: an error carries its code (`vm_not_found`), an Activity entry its action and fields, a Signal's reason its parameters; the English text goes alongside as the fallback. With `--json`, a failing CLI command writes its code, message and parameters as one JSON object on stderr (docs/control-room-api.md), and the web service maps the code to an HTTP status with a plain table, never by the wording. Codes are groups, such as `invalid_domain` or `force_required`, not one per place in the code; failures only the Host can cause (setup, regenerate, timers) share the generic `action_failed`.

## Consequences

- Rewording a message changes nothing for the API or the UI. Renaming a code is a change of the API: the UI translates codes and branches on some of them.
- Every new error the Control Room can trigger needs a code (an existing group, or a new one in the web service's table, the API docs and both UI locale files); a test checks that the CLI's codes, the table, the docs and the translations agree.
- Activity entries and Alerts recorded before this keep their German text and no code; the UI shows any entry it has no translation for with its stored text.
- People at the terminal of the Host read English only.

## Considered Options

- Keep matching on the wording, in English: rejected, every rewording would still silently change the API, and the table would keep growing with one pattern per message.
- A bilingual CLI (a language setting or `LANG`): rejected, it doubles every message in the CLI, the guest scripts and the proxy, and the Control Room would still need codes to branch on.
- One code per call site: rejected, the UI would need hundreds of translations for messages nobody sees there, and moving a check would rename its code. Groups name what the user can do about the failure.
