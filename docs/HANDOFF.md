# Home Control: handoff brief for the home-server session

This document carries the whole project to the Claude session running on the always-on home server. Read all of it before changing anything.

**The owner prefers questions over guesses.** When something here doesn't match what you find on the machine, or a decision isn't covered, ask.

Personal details (device names, counts, addresses, keys) are deliberately **not** in this file because the repository is public. The owner gives you those in chat. Keys never go in chat.

---

## 1. Goal

One control center for the house, running on an always-on home server and opened in a browser from any device on the owner's Tailscale network. It should go deeper than any single vendor app, mainly by linking brands together:

- **Govee lights:** full control, Govee scenes, custom live effects across several lights.
- **Ring:**
  - doorbell, cameras (some battery, some wired), floodlight cams
  - **Ring Alarm** (base station, keypad, motion and contact sensors), chime
  - recorded clips (the owner has Ring Protect)
- **Rules:** "when this happens, if these conditions hold, do that", across all brands. This is the main reason the project exists.
- **Alexa (several Echo devices) and a Sonos soundbar with Alexa built in:**
  - custom announcements, changing what Ring announces and how
  - when the alarm goes off, sound it on every Echo and the Sonos as well, loudly
- **Google devices:** none yet. Keep the design ready for them (section 8, phase 6).

**Timers are not a priority.** The owner uses the Govee app's own timers, which run without any PC. The app's timer tab exists but shouldn't get more work unless asked.

## 2. Decisions already made, and why

**Port:** the page uses **8780** by default. **Never use 8765**: it belongs to the owner's trading desk. Where Home Control and the trading desk conflict on anything, Home Control yields.

| Decision | Reason |
|---|---|
| App runs on a **home** server on the same network as the devices | Govee LAN control, live effects, and Sonos all need the local network. The owner's other always-on server is at work, not on the home network, so it was ruled out. |
| **Python app is the hub** (FastAPI + one local web page, no build step) | Already built and tested. Easy to run on Windows or Linux. |
| **Ring through a small Node.js helper** using `ring-client-api` | It's the only well-maintained library that covers Ring **Alarm** (modes, sensors, siren) as well as cameras. The Python library covers cameras/doorbells only. Node 20/22/24 required. |
| **Alexa through the same Node helper** using `alexa-remote2` | No official API lets a home app make an Echo speak. `alexa-remote2` is the unofficial library used by Node-RED and ioBroker. |
| **Sonos through Python `soco`** (local network) | Sonos is controlled locally, so it's reliable and fast. |
| **Govee: LAN API first, cloud API second, Govee Desktop optional** | LAN is fast and reports state. Cloud reaches every light and runs scenes. Govee Desktop is only needed for per-segment effects, and requires running as admin. |
| **Public GitHub repo; secrets never in it** | Owner's choice. Personal data lives in `data/` (git-ignored). Keys are in Windows Credential Manager or `~/.config/home-control/secrets.json`. |
| **Page reachable over Tailscale** | The owner wants to reach it from anywhere. Bind to the server's Tailscale address. The page has no login yet, so never expose it beyond Tailscale. |

## 3. What exists today (`main` branch)

```
home_control/
  __main__.py        start-up, --demo, --set-govee-key, --set-guid, --host/--port
  hub.py             merges each light's routes (lan/cloud/desktop) and picks the best per command, with fallback
  controller.py      commands, groups (linked "span" / "mirror"), live effects, state polling
  patterns.py        effect patterns: solid, breathe, color cycle, chase, rainbow, twinkle, candy cane, christmas
  scheduler.py, sun.py   timers incl. sunrise/sunset (low priority)
  store.py           data/settings.json: settings, groups, presets, schedules, aliases, lan_cache
  secrets.py         Credential Manager (Windows) / private file (Linux) / env vars
  server.py          JSON API + serves static/ (the page)
  govee/lan.py       Govee LAN API (UDP 4001 multicast scan, 4002 replies, 4003 commands)
  govee/cloud.py     Govee cloud API v1 router (openapi.api.govee.com)
  govee/desktop.py   Govee Desktop GoveeAPI.dll via pythonnet (optional)
  govee/demo.py      pretend lights
tools/               one-off inventory scripts
tests/               59 tests: pretend lights, pretend LAN light over real UDP, pretend Govee cloud over real HTTP
```

**Verified so far:**
- The automated tests pass.
- Demo mode was clicked through in a real browser.
- On the owner's old PC, Govee Desktop's API listed the lights and controlled the spotlights. That was before the app existed; it used `tools/govee_desktop_inventory.py`.

**Not yet verified with real hardware:**
- the app itself against real lights
- the LAN API
- the real Govee cloud
- scenes
- state reading

**Phase 1 below is that verification.**

**What's known about the owner's lights:**
- Govee Desktop listed about 6 lights.
- One light ("front lights") had LAN Control off.
- The owner has more lights than that, so expect the cloud to list more than the LAN scan finds.

## 4. Server setup checklist

Ask the owner which OS the server runs. The steps are written for **Windows**; Linux equivalents are noted.

1. **Power and updates:**
   - never sleep or hibernate
   - BIOS/UEFI "restore on AC power loss" = **On**, so it comes back after an outage
   - Windows Update active hours set so restarts happen at night
2. **Network:**
   - Wired Ethernet if possible.
   - A **DHCP reservation** for the server on the owner's router, so its address never changes.
   - **Same network/VLAN as the lights, Echos and Sonos.** Many routers put IoT gear on a guest network; check this.
3. **Tailscale:**
   - install it and sign in to the owner's tailnet
   - note the server's `100.x.y.z` address
   - optionally turn on MagicDNS so the page has a name
4. **Software:**
   - Python 3.11+ (tick "Add to PATH")
   - Node.js 22 LTS
   - Git
   - check versions with `python --version`, `node --version`, `git --version`
5. **Code:**
   ```
   git clone https://github.com/anthonydirect17/home-control %USERPROFILE%\home-control
   cd %USERPROFILE%\home-control
   python -m pip install -r requirements.txt
   python -m home_control --demo --no-browser
   ```
   Open http://127.0.0.1:8780/ and confirm 4 pretend lights, then stop it with Ctrl+C.
6. **Govee key:**
   - The owner runs `python -m home_control --set-govee-key` **in their own terminal** and types the key. It was emailed by Govee to the owner's Govee account email.
   - Never ask for the key in chat.
   - Govee Desktop is **not** needed on the server. Don't install it unless the owner wants per-segment effects.
7. **Windows Firewall:** scope every inbound rule by port and remote address, not by program:
   - TCP 8780 (the page) only from the Tailscale range `100.64.0.0/10`. Don't tie the rule to the server's own Tailscale address.
   - UDP 4002 only, for Govee LAN replies. 4001 and 4003 are outgoing only.
   - Later: the Sonos audio-file port (phase 4)
8. **Start it for real:**
   ```
   python -m home_control --host tailscale --no-browser
   ```
   `--host tailscale` binds to the machine's current Tailscale address, looked up at every start, so an address change never breaks it. If the owner also wants it reachable at `localhost` on the server itself, discuss binding `0.0.0.0` together with a firewall rule restricting TCP 8780 to Tailscale and localhost.
9. **Auto-start** (after phase 1 passes):
   - **Windows:** a Task Scheduler task with these settings:
     - trigger "At startup"
     - "Run whether user is logged on or not", under the owner's account, so Credential Manager entries are readable
     - "Restart if the task fails" every 1 minute
     - no time limit
   - **Linux:** a systemd service with `Restart=always`.
   - Either way, confirm it comes back after a reboot.

## 5. Phase 1: verify Govee on real hardware

With the owner watching the lights:
1. Start the app and read the console. It lists which connections came up (Govee LAN, Govee cloud) and how many lights.
2. Compare the list with the Govee Home app. For each light, note which routes the card shows ("via LAN, Cloud").
3. On one LAN light, test: on, off, brightness, color, a Govee scene, the Chase effect. Ask the owner what the light actually did.
4. After about 20 seconds, LAN light cards should say "State reported by the light".
5. On a cloud-only light, test on/off/color/scene. Effects should be refused with a clear message.
6. Try a linked group of two LAN lights with Rainbow.
7. Fix what's broken. Likely trouble spots, since none has met real hardware yet:
   - LAN discovery: multicast on a machine with several network adapters (Hyper-V, VPN, Tailscale). `govee/lan.py` sends the scan from each local IPv4 address.
   - Cloud response shapes: scene and DIY-scene payloads.
   - `colorTemInKelvin: 0` handling on specific models.
8. Report to the owner in plain language. Then add an `Effects speed` recommendation based on how the lights coped at 4 updates per second.

## 6. Phase 2: Ring connector (Node.js helper)

Library: **`ring-client-api` 14.3.0** (Node `^20 || ^22 || ^24`). The facts below were checked against its published type definitions.

**Sign-in:**
- `new RingRestClient({ email, password })`, then `getCurrentAuth()`.
- If two-step verification is needed, it throws and sets `restClient.promptFor2fa`. Then call `getAuth(code)`.
- The result's `refresh_token` is what the app keeps.
- **Ring rotates refresh tokens:** `RingApi.onRefreshTokenUpdated` emits `{ oldRefreshToken, newRefreshToken }`, and the new one **must** be saved every time, or the next start fails.

**Main objects:** `new RingApi({ refreshToken, cameraStatusPollingSeconds, locationModePollingSeconds, avoidSnapshotBatteryDrain })`, then `getLocations()` / `getCameras()`.

**Location (one per home):**
- Alarm mode:
  - `getAlarmMode()` returns `'all' | 'some' | 'none'` (away / home / disarmed)
  - `setAlarmMode(mode, bypassZids?)`, `disarm()`, `armHome()`, `armAway()`
- Siren: `soundSiren()`, `silenceSiren()`
- Devices:
  - `getDevices()` returns `RingDevice[]`
  - `onDevices`, `onDeviceDataUpdate`
  - `onConnected` is a BehaviorSubject\<boolean\>
- History: `getHistory()`, `getCameraEvents()`
- `hasAlarmBaseStation`
- **Never call `triggerBurglarAlarm()` or `triggerFireAlarm()`.** These signal Ring's professional monitoring and can dispatch emergency services.

**Alarm device (`RingDevice`):**
- Identity: `zid`, `name`, `deviceType`
- Updates: `onData`, which is a BehaviorSubject of the data below
- `data` includes:
  - `faulted?` (open or triggered)
  - `tamperStatus`
  - `batteryLevel?` and `batteryStatus`
  - `mode?`
  - `alarmInfo?: { state, faultedDevices }`
  - `siren?: { state }`
- Device types:
  - sensors: `sensor.contact`, `sensor.motion`, `sensor.glassbreak`, `sensor.flood-freeze`
  - alarm equipment: `security-panel`, `security-keypad`, `hub.redsky`, `hub.kili`
  - smoke and CO: `alarm.smoke`, `alarm.co`, `listener.smoke-co`
- Alarm states (`alarmInfo.state`):
  - `burglar-alarm`, `entry-delay`, `fire-alarm`, `co-alarm`, `panic`
  - `user-verified-*`, `*-accelerated-alarm`

**Camera (`RingCamera`):**
- Identity: `id`, `name`, `isDoorbot`, `hasLight`, `hasSiren`
- Battery: `batteryLevel`, `hasLowBattery`
- `isOffline`
- Events:
  - `onDoorbellPressed`
  - `onMotionDetected` (boolean) and `onMotionStarted`
  - `onNewNotification`, `onBatteryLevel`
- Snapshots:
  - `getSnapshot()` returns a JPEG Buffer
  - battery cameras limit snapshot frequency; see `avoidSnapshotBatteryDrain`
- Control: `setLight(on)`, `setSiren(on)`
- History and clips:
  - `getEvents()`, `videoSearch()`
  - `getRecordingUrl(dingId, { transcoded })` (needs Ring Protect, which the owner has)
- Live view: `createSimpleWebRtcSession()` with `start(sdp)` returns an answer SDP, which a browser `RTCPeerConnection` can use. That's a phase 6 item.

**Proposed shape** (adjust if you find a better one, and tell the owner):
- A `ring-bridge/` folder with its own `package.json`, pinned to `ring-client-api@14.3.0`. **The Python app starts it as a child process and restarts it if it dies.**
- **Python owns the secrets.** At start-up Python writes `{ refreshToken, port, apiSecret }` as one JSON line to the helper's **stdin**. Not in environment variables or files.
- **Helper → Python:** one JSON object per stdout line:
  - `{"type":"ready"}`
  - `{"type":"event", ...}` for motion, ding, alarm mode change, alarm state, sensor fault/clear, tamper, camera offline/online, low battery
  - `{"type":"token","refreshToken":"..."}`: Python saves it with `save_secret_value`
  - `{"type":"log","level":...}`
- **Python → helper:** HTTP on `127.0.0.1:<port>` with header `X-Api-Secret`:
  - `GET /devices`
  - `GET /cameras/:id/snapshot`
  - `GET /cameras/:id/events`
  - `GET /cameras/:id/recording/:dingId`
  - `POST /cameras/:id/light`, `POST /cameras/:id/siren`
  - `POST /locations/:id/mode`, `POST /locations/:id/siren`
- **Login:** `python -m home_control --ring-login` runs a helper login script. The **owner** types their email, password and two-step code in their own terminal, and Python stores the token as secret `ring-refresh-token` (add it to `SECRETS`).
- **Page:** a Ring tab with:
  - alarm mode and Disarm / Home / Away buttons, with an **in-page confirmation step**
  - sensors with open/closed, battery, and tamper
  - cameras with latest snapshot, light and siren toggles, recent events, and clip playback
  - a live event feed
- **Tests:** a fake helper process speaking the same stdin/stdout/HTTP protocol, so the Python side is tested without Ring. Also small Node tests for the helper's event mapping, using fake RingApi objects.

**Done when:** the owner can see every camera and alarm sensor on the page, a doorbell press and a motion event appear in the feed within seconds, and a refreshed token survives an app restart.

## 7. Phase 3: rules ("when → if → then")

**Triggers:**
- Ring: motion on camera X, doorbell pressed
- Alarm:
  - mode changed to X
  - alarm state (burglar/fire/CO/entry delay)
  - sensor X faulted (door open / motion), sensor tampered
- Device health: camera offline, battery low
- Time: sunrise/sunset with offset, a time of day
- Lights: a light turned on or off (LAN-reported)

**Conditions:**
- time window, "after dark" (sun-based)
- alarm mode is X
- day of week
- a light's current state
- **cooldown** (don't re-fire within N minutes)

**Actions:**
- Lights:
  - on/off/brightness/color/scene/effect
  - **flash N times**
  - **"for N minutes then restore what was there before"**
- Ring: camera light or siren on/off, alarm mode (only with explicit opt-in per rule)
- Speakers: Alexa announcement, run Alexa routine, Sonos play sound/announcement
- Wait N seconds (for sequences)

**Requirements:**
- Rules are stored in `data/` and edited on a Rules tab.
- Every firing is logged with what happened.
- A **"Test"** button runs a rule's actions once.
- A **dry-run** mode shows what would happen without doing it.
- Restoring lights needs a state snapshot taken before the action.

**Ship starter rules, switched off, for the owner to enable:**
- doorbell → porch lights flash
- motion at night → lights bright white for 5 minutes then restore
- alarm triggered → all lights flash red, and alarm sound on all speakers (phase 4)
- armed away → "away" lighting look
- door opened at night → hallway lights
- camera offline or low battery → warning color on a chosen light

## 8. Phases 4–6

**Phase 4: Alexa and Sonos**
- **Alexa:**
  - Library: `alexa-remote2` 8.1.1 (Node ≥16), inside the same Node helper.
  - Sign-in uses its cookie/proxy flow: it starts a local login page the owner opens in a browser once. Store the resulting cookie data as a secret.
  - Uses:
    - TTS announcements to chosen Echos
    - run existing Alexa routines
    - set volume
    - play Alexa sounds
  - It's unofficial and can break when Amazon changes things, so handle failures quietly and report them on the page.
- **Changing what Ring announces:**
  - The owner turns off Ring's built-in Alexa announcements in the Alexa app (Ring device → announcements).
  - Rules then announce exactly what the owner wants: wording, which rooms, quiet hours, and which cameras matter.
  - Confirm with the owner before telling them to turn anything off.
- **Sonos:**
  - Library: `soco` 0.32.0 (Python, local network).
  - Snapshot each speaker's state (`soco.snapshot.Snapshot`), set volume, play a sound file over HTTP, then restore.
  - The app has to serve the sound file on the **home LAN address**, because Sonos fetches it itself. That's a separate small port, firewall-limited to the LAN.
- **Alarm on every speaker:**
  - On `alarmInfo.state` in burglar/fire/CO, every Echo and the Sonos go to high volume and play a siren sound or a spoken alert, repeating until the alarm is cleared.
  - Then restore volumes.
  - **Test only with the owner present**, and warn them first because it's loud.

**Phase 5: hardening**
- **Done ahead of time:**
  - hidden auto-start with crash restart (`scripts/windows/`, `--keep-running`)
  - rotated logs (`--log-file`)
  - `GET /api/health`
  - the page PIN (`--set-pin`)
  - the private-file secrets fallback (`--secrets-store file`)
  - see [WINDOWS_SERVER_SETUP.md](WINDOWS_SERVER_SETUP.md)
- **Still to do:**
  - helper status and Ring token age in the health output once phase 2 exists
  - stopping effects automatically when the machine is busy

**Phase 6: later**
- **Google:** `pychromecast` 14.x for Nest speakers (local; announcements and sounds) when the owner buys any. Nest cameras and thermostats need Google's Device Access program (one-time fee) and are cloud-based.
- **Ring live view in the page:** WebRTC via `createSimpleWebRtcSession`.
- **Per-segment effects without Govee Desktop:** Govee's unofficial LAN `ptReal` command, model-dependent; only if the owner wants it.

## 9. Safety rules (non-negotiable)

- **Never call** `triggerBurglarAlarm`, `triggerFireAlarm`, or anything else that contacts Ring's monitoring center.
- **Arming, disarming, sirens (Ring or speakers), and camera sirens:**
  - only with the owner's explicit OK each time during development
  - in rules, only when the owner has turned that rule on themselves
- **The page asks for confirmation** before disarming or sounding a siren.
- **Keys and tokens** (Govee key, Govee Desktop GUID, Ring refresh token, Alexa cookies):
  - never in chat, the repo, logs, or the page
  - the owner types them into hidden prompts
- **Never commit `data/`**, device lists, IP addresses, the home location, or screenshots of the owner's cameras.
- **Test light effects** knowing someone may be home; ask before flashing lights late at night.

## 10. Working conventions

- **Repo:** https://github.com/anthonydirect17/home-control (public).
- **Branches:** work on a branch, open a PR, merge when the owner is happy.
- **Validate before every commit:**
  - run `python -m pytest`
  - keep tests hardware-free with fakes
  - add tests with each feature
- **Code style:** match the existing code: small modules, plain-language docstrings, no build step for the page, and colors from CSS tokens (light and dark).
- **Errors shown on the page:** say what failed and what to do, in plain words.
- **Before calling something done,** run it against the real devices with the owner.

## 11. Govee Desktop API reference

Only needed if Govee Desktop is used. This summarizes Govee's "Govee Desktop OpenAPI Instructions".

- **Requirements:**
  - Govee Desktop running **as administrator**
  - Settings → API → **Enable API**
  - the API GUID shown there
  - the light's LAN Control on
  - Scenic/Razer DreamView off
- **Loading:** `GoveeAPI\GoveeAPI.dll` in the install folder, class `GoveeAPI.ConnectGovee`, loaded with pythonnet.
- **Methods:**
  - `InitConnect(guid)`: call first
  - `DeviceSwitchControl(name, 0|1)`
  - `DeviceBrightnessControl(name, 1-100)`
  - `DeviceColorControl(name, r, g, b)`
  - `GetDeviceBaseInfo()`: JSON with Name, SegmentNums, SkuType, IsLANOn
  - `DeviceSegmentsColor(name, "#RRGGBB,#RRGGBB,...", gradient 0|1)`
- **Return codes:** `0` = OK. Errors:

  | Code | Meaning |
  |---|---|
  | 1 | not running |
  | 100 | program error |
  | 101 | init failed |
  | 102 | offline |
  | 1001 | bad GUID |
  | 1002 | empty parameter |
  | 1003 | no such device |
  | 1010 | bad color |
  | 1011 | bad brightness |
  | 4000 | no reply (often still worked) |
  | 4001 | send failed |

## 12. Questions to ask the owner at the start

1. Which OS is the server running, and is it wired to the same network as the lights, Echos and Sonos?
2. What is the server's Tailscale name or address? Should the page be reachable only over Tailscale, or also at the server's own screen?
3. Phase 1 results first, or go straight to Ring?
4. For the alarm-on-speakers feature: which speakers, what sound (siren tone or spoken), how loud, and should it stop when the alarm is disarmed?
5. What should announcements say, in which rooms, and with what quiet hours?
