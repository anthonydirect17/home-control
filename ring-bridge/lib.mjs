// Core of the Ring helper, kept free of ring-client-api imports so it can be
// tested with fake cameras, sensors and locations.
//
// Safety: this helper never contacts Ring's monitoring center. It has no
// code path to the library's burglar or fire alarm triggers, and a test
// checks that their names never appear in this folder's code.

import { createServer } from 'node:http'
import { timingSafeEqual } from 'node:crypto'

export const MODE_NAMES = { none: 'disarmed', some: 'home', all: 'away' }
const MODE_VALUES = new Set(Object.keys(MODE_NAMES))
const HIDDEN_TYPES = new Set(['access-code', 'access-code.vault'])
const PANEL_TYPE = 'security-panel'

export function describeCamera(cam) {
  const d = cam.data || {}
  return {
    id: String(cam.id),
    name: cam.name,
    locationId: d.location_id ?? null,
    model: cam.model ?? null,
    isDoorbell: !!cam.isDoorbot,
    hasLight: !!cam.hasLight,
    hasSiren: !!cam.hasSiren,
    lightOn: d.led_status === 'on',
    battery: cam.batteryLevel ?? null,
    batteryLow: !!cam.hasLowBattery,
    // A camera on battery allows about one snapshot every 10 minutes, and none while it records.
    onBattery: !!cam.operatingOnBattery,
    snapshotWhileRecording: !!cam.canTakeSnapshotWhileRecording,
    offline: !!cam.isOffline,
  }
}

export function describeSensor(dev, locationId) {
  const d = dev.data || {}
  return {
    id: dev.zid,
    name: dev.name,
    locationId,
    type: dev.deviceType,
    faulted: !!d.faulted,
    tamper: d.tamperStatus === 'tamper',
    battery: d.batteryLevel ?? null,
    batteryLow: d.batteryStatus === 'low',
  }
}

export function describeLocation(loc, panel) {
  const d = (panel && panel.data) || {}
  const mode = MODE_VALUES.has(d.mode) ? d.mode : null
  return {
    id: loc.id,
    name: loc.name,
    hasAlarm: !!loc.hasAlarmBaseStation,
    mode,
    modeName: mode ? MODE_NAMES[mode] : null,
    alarmState: d.alarmInfo?.state ?? null,
    sirenOn: d.siren?.state === 'on',
  }
}

// Subscribe to everything worth knowing and keep a live picture of it.
// `locations` are ring-client-api Location objects (or fakes with the same shape).
export async function watch(locations, emit, now = () => new Date().toISOString()) {
  const state = { locations: new Map(), cameras: new Map(), sensors: new Map(), panels: new Map() }
  const send = (kind, extra) => emit({ type: 'event', kind, at: now(), ...extra })

  for (const loc of locations) {
    state.locations.set(loc.id, loc)
    for (const cam of loc.cameras || []) {
      state.cameras.set(String(cam.id), cam)
      const ref = () => ({ id: String(cam.id), name: cam.name })
      cam.onDoorbellPressed?.subscribe(() => send('doorbell', { camera: ref() }))
      cam.onMotionStarted?.subscribe(() => send('motion', { camera: ref() }))
      let offline = !!cam.isOffline
      let low = !!cam.hasLowBattery
      cam.onData?.subscribe(() => {
        if (!!cam.isOffline !== offline) {
          offline = !!cam.isOffline
          send(offline ? 'camera-offline' : 'camera-online', { camera: ref() })
        }
        if (!!cam.hasLowBattery !== low) {
          low = !!cam.hasLowBattery
          if (low) send('battery-low', { camera: ref(), battery: cam.batteryLevel ?? null })
        }
      })
    }

    if (!loc.hasAlarmBaseStation) continue
    const devices = await loc.getDevices()
    for (const dev of devices) {
      if (HIDDEN_TYPES.has(dev.deviceType)) continue
      if (dev.deviceType === PANEL_TYPE) {
        state.panels.set(loc.id, dev)
        let last = { ...(dev.data || {}) }
        dev.onData?.subscribe((d) => {
          const where = { location: { id: loc.id, name: loc.name } }
          if (d.mode !== last.mode && MODE_VALUES.has(d.mode)) {
            send('alarm-mode', { ...where, mode: d.mode, modeName: MODE_NAMES[d.mode] })
          }
          const was = last.alarmInfo?.state ?? null
          const is = d.alarmInfo?.state ?? null
          if (is !== was) send(is ? 'alarm' : 'alarm-cleared', { ...where, state: is ?? was })
          const sirenWas = last.siren?.state === 'on'
          const sirenIs = d.siren?.state === 'on'
          if (sirenIs !== sirenWas) send(sirenIs ? 'siren-on' : 'siren-off', where)
          last = { ...d }
        })
        continue
      }
      state.sensors.set(dev.zid, { dev, locationId: loc.id })
      let last = { ...(dev.data || {}) }
      dev.onData?.subscribe((d) => {
        const sensor = { id: dev.zid, name: dev.name, type: dev.deviceType }
        if (!!d.faulted !== !!last.faulted) send(d.faulted ? 'sensor-open' : 'sensor-closed', { sensor })
        if ((d.tamperStatus === 'tamper') !== (last.tamperStatus === 'tamper')) {
          send(d.tamperStatus === 'tamper' ? 'sensor-tamper' : 'sensor-tamper-cleared', { sensor })
        }
        if (d.batteryStatus === 'low' && last.batteryStatus !== 'low') {
          send('battery-low', { sensor, battery: d.batteryLevel ?? null })
        }
        last = { ...d }
      })
    }
  }
  return state
}

export function snapshotOf(state) {
  return {
    locations: [...state.locations.values()].map((l) => describeLocation(l, state.panels.get(l.id))),
    cameras: [...state.cameras.values()].map(describeCamera),
    sensors: [...state.sensors.values()].map(({ dev, locationId }) => describeSensor(dev, locationId)),
  }
}

export function secretMatches(given, secret) {
  const a = Buffer.from(String(given || ''))
  const b = Buffer.from(String(secret))
  return a.length === b.length && timingSafeEqual(a, b)
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = ''
    req.on('data', (c) => { raw += c; if (raw.length > 10_000) req.destroy() })
    req.on('end', () => { try { resolve(raw ? JSON.parse(raw) : {}) } catch { resolve(null) } })
  })
}

function json(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify(obj))
}

// Small local API for the Python app. Every request must carry the secret
// the app handed over at start-up.
export function makeHandler(state, secret, status) {
  return async (req, res) => {
    if (!secretMatches(req.headers['x-api-secret'], secret)) return json(res, 403, { error: 'forbidden' })
    const url = new URL(req.url, 'http://local')
    const parts = url.pathname.split('/').filter(Boolean)
    try {
      if (req.method === 'GET' && url.pathname === '/status') return json(res, 200, status())
      if (req.method === 'GET' && url.pathname === '/devices') return json(res, 200, snapshotOf(state))

      if (parts[0] === 'cameras' && parts[1]) {
        const cam = state.cameras.get(parts[1])
        if (!cam) return json(res, 404, { error: 'no such camera' })
        if (req.method === 'GET' && parts[2] === 'snapshot') {
          const jpeg = await cam.getSnapshot()
          res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Cache-Control': 'no-store' })
          return res.end(jpeg)
        }
        if (req.method === 'GET' && parts[2] === 'events') {
          const limit = Math.max(1, Math.min(50, Number(url.searchParams.get('limit')) || 20))
          const r = await cam.getEvents({ limit })
          return json(res, 200, (r.events || []).map((e) => ({
            dingId: e.ding_id_str, at: e.created_at, kind: e.kind,
            hasRecording: e.recording_status === 'ready',
          })))
        }
        if (req.method === 'GET' && parts[2] === 'recording' && parts[3]) {
          if (!/^\d+$/.test(parts[3])) return json(res, 400, { error: 'bad recording id' })
          return json(res, 200, { url: await cam.getRecordingUrl(parts[3], { transcoded: true }) })
        }
        if (req.method === 'POST' && (parts[2] === 'light' || parts[2] === 'siren')) {
          const body = await readBody(req)
          if (!body || typeof body.on !== 'boolean') return json(res, 400, { error: 'on must be true or false' })
          if (parts[2] === 'light' && !cam.hasLight) return json(res, 400, { error: 'this camera has no light' })
          if (parts[2] === 'siren' && !cam.hasSiren) return json(res, 400, { error: 'this camera has no siren' })
          await (parts[2] === 'light' ? cam.setLight(body.on) : cam.setSiren(body.on))
          return json(res, 200, { ok: true })
        }
      }

      if (parts[0] === 'locations' && parts[1] && req.method === 'POST') {
        const loc = state.locations.get(parts[1])
        if (!loc) return json(res, 404, { error: 'no such location' })
        if (!loc.hasAlarmBaseStation) return json(res, 400, { error: 'this location has no alarm' })
        const body = await readBody(req)
        if (parts[2] === 'mode') {
          if (!body || !MODE_VALUES.has(body.mode)) return json(res, 400, { error: 'mode must be none, some or all' })
          await loc.setAlarmMode(body.mode)
          return json(res, 200, { ok: true })
        }
        if (parts[2] === 'siren') {
          if (!body || typeof body.on !== 'boolean') return json(res, 400, { error: 'on must be true or false' })
          await (body.on ? loc.soundSiren() : loc.silenceSiren())
          return json(res, 200, { ok: true })
        }
      }
      return json(res, 404, { error: 'not found' })
    } catch (e) {
      return json(res, 502, { error: String(e?.message || e) })
    }
  }
}

export function startServer(state, secret, port, status) {
  return new Promise((resolve, reject) => {
    const server = createServer(makeHandler(state, secret, status))
    server.once('error', reject)
    server.listen(port, '127.0.0.1', () => resolve(server))
  })
}
