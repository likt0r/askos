// Checks the generated dist/ against what the backend and its CSP expect
// (docs/control-room-api.md "Serving the UI"); run by `npm run generate`.
//   - real files only (the RPM drops symbolic links)
//   - index.html present; files under /_nuxt/ and /_fonts/ carry a content hash
//   - nothing refers to another origin (scripts, styles, fonts, images)
//   - lists the inline <script>/<style> of index.html (the CSP allows them by hash)
import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, readdirSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const dist = fileURLToPath(new URL('../dist', import.meta.url))
const problems = []
const files = []

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    const st = lstatSync(full)
    if (st.isSymbolicLink()) problems.push(`symbolic link: ${relative(dist, full)}`)
    else if (st.isDirectory()) walk(full)
    else files.push({ path: '/' + relative(dist, full), size: st.size, full })
  }
}
walk(dist)

if (!files.some(f => f.path === '/index.html')) problems.push('index.html missing')

const HASHED = /[.-][A-Za-z0-9_-]{8,}\.[a-z0-9]+$|^[A-Za-z0-9_-]{8,}\.[a-z0-9]+$/
for (const f of files) {
  if (/^\/_(nuxt|fonts)\//.test(f.path) && !HASHED.test(f.path.split('/').pop())) {
    problems.push(`no content hash in the name (would be cached as immutable): ${f.path}`)
  }
}

const external = /(?:src|href)\s*=\s*["']?(?:https?:)?\/\/|url\(\s*["']?(?:https?:)?\/\/|@import\s+(?:url\()?["']?(?:https?:)?\/\//i
for (const f of files.filter(f => /\.(html|css)$/.test(f.path))) {
  if (external.test(readFileSync(f.full, 'utf8'))) problems.push(`refers to another origin: ${f.path}`)
}

const index = readFileSync(join(dist, 'index.html'), 'utf8')
const inline = [...index.matchAll(/<(script|style)\b([^>]*)>([\s\S]*?)<\/\1\s*>/gi)]
  .filter(([, tag, attrs]) => !(tag.toLowerCase() === 'script' && /\ssrc\s*=/i.test(attrs)))
  .map(([, tag, attrs, body]) => {
    const type = /\stype\s*=\s*["']?([^"'\s>]+)/i.exec(attrs)?.[1] ?? ''
    const hash = createHash('sha256').update(body).digest('base64')
    return `${tag}${type ? ` type=${type}` : ''} ${body.length} B sha256-${hash}`
  })

const total = files.reduce((n, f) => n + f.size, 0)
console.log(`dist: ${files.length} files, ${(total / 1024).toFixed(0)} KiB; version ${createHash('sha256').update(index).digest('hex').slice(0, 16)}`)
for (const line of inline) console.log(`  inline ${line}`)
if (problems.length) {
  console.error(problems.map(p => `check-dist: ${p}`).join('\n'))
  process.exit(1)
}
