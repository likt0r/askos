// A VM's SSH access in its Access tab (#135) against a fake backend: the switch, the keys with
// name, type, fingerprint and date, adding one pasted or from a .pub file with an optional
// name, removing one after a confirmation that says open sessions end, a pending change and
// ssh_missing explained, the connection help from GET /vms/<vm>/ssh-config with copy buttons
// (Local-only: the --mine hint), the SSH sessions apart from the Orca devices, copying the keys
// of another VM once (#136) with which keys went in and which not, German texts.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetSessionForTests } from '~/composables/useSession'
import { resetLiveForTests } from '~/composables/useLive'
import { SSH_KEY_MAX_BYTES } from '~/utils/access'
import { FakeEventSource } from './fakes'
import type { Call } from './fakes'
import { $, $$, changes, click, fail, must, ok, openApp, settle, type, unmountAll, vm, waitFor } from './access-helpers'

const LAPTOP = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f laptop'
const LAPTOP_FP = 'SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA'
const PHONE_FP = 'SHA256:Hn0s7Kd1pqVpQq1yH9p4b2N0q3hZ2yWm5o8XxQyGk2c'
const HOST_FP = 'SHA256:q1yH9p4b2N0q3hZ2yWm5o8XxQyGk2cHn0s7Kd1pqVpQ'

const KEYS = [
  { name: 'laptop', type: 'ssh-ed25519', fingerprint: LAPTOP_FP, added: '2026-11-02T08:00:00Z' },
  { name: '', type: 'ecdsa-sha2-nistp256', fingerprint: PHONE_FP, added: '2026-11-03T09:30:00Z' },
]

function sshVm(ssh: Record<string, unknown> = {}, fields: Record<string, unknown> = {}) {
  return vm('work', 'running', {
    github_owners: ['likt0r'],
    ssh: { enabled: true, port: 2201, host_key_fingerprint: HOST_FP, keys: KEYS, pending: false, ...ssh },
    ...fields,
  })
}

// what `agentbox ssh-config work --json` prints, unchanged by the backend
const BLOCK = 'Host work.agentbox\n    HostName 127.0.0.1\n    Port 2201\n    User agent\n    HostKeyAlias work.agentbox\n'
  + '    ForwardAgent no\n    ForwardX11 no\n    StrictHostKeyChecking yes\n    UserKnownHostsFile ~/.ssh/known_hosts.agentbox\n'
const KNOWN = 'work.agentbox ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKeyOfWork'
function sshConfig(mode = 'local', on = true) {
  return {
    mode,
    hosts: on
      ? [{ vm: 'work', host: 'work.agentbox', options: { HostName: '127.0.0.1', Port: 2201, User: 'agent', HostKeyAlias: 'work.agentbox', UserKnownHostsFile: '~/.ssh/known_hosts.agentbox' }, config: BLOCK, known_hosts: KNOWN, command: 'ssh work.agentbox' }]
      : [],
  }
}

/** The Access tab of work with these SSH fields; GET /vms answers what `current` holds, and the
 * `others` VMs beside it. */
async function openSsh(ssh: Record<string, unknown> = {}, routes: Record<string, unknown> = {}, mode = 'local',
  others: ReturnType<typeof vm>[] = []) {
  const current = { vm: sshVm(ssh) }
  const opened = await openApp('/vms/work/access', {
    'GET /github-app': ok({ apps: [], missing: [] }),
    'GET /vms/work/github-tokens?since=-7d': ok({ vm: 'work', repositories: [], requests: [], truncated: false, retention_days: 90 }),
    'GET /vms': () => ok({ vms: [current.vm, ...others], problems: [] }),
    'GET /vms/work/ssh-config': () => ok(sshConfig(mode, (current.vm.ssh as { enabled: boolean }).enabled)),
    ...(routes as Record<string, never>),
  }, [current.vm, ...others])
  return { ...opened, current }
}

const PRIV = vm('priv', 'stopped', { network: 2 })
const DESK_FP = 'SHA256:Kd1pqVpQq1yH9p4b2N0q3hZ2yWm5o8XxQyGk2cHn0s7'

/** Chooses the VM to copy keys from through the select's hidden form control, as autofill gives
 * a value: an input event (its options exist only while the list is open). */
async function chooseCopyFrom(name: string) {
  const native = ($('select[name="ssh-copy-from"]') ?? must('[data-testid="ssh-copy"] select')) as HTMLSelectElement
  if (![...native.options].some(o => o.value === name)) native.insertAdjacentHTML('beforeend', `<option value="${name}">${name}</option>`)
  native.value = name
  native.dispatchEvent(new Event('input', { bubbles: true }))
  await settle()
}

function copyButton(): HTMLButtonElement {
  return must('[data-testid="ssh-copy-submit"]') as HTMLButtonElement
}

function textarea(): HTMLTextAreaElement {
  const el = must('[data-testid="ssh-key-input"]')
  return (el.matches('textarea') ? el : el.querySelector('textarea')) as HTMLTextAreaElement
}

function switchButton(): HTMLElement {
  const el = must('[data-testid="ssh-switch"]')
  return el.matches('[role="switch"]') ? el : el.querySelector<HTMLElement>('[role="switch"]')!
}

async function chooseFile(name: string, content: string) {
  const input = must('[data-testid="ssh-key-file"]') as HTMLInputElement
  Object.defineProperty(input, 'files', { value: [new File([content], name, { type: 'text/plain' })], configurable: true })
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await settle()
}

beforeEach(async () => {
  resetSessionForTests()
  resetLiveForTests()
  FakeEventSource.reset()
  vi.stubGlobal('EventSource', FakeEventSource)
  await useNuxtApp().$i18n.setLocale('en')
})
afterEach(() => {
  unmountAll()
  resetLiveForTests()
  resetSessionForTests()
})

describe('SSH access', () => {
  it('lists each key with name, type, fingerprint and date, and the host key', async () => {
    await openSsh()
    const section = must('[data-testid="ssh-access"]')
    expect(section.textContent).toContain('SSH access')
    expect(must('[data-testid="ssh-state"]').textContent).toContain('On: the SSH port 2201 is open.')
    expect(must('[data-testid="ssh-host-key"]').textContent).toContain(HOST_FP)
    const rows = $$('[data-testid="ssh-key"]')
    expect(rows.map(r => r.dataset.fingerprint)).toEqual([LAPTOP_FP, PHONE_FP])
    expect(rows[0]!.querySelector('[data-testid="ssh-key-name"]')!.textContent).toBe('laptop')
    expect(rows[0]!.querySelector('[data-testid="ssh-key-type"]')!.textContent).toContain('ssh-ed25519')
    expect(rows[0]!.querySelector('[data-testid="ssh-key-fingerprint"]')!.textContent!.trim()).toBe(LAPTOP_FP)
    expect(rows[0]!.querySelector('[data-testid="ssh-key-added"]')!.textContent).toContain('added Nov 2, 2026')
    expect(rows[1]!.querySelector('[data-testid="ssh-key-name"]')!.textContent).toBe('no name')
    expect(section.textContent).toContain('2 keys')
    expect($('[data-testid="ssh-pending"]')).toBeNull()
  })

  it('turns SSH access off and on with the switch', async () => {
    const { api, current } = await openSsh({}, {
      'PUT /vms/work/ssh': (call: Call) => {
        const on = (call.body as { enabled: boolean }).enabled
        current.vm = sshVm({ enabled: on })
        return ok({ vm: 'work', enabled: on, port: 2201, keys: 2, changed: true, state: 'running', applies: 'now', message: 'x' })
      },
    })
    expect(switchButton().getAttribute('aria-checked')).toBe('true')
    await click(switchButton())
    await waitFor(() => expect(must('[data-testid="ssh-state"]').textContent).toContain('Off: the SSH port 2201 is closed.'))
    expect(document.body.textContent).toContain('SSH access for work is off: its SSH port is closed and every SSH session ended.')
    // without SSH access, no connection help
    await waitFor(() => expect($('[data-testid="ssh-help-off"]')).not.toBeNull())
    expect($('[data-testid="ssh-config-block"]')).toBeNull()
    await click(switchButton())
    await waitFor(() => expect(must('[data-testid="ssh-state"]').dataset.enabled).toBe('true'))
    expect(changes(api.calls)).toEqual([
      { method: 'PUT', path: '/vms/work/ssh', body: { enabled: false }, csrf: '1' },
      { method: 'PUT', path: '/vms/work/ssh', body: { enabled: true }, csrf: '1' },
    ])
  })

  it('adds a pasted key with a name, and one from a .pub file under its comment', async () => {
    const { api } = await openSsh({ keys: [] }, {
      'POST /vms/work/ssh-keys': (call: Call) => ({
        status: 201,
        body: { vm: 'work', fingerprint: LAPTOP_FP, name: (call.body as { name?: string }).name ?? 'laptop', type: 'ssh-ed25519', enabled: true, keys: 1, state: 'running', applies: 'now', message: 'x' },
      }),
    })
    expect(must('[data-testid="ssh-no-keys"]').textContent).toContain('No SSH key yet.')
    expect((must('[data-testid="ssh-key-add"]') as HTMLButtonElement).disabled).toBe(true)
    await type('[data-testid="ssh-key-input"]', LAPTOP)
    await type('[data-testid="ssh-key-name-input"]', '  my laptop ')
    await click('[data-testid="ssh-key-add"]')
    expect(document.body.textContent).toContain('SSH key my laptop added to work.')
    expect(textarea().value).toBe('')

    await chooseFile('id_ed25519.pub', LAPTOP + '\n')
    expect(textarea().value).toBe(LAPTOP)
    expect(must('[data-testid="ssh-add"]').textContent).toContain('From id_ed25519.pub')
    await click('[data-testid="ssh-key-add"]')
    expect(changes(api.calls)).toEqual([
      { method: 'POST', path: '/vms/work/ssh-keys', body: { key: LAPTOP, name: 'my laptop' }, csrf: '1' },
      { method: 'POST', path: '/vms/work/ssh-keys', body: { key: LAPTOP }, csrf: '1' },
    ])
  })

  it('refuses a key file over the size the Control Room takes, without sending it', async () => {
    const { api } = await openSsh({ keys: [] })
    await chooseFile('big.pub', LAPTOP + ' ' + 'x'.repeat(SSH_KEY_MAX_BYTES))
    expect(textarea().value).toBe('')
    expect(must('[data-testid="ssh-error"]').dataset.code).toBe('ssh_key_invalid')
    expect(changes(api.calls)).toEqual([])
  })

  it('says a key added while SSH access is off waits for it', async () => {
    await openSsh({ enabled: false, keys: [] }, {
      'POST /vms/work/ssh-keys': { status: 201, body: { vm: 'work', fingerprint: LAPTOP_FP, name: 'laptop', type: 'ssh-ed25519', enabled: false, keys: 1, state: 'running', applies: null, message: 'x' } },
    })
    await type('[data-testid="ssh-key-input"]', LAPTOP)
    await click('[data-testid="ssh-key-add"]')
    expect(document.body.textContent).toContain('SSH key laptop added to work. SSH access is off: turn it on to log in with it.')
  })

  it('never sends a private key, and translates what agentbox refuses', async () => {
    const { api } = await openSsh({ keys: [] }, { 'POST /vms/work/ssh-keys': fail(400, 'ssh_key_too_weak') })
    await type('[data-testid="ssh-key-input"]', '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----')
    expect(must('[data-testid="ssh-private-key"]').textContent).toContain('This is a private key.')
    expect((must('[data-testid="ssh-key-add"]') as HTMLButtonElement).disabled).toBe(true)
    expect(changes(api.calls)).toEqual([])

    await type('[data-testid="ssh-key-input"]', 'ssh-rsa AAAAB3NzaC1yc2E old')
    await click('[data-testid="ssh-key-add"]')
    const error = must('[data-testid="ssh-error"]')
    expect(error.dataset.code).toBe('ssh_key_too_weak')
    expect(error.textContent).toContain('RSA keys need at least 3072 bits.')
    // nothing was saved: the key stays in the field to fix
    expect(textarea().value).toBe('ssh-rsa AAAAB3NzaC1yc2E old')
  })

  it('removes a key only after a confirmation that says open sessions end', async () => {
    const { api, current } = await openSsh({}, {
      'POST /vms/work/ssh-keys/remove': () => {
        current.vm = sshVm({ keys: [KEYS[1]] })
        return ok({ vm: 'work', fingerprint: LAPTOP_FP, name: 'laptop', type: 'ssh-ed25519', enabled: true, keys: 1, state: 'running', applies: 'now', message: 'x' })
      },
    })
    await click(must(`[data-testid="ssh-key"][data-fingerprint="${LAPTOP_FP}"] [data-testid="ssh-key-remove"]`))
    expect(document.body.textContent).toContain('Remove the SSH key laptop?')
    expect(document.body.textContent).toContain('every open SSH session of work ends, also sessions opened with another key')
    expect(changes(api.calls)).toEqual([])
    await click('[data-testid="confirm-cancel"]')
    expect(changes(api.calls)).toEqual([])

    await click(must(`[data-testid="ssh-key"][data-fingerprint="${LAPTOP_FP}"] [data-testid="ssh-key-remove"]`))
    await click('[data-testid="confirm-action"]')
    expect(changes(api.calls)).toEqual([{ method: 'POST', path: '/vms/work/ssh-keys/remove', body: { fingerprint: LAPTOP_FP }, csrf: '1' }])
    expect(document.body.textContent).toContain('SSH key laptop removed from work.')
    await waitFor(() => expect($$('[data-testid="ssh-key"]').map(r => r.dataset.fingerprint)).toEqual([PHONE_FP]))
  })

  it('explains a change the VM has not taken yet, and a Build without the wrapper', async () => {
    await openSsh({ pending: true, keys: [] }, { 'POST /vms/work/ssh-keys': fail(409, 'ssh_missing') })
    expect(must('[data-testid="ssh-pending"]').textContent).toContain('The VM has not taken the last change yet.')
    await type('[data-testid="ssh-key-input"]', LAPTOP)
    await click('[data-testid="ssh-key-add"]')
    const error = must('[data-testid="ssh-error"]')
    expect(error.dataset.code).toBe('ssh_missing')
    expect(error.textContent).toContain('The change is saved')
    expect(error.textContent).toContain('until a Reset onto a newer Build')
    // saved: the key is entered, the field is empty again
    expect(textarea().value).toBe('')
  })

  it('names the command that finishes a change saved but not applied', async () => {
    await openSsh({}, {
      'PUT /vms/work/ssh': {
        status: 500,
        body: { error: { code: 'ssh_not_applied', message: 'x', params: { finish: 'agentbox set-ssh work off' } } },
      },
    })
    await click(switchButton())
    expect(must('[data-testid="ssh-error"]').textContent).toContain('agentbox set-ssh work off on the Host finishes it.')
  })

  it('shows the connection help as agentbox ssh-config gives it, with copy buttons and the --mine hint', async () => {
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const { api } = await openSsh()
    await waitFor(() => expect($('[data-testid="ssh-config-block"]')).not.toBeNull())
    expect(api.calls.some(c => c.method === 'GET' && c.path === '/vms/work/ssh-config')).toBe(true)
    expect(must('[data-testid="ssh-config-block"]').textContent).toBe(BLOCK)
    expect(must('[data-testid="ssh-known-hosts"]').textContent).toBe(KNOWN)
    expect(must('[data-testid="ssh-command"]').textContent).toBe('ssh work.agentbox')
    expect(must('[data-testid="ssh-help-known-hosts"]').textContent).toContain('Append this line to ~/.ssh/known_hosts.agentbox')
    await click('[data-testid="ssh-copy-config"]')
    expect(writeText).toHaveBeenLastCalledWith(BLOCK)
    expect(must('[data-testid="ssh-copy-config"]').textContent).toContain('Copied')
    await click('[data-testid="ssh-copy-known-hosts"]')
    expect(writeText).toHaveBeenLastCalledWith(KNOWN + '\n')
    await click('[data-testid="ssh-copy-command"]')
    expect(writeText).toHaveBeenLastCalledWith('ssh work.agentbox')
    // Local-only: the owner sits at the Host, where --mine adds their own keys
    expect(must('[data-testid="ssh-local-only"]').textContent).toContain('The Host is Local-only')
    expect(must('[data-testid="ssh-mine"]').textContent).toBe('sudo agentbox add-ssh-key work --mine')
    await click('[data-testid="ssh-copy-mine"]')
    expect(writeText).toHaveBeenLastCalledWith('sudo agentbox add-ssh-key work --mine')
  })

  it('has no --mine hint in Network mode', async () => {
    await openSsh({}, {}, 'network')
    await waitFor(() => expect($('[data-testid="ssh-config-block"]')).not.toBeNull())
    expect($('[data-testid="ssh-local-only"]')).toBeNull()
  })

  it('copies the keys of another VM once and lists which went in and which not, with why', async () => {
    const { api } = await openSsh({ enabled: false }, {
      'POST /vms/work/ssh-keys': ok({
        vm: 'work', from: 'priv', folder: null,
        added: [{ type: 'ecdsa-sha2-nistp256', fingerprint: DESK_FP, name: 'desk' }],
        skipped: [
          { type: 'ssh-ed25519', fingerprint: LAPTOP_FP, name: 'laptop', error: 'ssh_key_duplicate', message: 'entered for work already' },
          { type: 'ssh-ed25519', fingerprint: PHONE_FP, name: '', error: 'ssh_keys_too_many', message: 'at most 16' },
          { error: 'ssh_key_unheard_of', message: 'agentbox\'s own words' },
        ],
        enabled: false, keys: 3, state: 'stopped', applies: 'next_start', message: 'x',
      }),
    }, 'local', [PRIV])
    const form = must('[data-testid="ssh-copy"]')
    expect(form.textContent).toContain('Copy keys from VM …')
    expect(form.textContent).toContain('once: keys added to or removed from it later stay its own')
    expect(copyButton().disabled).toBe(true)
    // the VM itself is no source
    await chooseCopyFrom('work')
    expect(copyButton().disabled).toBe(true)
    await chooseCopyFrom('priv')
    expect(must('[data-testid="ssh-copy-from"]').textContent).toContain('priv')
    expect(copyButton().disabled).toBe(false)
    await click(copyButton())
    expect(changes(api.calls)).toEqual([{ method: 'POST', path: '/vms/work/ssh-keys', body: { from: 'priv' }, csrf: '1' }])

    const result = must('[data-testid="ssh-copy-result"]')
    expect(must('[data-testid="ssh-copy-summary"]').textContent).toContain('One key copied from priv.')
    expect(must('[data-testid="ssh-copy-summary"]').textContent).toContain('SSH access is off: turn it on to log in with them.')
    const added = $$('[data-testid="ssh-copy-added-key"]')
    expect(added.map(k => k.dataset.fingerprint)).toEqual([DESK_FP])
    expect(added[0]!.textContent).toContain('desk')
    expect(result.textContent).toContain('Not copied:')
    const skipped = $$('[data-testid="ssh-copy-skipped-key"]')
    expect(skipped.map(k => k.dataset.error)).toEqual(['ssh_key_duplicate', 'ssh_keys_too_many', 'ssh_key_unheard_of'])
    expect(skipped.map(k => k.querySelector('[data-testid="ssh-copy-skipped-name"]')!.textContent)).toEqual(['laptop', PHONE_FP, 'a key'])
    expect(skipped.map(k => k.querySelector('[data-testid="ssh-copy-reason"]')!.textContent)).toEqual([
      'This SSH key is entered for this VM already.', 'A VM has at most 16 SSH keys.', 'agentbox\'s own words'])
    expect($('[data-testid="ssh-error"]')).toBeNull()
  })

  it('drops the result of a copy when the next key action starts', async () => {
    const { current } = await openSsh({}, {
      'POST /vms/work/ssh-keys': ok({
        vm: 'work', from: 'priv', folder: null,
        added: [{ type: 'ecdsa-sha2-nistp256', fingerprint: DESK_FP, name: 'desk' }], skipped: [],
        enabled: true, keys: 3, state: 'running', applies: 'now', message: 'x',
      }),
      'POST /vms/work/ssh-keys/remove': () => {
        current.vm = sshVm({ keys: [KEYS[1]] })
        return ok({ vm: 'work', fingerprint: LAPTOP_FP, name: 'laptop', type: 'ssh-ed25519', enabled: true, keys: 1, state: 'running', applies: 'now', message: 'x' })
      },
    }, 'local', [PRIV])
    await chooseCopyFrom('priv')
    await click(copyButton())
    expect($('[data-testid="ssh-copy-result"]')).not.toBeNull()
    await click(must(`[data-testid="ssh-key"][data-fingerprint="${LAPTOP_FP}"] [data-testid="ssh-key-remove"]`))
    await click('[data-testid="confirm-action"]')
    await waitFor(() => expect($('[data-testid="ssh-copy-result"]')).toBeNull())
  })

  it('says when the other VM has no keys, and when there is no other VM', async () => {
    await openSsh({}, {
      'POST /vms/work/ssh-keys': ok({ vm: 'work', from: 'priv', folder: null, added: [], skipped: [], enabled: true, keys: 2, state: 'running', applies: null, message: 'x' }),
    }, 'local', [PRIV])
    await chooseCopyFrom('priv')
    await click(copyButton())
    expect(must('[data-testid="ssh-copy-summary"]').textContent).toContain('priv has no SSH keys: nothing was copied.')
    expect($('[data-testid="ssh-copy-added"]')).toBeNull()
    expect($('[data-testid="ssh-copy-skipped"]')).toBeNull()
  })

  it('has nothing to copy from without another VM', async () => {
    await openSsh()
    expect(must('[data-testid="ssh-copy-no-other"]').textContent).toContain('There is no other VM to copy keys from.')
    expect($('[data-testid="ssh-copy-submit"]')).toBeNull()
  })

  it('explains a copy the VM has not taken', async () => {
    await openSsh({}, { 'POST /vms/work/ssh-keys': fail(409, 'ssh_missing') }, 'local', [PRIV])
    await chooseCopyFrom('priv')
    await click(copyButton())
    const error = must('[data-testid="ssh-error"]')
    expect(error.dataset.code).toBe('ssh_missing')
    expect(error.textContent).toContain('until a Reset onto a newer Build')
    expect($('[data-testid="ssh-copy-result"]')).toBeNull()
  })

  it('in German', async () => {
    await useNuxtApp().$i18n.setLocale('de')
    await openSsh({}, {}, 'local', [PRIV])
    expect(must('[data-testid="ssh-copy"]').textContent).toContain('Schlüssel übernehmen von VM …')
    expect(copyButton().textContent).toContain('Schlüssel übernehmen')
    const section = must('[data-testid="ssh-access"]')
    expect(section.textContent).toContain('SSH-Zugang')
    expect(section.textContent).toContain('SSH-Schlüssel')
    expect(must('[data-testid="ssh-state"]').textContent).toContain('An: Der SSH-Port 2201 ist offen.')
    await click(must(`[data-testid="ssh-key"][data-fingerprint="${LAPTOP_FP}"] [data-testid="ssh-key-remove"]`))
    expect(document.body.textContent).toContain('jede offene SSH-Sitzung von work endet')
  })
})
