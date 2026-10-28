// Home Control's Ring helper. Started by the Python app, never by hand.
//
// Protocol with the app:
//   stdin:  one JSON line at start-up: {"refreshToken", "port", "apiSecret"}
//   stdout: one JSON object per line:
//             {"type":"ready"}                         connected and serving
//             {"type":"token","refreshToken":"..."}    Ring rotated the token; the app must save it
//             {"type":"event", "kind":..., ...}         motion, doorbell, alarm, sensors, ...
//             {"type":"status","connected":bool}        alarm connection up or down
//             {"type":"error","message":"..."}          fatal; the helper exits after it
//   HTTP:   127.0.0.1:<port>, every request with header X-Api-Secret (see lib.mjs)

import { createInterface } from 'node:readline'
import { RingApi } from 'ring-client-api'
import { snapshotOf, startServer, watch } from './lib.mjs'

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + '\n')
// The Ring library logs with console.log; keep stdout for the protocol only.
console.log = (...args) => process.stderr.write(args.join(' ') + '\n')
console.info = console.log

function readConfig() {
  return new Promise((resolve, reject) => {
    const rl = createInterface({ input: process.stdin })
    rl.once('line', (line) => {
      try { resolve(JSON.parse(line)) } catch (e) { reject(e) }
      rl.close() // after settling, so the close handler below can't win the race
    })
    rl.once('close', () => reject(new Error('no start-up settings on stdin')))
  })
}

async function main() {
  const cfg = await readConfig()
  if (!cfg.refreshToken || !cfg.port || !cfg.apiSecret) throw new Error('start-up settings are incomplete')

  const api = new RingApi({
    refreshToken: cfg.refreshToken,
    controlCenterDisplayName: 'Home Control',
    cameraStatusPollingSeconds: 30,
    locationModePollingSeconds: 30,
    avoidSnapshotBatteryDrain: true,
  })
  api.onRefreshTokenUpdated.subscribe(({ newRefreshToken }) => emit({ type: 'token', refreshToken: newRefreshToken }))

  const locations = await api.getLocations()
  const state = await watch(locations, emit)
  let connected = true
  for (const loc of locations) {
    loc.onConnected?.subscribe((up) => {
      if (loc.hasAlarmBaseStation && up !== connected) {
        connected = up
        emit({ type: 'status', connected: up })
      }
    })
  }
  const status = () => {
    const s = snapshotOf(state)
    return { connected, locations: s.locations.length, cameras: s.cameras.length, sensors: s.sensors.length }
  }
  await startServer(state, cfg.apiSecret, cfg.port, status)
  emit({ type: 'ready', ...status() })

  const stop = () => { api.disconnect(); process.exit(0) }
  process.on('SIGTERM', stop)
  process.on('SIGINT', stop)
  process.stdin.on('end', stop) // the app went away
  process.stdin.resume()
}

main().catch((e) => {
  emit({ type: 'error', message: String(e?.message || e) })
  setTimeout(() => process.exit(1), 100)
})
