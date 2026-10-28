// One-time Ring sign-in, run through `python -m home_control --ring-login`.
//
// The owner types the Ring email, password (hidden) and two-step code in
// their own terminal. Prompts go to stderr; the only thing written to stdout
// is one JSON line for the app: {"type":"token","refreshToken":"..."}.
// Nothing is saved by this script itself; the app stores the token.

import { createInterface } from 'node:readline'
import { RingRestClient } from 'ring-client-api/rest-client'

const say = (text) => process.stderr.write(text)

function askLine(question) {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin, output: process.stderr, terminal: false })
    say(question)
    rl.once('line', (line) => { rl.close(); resolve(line.trim()) })
  })
}

function askHidden(question) {
  if (!process.stdin.isTTY) return askLine(question)
  return new Promise((resolve) => {
    say(question)
    let value = ''
    const stdin = process.stdin
    stdin.setRawMode(true)
    stdin.resume()
    stdin.setEncoding('utf8')
    const onData = (chunk) => {
      for (const ch of chunk) {
        if (ch === '\r' || ch === '\n') {
          stdin.setRawMode(false)
          stdin.pause()
          stdin.off('data', onData)
          say('\n')
          return resolve(value)
        }
        if (ch === '\u0003') { say('\n'); process.exit(130) }      // Ctrl+C
        if (ch === '\u0008' || ch === '\u007f') { value = value.slice(0, -1); continue } // backspace
        value += ch
      }
    }
    stdin.on('data', onData)
  })
}

async function main() {
  say('Sign in to Ring. This device shows up in the Ring app\'s Control Center as "Home Control".\n')
  const email = await askLine('Ring email: ')
  const password = await askHidden('Ring password (hidden): ')
  const client = new RingRestClient({ email, password, controlCenterDisplayName: 'Home Control' })
  let auth
  try {
    auth = await client.getCurrentAuth()
  } catch (e) {
    if (!client.promptFor2fa) throw e
    say(client.promptFor2fa + '\n')
    for (let tries = 0; tries < 3 && !auth; tries++) {
      const code = await askLine('Two-step code: ')
      try {
        auth = await client.getAuth(code)
      } catch {
        say('That code did not work. Try again.\n')
      }
    }
    if (!auth) throw new Error('two-step code not accepted after 3 tries')
  }
  process.stdout.write(JSON.stringify({ type: 'token', refreshToken: auth.refresh_token }) + '\n')
  say('Signed in.\n')
  client.clearTimeouts?.()
  process.exit(0)
}

main().catch((e) => {
  say(`Ring sign-in failed: ${e?.message || e}\n`)
  process.exit(1)
})
