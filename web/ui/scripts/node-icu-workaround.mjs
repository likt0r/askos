// Preloaded into the build and test tools (NODE_OPTIONS=--import, see package.json).
//
// Some Node builds crash with a segmentation fault in Intl.Segmenter#segment (seen in the
// Agent Box VM image: Fedora's nodejs22 22.23.1, built for ICU 78.2, running against
// libicu 77.1). The build tools (consola, rolldown's logger, string-width, cliui) use
// Intl.Segmenter only to measure text for the terminal and fall back to simpler code when
// it is missing. So when a probe in a child process crashes, Intl.Segmenter is removed
// for this process; the generated files do not change. On a working Node nothing happens.
import { spawnSync } from 'node:child_process'

const KEY = 'AGENTBOX_INTL_SEGMENTER'
let state = process.env[KEY]
if (state !== 'ok' && state !== 'broken') {
  const probe = spawnSync(process.execPath, ['-e', 'for (const _ of new Intl.Segmenter().segment("ab")) {}'], {
    stdio: 'ignore',
    env: { ...process.env, NODE_OPTIONS: '' },
  })
  state = probe.status === 0 ? 'ok' : 'broken'
  process.env[KEY] = state      // child processes (workers, prerender) skip the probe
}
if (state === 'broken') {
  delete Intl.Segmenter
}
