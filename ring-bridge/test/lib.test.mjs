// Tests for lib.mjs with fake Ring objects. Run: node --test (or npm test)
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describeCamera, snapshotOf, startServer, watch } from '../lib.mjs'

const here = dirname(fileURLToPath(import.meta.url))

// Minimal stand-ins for the rxjs subjects ring-client-api exposes.
class Subject {
  constructor(value) { this.subs = []; this.value = value; this.replay = arguments.length > 0 }
  subscribe(fn) { this.subs.push(fn); if (this.replay) fn(this.value); return { unsubscribe() {} } }
  next(v) { this.value = v; for (const fn of this.subs) fn(v) }
}

function fakeCamera(id, name, extra = {}) {
  const cam = {
    id, name, model: 'fake', isDoorbot: false, hasLight: false, hasSiren: false,
    batteryLevel: 80, hasLowBattery: false, isOffline: false, data: { location_id: 'L1' },
    onDoorbellPressed: new Subject(), onMotionStarted: new Subject(), calls: [],
    async getSnapshot() { return Buffer.from('JPEGDATA') },
    async getEvents({ limit }) {
      return { events: [{ ding_id_str: '7001', created_at: '2026-10-28T01:00:00Z', kind: 'motion', recording_status: 'ready' }].slice(0, limit) }
    },
    async getRecordingUrl(id, opts) { return `https://example.invalid/clip/${id}?t=${opts.transcoded}` },
    async setLight(on) { cam.calls.push(['light', on]) },
    async setSiren(on) { cam.calls.push(['siren', on]) },
    ...extra,
  }
  cam.onData = new Subject(cam.data)
  return cam
}

function fakeDevice(zid, name, deviceType, data) {
  return { zid, name, deviceType, data, onData: new Subject(data) }
}

function fakeLocation() {
  const cams = [fakeCamera(101, 'Front Door', { isDoorbot: true }), fakeCamera(102, 'Backyard', { hasLight: true, hasSiren: true })]
  const panel = fakeDevice('P', 'Alarm', 'security-panel', { mode: 'none' })
  const door = fakeDevice('S1', 'Front Door Contact', 'sensor.contact', { faulted: false, tamperStatus: 'ok', batteryLevel: 90, batteryStatus: 'ok' })
  const motion = fakeDevice('S2', 'Hallway Motion', 'sensor.motion', { faulted: false, tamperStatus: 'ok' })
  const code = fakeDevice('C', 'Code', 'access-code', {})
  const loc = {
    id: 'L1', name: 'Home', hasAlarmBaseStation: true, cameras: cams, calls: [],
    async getDevices() { return [panel, door, motion, code] },
    async setAlarmMode(m) { loc.calls.push(['mode', m]) },
    async soundSiren() { loc.calls.push(['siren', true]) },
    async silenceSiren() { loc.calls.push(['siren', false]) },
  }
  return { loc, cams, panel, door, motion }
}

const fixedNow = () => '2026-10-28T02:00:00Z'

test('events: doorbell, motion, camera offline and low battery', async () => {
  const { loc, cams } = fakeLocation()
  const out = []
  await watch([loc], (e) => out.push(e), fixedNow)
  assert.equal(out.length, 0, 'nothing is emitted just for subscribing')
  cams[0].onDoorbellPressed.next({})
  cams[1].onMotionStarted.next(null)
  cams[1].isOffline = true; cams[1].onData.next({})
  cams[1].isOffline = true; cams[1].onData.next({}) // unchanged: no repeat
  cams[1].isOffline = false; cams[1].onData.next({})
  cams[0].hasLowBattery = true; cams[0].batteryLevel = 12; cams[0].onData.next({})
  assert.deepEqual(out.map((e) => e.kind), ['doorbell', 'motion', 'camera-offline', 'camera-online', 'battery-low'])
  assert.deepEqual(out[0].camera, { id: '101', name: 'Front Door' })
  assert.equal(out[4].battery, 12)
  assert.ok(out.every((e) => e.type === 'event' && e.at === fixedNow()))
})

test('events: alarm mode, alarm going off and clearing, siren, sensors', async () => {
  const { loc, panel, door, motion } = fakeLocation()
  const out = []
  await watch([loc], (e) => out.push(e), fixedNow)
  panel.onData.next({ mode: 'all' })
  panel.onData.next({ mode: 'all', alarmInfo: { state: 'burglar-alarm' }, siren: { state: 'on' } })
  panel.onData.next({ mode: 'none' })
  door.onData.next({ faulted: true, tamperStatus: 'ok', batteryStatus: 'ok' })
  door.onData.next({ faulted: false, tamperStatus: 'tamper', batteryStatus: 'low', batteryLevel: 5 })
  motion.onData.next({ faulted: true, tamperStatus: 'ok' })
  assert.deepEqual(out.map((e) => e.kind), [
    'alarm-mode', 'alarm', 'siren-on', 'alarm-mode', 'alarm-cleared', 'siren-off',
    'sensor-open', 'sensor-closed', 'sensor-tamper', 'battery-low', 'sensor-open',
  ])
  assert.equal(out[0].modeName, 'away')
  assert.equal(out[1].state, 'burglar-alarm')
  assert.equal(out[4].state, 'burglar-alarm')
  assert.deepEqual(out[6].sensor, { id: 'S1', name: 'Front Door Contact', type: 'sensor.contact' })
})

test('snapshot lists cameras, sensors (no access codes) and the alarm mode', async () => {
  const { loc } = fakeLocation()
  const state = await watch([loc], () => {}, fixedNow)
  const s = snapshotOf(state)
  assert.deepEqual(s.locations, [{ id: 'L1', name: 'Home', hasAlarm: true, mode: 'none', modeName: 'disarmed', alarmState: null, sirenOn: false }])
  assert.deepEqual(s.cameras.map((c) => c.name), ['Front Door', 'Backyard'])
  assert.deepEqual(s.sensors.map((x) => x.type), ['sensor.contact', 'sensor.motion'])
  assert.equal(describeCamera(loc.cameras[0]).isDoorbell, true)
})

test('local API: secret required, snapshot, events, light, siren, mode', async () => {
  const { loc, cams } = fakeLocation()
  const state = await watch([loc], () => {}, fixedNow)
  const server = await startServer(state, 's3cret-value', 0, () => ({ connected: true }))
  const base = `http://127.0.0.1:${server.address().port}`
  const h = { 'X-Api-Secret': 's3cret-value', 'Content-Type': 'application/json' }
  try {
    assert.equal((await fetch(`${base}/devices`)).status, 403)
    assert.equal((await fetch(`${base}/devices`, { headers: { 'X-Api-Secret': 'wrong' } })).status, 403)
    assert.equal((await (await fetch(`${base}/status`, { headers: h })).json()).connected, true)
    const snap = await fetch(`${base}/cameras/101/snapshot`, { headers: h })
    assert.equal(snap.headers.get('content-type'), 'image/jpeg')
    assert.equal(Buffer.from(await snap.arrayBuffer()).toString(), 'JPEGDATA')
    const events = await (await fetch(`${base}/cameras/101/events?limit=5`, { headers: h })).json()
    assert.deepEqual(events, [{ dingId: '7001', at: '2026-10-28T01:00:00Z', kind: 'motion', hasRecording: true }])
    const rec = await (await fetch(`${base}/cameras/101/recording/7001`, { headers: h })).json()
    assert.equal(rec.url, 'https://example.invalid/clip/7001?t=true')
    assert.equal((await fetch(`${base}/cameras/101/recording/..%2Fx`, { headers: h })).status, 400)

    assert.equal((await fetch(`${base}/cameras/101/light`, { method: 'POST', headers: h, body: '{"on":true}' })).status, 400) // no light
    assert.equal((await fetch(`${base}/cameras/102/light`, { method: 'POST', headers: h, body: '{"on":true}' })).status, 200)
    assert.equal((await fetch(`${base}/cameras/102/siren`, { method: 'POST', headers: h, body: '{"on":"yes"}' })).status, 400)
    assert.equal((await fetch(`${base}/cameras/102/siren`, { method: 'POST', headers: h, body: '{"on":false}' })).status, 200)
    assert.deepEqual(cams[1].calls, [['light', true], ['siren', false]])

    assert.equal((await fetch(`${base}/locations/L1/mode`, { method: 'POST', headers: h, body: '{"mode":"panic"}' })).status, 400)
    assert.equal((await fetch(`${base}/locations/L1/mode`, { method: 'POST', headers: h, body: '{"mode":"some"}' })).status, 200)
    assert.equal((await fetch(`${base}/locations/L1/siren`, { method: 'POST', headers: h, body: '{"on":false}' })).status, 200)
    assert.deepEqual(loc.calls, [['mode', 'some'], ['siren', false]])
    assert.equal((await fetch(`${base}/locations/NOPE/mode`, { method: 'POST', headers: h, body: '{"mode":"none"}' })).status, 404)
  } finally {
    server.close()
  }
})

test('the helper never calls anything that contacts Ring monitoring', () => {
  const root = join(here, '..')
  const files = readdirSync(root).filter((f) => f.endsWith('.mjs')).map((f) => join(root, f))
  for (const f of files) {
    const text = readFileSync(f, 'utf8')
    for (const forbidden of ['triggerBurglarAlarm', 'triggerFireAlarm', 'DispatchSignal']) {
      assert.ok(!text.includes(forbidden), `${f} mentions ${forbidden}`)
    }
  }
})
