# Home Control V2, the Bridge and the Fusion kit: the plan

**From:** the Home Control session lead.
**For:** the server's coordinator (the box parts) and the cloud coordinator (the code review); Anthony decides the open points.
**Answers:** the server coordinator's V2 brief of 2026-10-31 (all 9 sections), the server's onboarding guide (section 0), and the cloud coordinator's review of pull request 15 with Anthony's decisions (2026-10-31).
**Written:** 2026-10-31. Plain hyphens only; no secrets, account names, device names or addresses.

## 0. In one page

- **What V2 is:** our own build, with no Home Assistant, in Anthony's account on the server, in the Fusion look. It adds Sonos, Fire TV, PC status, Ring Alarm and the Sensi thermostat to V1's lights. On top of those it adds a rules engine ("when, only if, then, until") with house modes, and the screens from the canvas: the dashboard, Light Studio and Alerts.
- **The Bridge:** one front door for every app except The Desk, with six tiles: HOME, WATCHER, LAB, CRUISE, STUDIO and WORKSHOP. It comes with a shared Fusion style kit and **one health feed** that every app answers in the same shape.
- **Order:** the plan and the device layer first (this document, and pull request 15, open for review). Screens come only after both coordinators have read this plan.
- **Ring:** stays on the Node library `ring-client-api` that V1 already runs. The Python library has no Ring Alarm support at all (section 3.5).
- **Built** (pull request 15, unmerged, 263 Python and 6 Node tests passing):
  - the health feed;
  - PC status, Sonos and Fire TV;
  - Ring's V2 parts: house modes, self-stopping camera sirens and the garage reader;
  - the rules engine;
  - the Light Studio's pulse, wave and strobe.
- **The alarm (Anthony, 2026-10-31):** arming and disarming from the app with the PIN. The switch for it starts off, and Anthony turns it on in the app with his PIN. Rules may arm, change or disarm the alarm, and saving or pressing such a rule needs the PIN (sections 3.5 and 4).
- **Sensi waits** on Anthony's choice of sign-in (section 3.6).
- **Nothing reaches the box** without its own card, the server coordinator's go-ahead and Anthony's GO.

## 1. Scope and phases (as the brief sets them)

| Phase | What | Needs on the box |
|---|---|---|
| **1** | The Fusion look; lights (V1 plus the Light Studio); Sonos; Fire TV; PC status; the Bridge with the HOME tile; the health feed | One port block for the Bridge (section 6). No new inbound rule for the devices |
| **2** | Ring (events, motion, doorbell, snapshots, history, the Alarm), the garage from a Ring camera's picture, Sensi, the rules engine and Alerts screen, Sonos sounds and spoken alerts, push alerts | Ring helper packages; one LAN rule for Sonos sounds |
| **3** | Ring live view; the Workshop tile; an Alexa skill if Anthony asks | By their own cards |

## 2. How the code is laid out

- **One Python app for HOME**, the V1 app grown up, running at below-normal priority in Anthony's session as now.
- **A device layer:** one small service per kind of device, all with the same shape (`home_control/services.py`).
  - Each keeps its own state current in a background thread. The page and the health feed read it at once and never wait on a device.
  - A device that stops answering marks its service down with a plain message. The loop keeps trying, with a growing pause up to 5 minutes.
  - Interface: `status`, `state()`, `health_text()`, `act(target, action, args)`.
  - Routes: `GET /api/services` and `POST /api/services/{key}/act`, both behind the PIN cookie.
- **The Ring helper** stays a separate Node process (V1's design, already approved). It runs on 127.0.0.1 with a per-run secret, and the app supervises it.
- **The rules engine** (phase 2) runs in the app. It listens to device events and the clock, and acts through the same services.
- **Every route is marked** "web-view safe" or "Tailscale only" (section 5.4).

## 3. The devices, one by one

### 3.1 Govee lights: V1's code, kept

- **Cloud API** for every light and for Govee's own scenes and Anthony's DIY scenes. LAN Control, turned on per light, for fast patterns.
- **The Light Studio:**
  - **Patterns made in the app** (solid, pulse, wave, strobe, cycle, breathe; two colors, speed and brightness; a live preview; a saved library) are played by the server over LAN Control. That makes them smooth, and they never count against the cloud's daily request limit. V1's pattern engine already does this.
  - **Lights without LAN Control** still take solid colors and Govee scenes through the cloud. The studio marks which lights can run fast patterns.

### 3.2 Sonos soundbar: built (`home_control/sonos.py`)

- **Library:** SoCo 0.32.0 (released 2026-10-15), local control only.
- **Finding it:** an SSDP search. Windows lets the replies in as answers to our own search, so **no inbound rule** is needed.
  - It is remembered by its Sonos id, name and last address in the private settings.
  - It is found again by searching if its address changes.
- **Talking to it:** HTTP from the box to the speaker (TCP 1400), outbound only.
- **No event listener:** it is polled every 5 seconds instead. Sonos events would need an inbound rule, and polling is enough here.
- **Actions:** play, pause, next, previous, volume, mute, TV or line-in, Sonos favorites, and the soundbar's night and speech modes.
  - An optional volume ceiling is in the settings (none by default).
- **Phase 2, sounds and spoken alerts:**
  - The speaker fetches the sound from the box. The box serves it on the registered LAN port **8781**, with an inbound rule from the soundbar only, by its own card. The app reads the box's LAN address at start, so a changed lease is no problem.
  - Spoken messages are made with Windows' built-in speech engine, as a WAV file.
  - **Interrupting TV sound:** playing a sound switches the soundbar away from the TV. SoCo's snapshot puts back what was playing, but bringing the TV input back cleanly is **not yet proven** on hardware.
  - **A gentler way to test:** Sonos's "audio clip", which plays over whatever is on and lowers it. That needs one more library, `sonos-websocket`.
  - Both get tested on the soundbar before Red Alert relies on them.

### 3.3 Fire TV sticks: built (`home_control/firetv.py`)

- **Library:** adb-shell 0.4.3, network ADB, outbound only (TCP 5555 on each stick).
  - I chose it over the `androidtv` library the brief named:
    - androidtv has had no release since 2024-11;
    - it pulls in an unmaintained 2020 pre-release (`pure-python-adb`);
    - it skips authentication unless it is given a key **file**, which the secrets rule forbids.
  - The few commands needed are written directly instead, and tested.
- **The ADB key:** the app makes its own key in memory and keeps it only in the encrypted secrets file. It is never typed and never a file.
- **Pairing, once per stick:**
  1. Anthony turns on Developer Options and ADB debugging on the stick.
  2. He runs `python -m home_control --firetv-pair` in his own window.
  3. It searches the network (DIAL, the same search phones use to cast) and lists the sticks by name, so no address is typed.
  4. He approves "Always allow from this computer" on the TV.
  - Each stick is remembered by serial number in its own private file (`data\firetv.json`), and found again by its DIAL id if its address changes.
- **Status:** awake, screensaver or asleep; the current app; playing or paused. Read every 10 seconds with one command per stick.
- **Actions:** on, off, home, back, play and pause, volume, mute, and opening an app.
  - Apps come from a fixed list of streaming apps, offered only when installed. Package names are checked before use, so nothing else can be sent to the stick.
  - The TVs themselves follow through HDMI-CEC.
- **Limit:** newer sticks running Amazon's **Vega OS** have no network ADB and cannot be paired. **Anthony: which models are the sticks?** (Settings > My Fire TV > About)
- **The network search is checked** (cloud coordinator's review): an answer is used only when its description is plain http, on the very private home-network address the reply came from. Redirects are refused, so nothing can point the app at another host.

### 3.4 PC status: built (`home_control/pcstatus.py`)

- **The server itself** is "online" whenever the app runs and Tailscale is up there.
- **Another PC** is read from Tailscale's own view of the network (`tailscale status --json`, run on the server).
  - **Nothing is ever sent to that PC.** It may run trading software, so the check stays at zero contact.
- **No sleep, shutdown or wake action exists,** and a test checks that.
- **Which PCs, and their labels,** come from the private settings.

### 3.5 Ring: the doorbell, the cameras and the Ring Alarm

**The library: `ring-client-api` (Node), as in V1.** I checked the brief's question first.

- **python-ring-doorbell 0.9.14** (2026-02-28) has **no Ring Alarm support:**
  - it loads only doorbells, cameras, chimes and intercoms;
  - there is no security panel, no alarm mode, no alarm sensors and no base station siren in its source.
  - Home Assistant's Ring integration, which uses it, lists no alarm either. Its users reach the Alarm through `ring-mqtt`, which is built on `ring-client-api`.
- **ring-client-api 14.3.0** (2026-02-08, Node 20, 22 or 24; the server has Node 24) documents and implements all of it:
  - the alarm mode (disarmed, home, away);
  - arming and disarming;
  - the sensors (contact, motion, tamper, battery);
  - the base station siren;
  - the camera sirens and lights;
  - snapshots, events and history.
- **V1 already runs it** as a small helper on 127.0.0.1, with its token passed on standard input and rotated tokens saved at once. It is built and tested; its install is Part B of the approved install card.
- **Load:** about 110 MB of memory measured once loaded, and near zero CPU between events.
- **Mixing languages:** Node next to Python is already approved and in place for V1. The Python side owns the page, the rules and the secrets, and the helper only talks to Ring.

**House modes follow the Ring Alarm** (brief, section 7):

| House mode | Ring Alarm |
|---|---|
| HOME | Disarmed |
| NIGHT | Armed Home |
| AWAY | Armed Away |
| VACATION | Armed Away (with VACATION's own rules) |

**Arming and disarming from the app (Anthony, 2026-10-31: "I am fine with control inside the app"):**
- **From the page:** arm, change or disarm with the PIN, every time. There is no extra confirm step. It is Tailscale-only, like every action.
  - Ring refuses to arm with a faulted sensor unless it is bypassed, and bypassing stays in the Ring app. The app shows Ring's refusal plainly.
- **The switch:** `allow_app_arming` starts **off**.
  - Anthony turns it on (or off) in the app with his PIN, through `POST /api/mode/app-arming`, and the page shows its state.
  - The page cannot change it any other way.
- **From rules:** a rule may arm, change the level (for example AWAY to NIGHT) or disarm. It needs the switch on, and the PIN every time the rule is saved or pressed by hand (section 4).
- **Logged:** every change a rule makes is written to the log with the rule, its trigger and the run.
- **Ring's plan:** arming from any app needs a Ring Protect plan in the US (Ring's plans page). Systems bought before 2023-03-29 may be grandfathered. **Anthony: which plan do you have?** The app shows Ring's own refusal plainly if arming is not allowed.

**Sirens:**
- **Camera sirens** (the cameras that have one) may be used by rules, about 30 seconds a time. Ring publishes no length for a siren started from an app, so **the app stops it itself after 30 seconds**:
  - the stop timer is set before the "on" call, so a call that fails after Ring acted still gets its "off";
  - turning it off is retried;
  - running sirens and their deadlines are kept in `data\sirens.json`. If the app is stopped mid-siren, the next start turns off any that are past due as soon as Ring connects.
- **The base station siren:** the library starts it with a plain panel command, not the monitoring call.
  - Ring does not say whether its center is notified when it sounds. Ring's SOS page calls "sound siren only" "without dispatch", but that is not an explicit statement.
  - **Proposal:** rules never sound the base station siren. It stays a manual, PIN-confirmed button, as in V1, until Anthony decides otherwise (still open).
- **Never:** the library's burglar and fire alarm calls do contact Ring's monitoring center. A test fails the build if their names ever appear in the helper or the app.

**Snapshots and the garage:**
- **Wired cameras:** a snapshot every 10 seconds at most, even during recording.
- **Battery cameras:** Ring allows about one snapshot every 10 minutes, and none while a clip is recording (about 30 to 40 seconds after motion). A motion snapshot from a battery camera is often stale.
- **The garage camera** (picked by Anthony on the setup screen; the app reads from Ring whether it is wired or on battery):
  - **Wired:** a snapshot every minute, and one 15 seconds after motion (one per burst of motion).
  - **Battery:** a snapshot every 10 minutes, and none extra after motion: Ring serves a battery camera's picture from its 10-minute store, so it would be the same one. After an error the next try still waits the 10 minutes.
  - **Open or closed:** the marked corner is compared with the saved "open" and "closed" references by correlation of a small greyscale patch, which ignores brightness and contrast. There is no AI model.
  - **"Can't tell"** when the corner shows almost no detail, matches neither well, or matches both about equally. The page then shows "Can't tell" with the time of the last sure reading.
  - **No false change at start:** the first sure reading after a start (or a new camera) only sets the state, so a "garage open" rule doesn't fire on every restart.
  - **"Last seen"** when the newest picture is older than 30 minutes. The page shows "Last seen closed at 18:05" in a dimmed style.
  - **Night:** separate night references are saved if the corner looks different in infrared.
  - The comparison needs the Pillow library (13.0.0) to read the JPEG.
- **Snapshots are kept in memory only:** the latest one per camera and the garage references. The references are saved in the private data folder.
- **No recording on the box in phases 1 and 2.** Recordings stay with Ring and Anthony's plan, and the app links to them.

**Live view (phase 3):** the library can open a WebRTC stream. Passing it to the phone needs a media relay on the box, which is heavier work with its own load and its own card.

**Names** come from Ring, as Anthony set them in the Ring app (brief, section 7). Nothing is typed.

### 3.6 Sensi thermostat: there is no ready library

- **What exists:**
  - There is no Sensi library on PyPI.
  - Home Assistant's core has no Sensi integration.
  - The one maintained client is **iprak/sensi** (MIT, version 2.2.3 of 2026-10-12, actively maintained). It is built into a Home Assistant add-on, not a library.
- **How it works:**
  - **Sign-in:** a refresh token, copied once from the browser. Sensi's own sign-in has a reCAPTCHA that a program cannot pass.
  - **Token use:** the token is exchanged at Sensi's OAuth endpoint, then kept rotating.
  - **Data:** a Socket.IO connection to Sensi's cloud. It reads temperature, humidity, setpoints, mode and fan, and sets the setpoint and mode.
- **Plan for phase 2:**
  - a small client of our own, modelled on iprak/sensi with credit;
  - one new library, `python-socketio`, pinned with its dependencies;
  - the token kept encrypted and rotated like Ring's.
- **The protocol** (read from iprak/sensi at `96b11bd`):
  - the refresh token is exchanged at `oauth.sensiapi.io/token`;
  - live data arrives as "state" events over Socket.IO at `rt.sensiapi.io`, path `/thermostat`, over WebSocket;
  - changes go out as `set_temperature`, `set_operating_mode` and `set_fan_mode`.
- **Sensi's app client ID and secret** (the token exchange needs them): **not in the repo** (cloud coordinator, 2026-10-31). Anthony types them once into a hidden prompt, and they are stored encrypted like his own secrets.
  - The steps will show where to copy them from.
  - No copy, hash or fragment goes in code, tests, docs or commits; tests use obvious fakes.
- **Still Anthony's:** the browser-token sign-in (proposed) or Seam.
- **What Anthony would do once:** sign in at Sensi's website with the browser's developer tools open, and paste the token into the app's hidden prompt. I will write the steps.
- **Known weak points** (from iprak/sensi's open issues):
  - the connection drops and reconnects about every 31 seconds;
  - data can stop updating while the heat is off;
  - two programs using one token knock each other out.
  The app shows the thermostat's data age, and "not answering" plainly.
- **Paid alternative:** Seam, a cloud service that supports Sensi through a hosted sign-in. **Anthony's choice if the browser step is unwelcome.**

### 3.7 The garage

The garage status comes from a Ring camera's picture (section 3.5). It is status only; there is no MyQ control and no ratgdo.

### 3.8 Echo speakers

The Echos are not controlled: Amazon offers no API for that. Alexa keeps working with each brand's own skill. A custom Alexa skill for V2's own scenes is phase 3, if Anthony asks.

### 3.9 Workshop (the sixth tile; phase 3, not urgent)

- **Printer status, read only:** this works with the P1S in its normal cloud mode, so Bambu Handy keeps working.
  - It uses the printer's local MQTT (TLS, port 8883), with the printer's LAN access code typed by Anthony into a hidden prompt and kept encrypted.
  - Bambu's January 2025 change locked control, not reading. The Home Assistant Bambu integration confirms reads still work on current firmware.
  - It shows progress, layer, temperatures, time to done and the AMS (which fills the filament shelf for Bambu's own spools).
  - A full refresh is asked for at most every 5 minutes; the rest comes as pushed changes.
  - **First a read-only test** against the P1S; only if it fails does Anthony get the LAN-mode choice.
- **The app never starts a print,** and has no pause or stop (those stay in Bambu Handy).
- **The library** of print files: **waiting on the 3D print session's inventory** and Anthony's decision on where the files live. The brief's proposal is a "Workshop" folder in Anthony's Google Drive.
- **The ledger, filament shelf, maintenance and queue** are data the app keeps itself.
- **Energy and cost per print:** from the printer's Govee energy plug, once its readings are confirmed readable.

## 4. Rules and alerts (phase 2)

- **Form:** **when** (trigger), **only if** (conditions), **then** (actions), **until** (end), each as data, edited on the Alerts screen.
  - **Triggers:**
    - Ring motion or a doorbell press;
    - an alarm sensor opening;
    - the time, or sunset and sunrise (V1's sun code);
    - the house mode changing;
    - the garage opening or closing;
    - a button on the page;
    - phase 3: a print finishing or failing.
  - **Conditions:** a time window, the house mode, which camera, and a cool-down.
  - **Actions:**
    - a light pattern or scene;
    - a camera siren (about 30 seconds);
    - a Sonos sound or spoken message;
    - snapshots;
    - Fire TV;
    - the thermostat;
    - the house mode: arm, change the level or disarm (Anthony, 2026-10-31), once the arming switch is on;
    - a push alert to Anthony's phone.
  - **Until:** STOP, a time limit, or a condition (for example the house mode changing).
- **How long a run lasts:**
  - **always capped:** "until STOP" or a house mode with no time ends after 60 minutes at most, so a forgotten run can't keep a security rule from firing again;
  - **no "until":** 5 minutes when the rule starts something that lasts (a light pattern, a camera siren), so a siren keeps its own 30 seconds; otherwise the run ends as soon as its actions are done.
- **Loop guard for the alarm:**
  - a house-mode trigger ignores changes made by a rule, including Ring's echo of them;
  - rules change the alarm at most once a minute.
  So two rules can never pass the alarm back and forth.
- **The safe test button** runs only the lights and the phone alert.
- **The PIN, every time,** for saving or pressing a rule that sounds a camera siren or changes the alarm (arm, change or disarm).
- **How a run works:**
  - It runs on its own thread. The push alert goes first, then the lights, sirens and snapshots.
  - When it ends, the lights go back to what they were doing, including an effect that was running.
- **The phone's STOP button** calls `/api/stop/<token>`. This is the one route open without the PIN. Its path is shared with no other route, and the token is random, per run and single use.
- **"Red Alert" (Anthony's example):** motion while AWAY or NIGHT sets the lights to a red pulse and sounds the camera sirens. It plays an alarm on the Sonos, takes snapshots and sends a push alert with a STOP button. It runs until STOP or 5 minutes.
- **Limits stated, as the brief asks:**
  - Ring keeps its own motion clips with Anthony's plan; the box keeps none.
  - The Echos cannot be set off from the app.
  - The base station siren is not used by rules (section 3.5).
- **Push alerts: the service is Anthony's choice, later.** The code allows either:
  - **ntfy:** free, with an iPhone and Android app.
    - Its STOP button makes the phone itself call the app over Tailscale.
    - On the free hosted service the topic name is the only protection, so anyone who learned it could send a fake alert. The STOP button therefore carries its own secret, checked by the app.
  - **Pushover:** a $5 one-time app per platform. Very reliable, with an emergency mode that repeats until acknowledged.
    - It has no custom buttons; STOP would be a link.
    - The app learns of the acknowledgement by asking Pushover's API.
  - Either way, its secret is typed by Anthony into a hidden prompt and kept encrypted.

## 5. The Bridge and the Fusion kit

### 5.1 What it is

- **One page, "The Bridge",** on one port on the server's Tailscale address. Every app except The Desk is reached through it, at its own path:
  - `/home/`
  - `/watcher/`
  - `/lab/`
  - `/cruise/`
  - `/studio/`
  - `/workshop/`
- **Each app keeps running as its own program,** on 127.0.0.1 only. The Bridge passes its traffic through: pages, API calls, live updates (WebSocket and server-sent events) and images.
  - One app stopping never takes the others down: its tile turns red, from its health feed.
  - **As each app moves in,** its own network opening (8780, 8820, 8811 and so on) closes by card.
- **The Desk stays separate.** The Bridge shows a small "The Desk: healthy" light.
  - **Approved by the cloud coordinator (2026-10-31), on these conditions:**
    - one route only, `GET /api/health`; no other route of The Desk, and never a write;
    - over Tailscale only, never through the Cloudflare tunnel, with no keys or cookies;
    - at most once a minute, with a 5-second time limit. A failure shows "not answering" until the next minute, with no fast retries (The Desk restarts briefly when it updates);
    - only `status` and `version` are used. The other fields are never stored, logged or passed on, and the Watcher records only the light;
    - The Desk's address is read at start, never written into the repo;
    - if the box needs a firewall or port change for it, that is a request card to the server's coordinator.
- **Repo:** **a new private repo for the Bridge and the kit** (Anthony, 2026-10-31), which Anthony creates. Under the CI rule, a private repo's first server card asks for a runner. Until the repo exists, the kit's first pieces (the health feed) live in Home Control's repo.
  - **Cost for the box:** one more private repo with a CI runner.

### 5.2 One sign-in at the door

- **The Bridge holds the only sign-in:**
  - the page PIN, 6 digits, as V1's (salted hash, encrypted, 5 wrong tries then 15 minutes locked);
  - devices remembered for 30 days.
- **How apps trust the Bridge:**
  - On every passed request the Bridge adds a header signed with a per-app secret. Each app's secret is made by the Bridge and given to that app in its own private settings, encrypted.
  - An app accepts only requests that carry a valid signature. 127.0.0.1 is not trusted by itself, because any program on the box can reach it.
- **Sensitive actions** (the alarm, the base station siren):
  - the app asks the Bridge to confirm the PIN;
  - the Bridge answers with a one-time token, valid for 60 seconds and for that one action;
  - the app checks the token.
- **Signing out** happens at the Bridge, for every app at once.

### 5.3 The tiles

There are six tiles, as on the canvas, and a new app joins the tile it fits. Each tile shows its app's health feed:
- the dot comes from `status`;
- the line comes from `summary`;
- the small text comes from the first values.

The command line ("jump to") searches the tiles' keywords. The Bridge's quick buttons (all lights off, Game Mode, the alarm) call the apps' own routes; the alarm button needs the PIN.

| Tile | App | Owner |
|---|---|---|
| HOME (large) | Home Control V2 | Home Control lead |
| WATCHER | Watcher (ServerWatch folds in), BoxStats, the CI log | Its owner; **I give it the kit, the top bar and HOME's feed** (Anthony, 2026-10-31) |
| LAB | Bot-Lab runs, Centurion, Markup Studio (a link to HOME), the master data line | The cloud coordinator's projects |
| CRUISE | Cruise tracker | Cruise lead |
| STUDIO | New Thread | New Thread lead |
| WORKSHOP | 3D printing | Home Control lead (phase 3) |

### 5.4 "Web-view safe" or "Tailscale only"

- **Every route is marked one or the other,** in the app and in the Bridge.
- **Tailscale only for now** (Anthony). If a public address is added later, the Bridge would take the Cloudflare tunnel's traffic on a separate 127.0.0.1 listener. On that listener it serves only "web-view safe" routes:
  - statuses;
  - the Watcher summary;
  - lights and scenes.
- **Live camera view, the Ring Alarm and every other action** answer only on the Tailscale listener. Nothing is built for the public listener until Anthony asks.

### 5.5 The Fusion kit (one copy, served by the Bridge at `/kit/`)

- **Tokens:**
  - the ground #060a1f, with its faint grid;
  - violet #a77bff and electric blue #38c8ff;
  - text colors with readable contrast;
  - the ok, warn and down colors.
- **Type:** Orbitron for headings and numbers, Exo 2 for the rest. The fonts are served by the Bridge, not Google, so the page works when the internet is down.
- **Parts:** cut-corner panels with the violet-to-blue title band; buttons at least 44 px; chips; ring gauges with ticks; the slow-turning outer rings; the scan line on video tiles; toggles; sliders.
- **Motion:** about 0.3 s on every control. All of it respects the phone's "reduce motion" setting.
- **The shared top bar:**
  - the **Bridge** button at top left, on every screen of every app;
  - a "back to this tile" button on screens inside a tile (for example the Light Studio, or Alerts inside HOME);
  - the clock and the house mode.
  Apps load it from the kit, so none can leave it out.
- **The health feed** (`docs/HEALTH_FEED.md`, schema `fusion-health/1`): every app answers `GET /api/health` in one shape:
  - a status (ok, warn or down);
  - a one-line summary;
  - up to 6 key values.
  The Bridge tiles and the Watcher read only this; no app invents its own. `check_feed()` refuses a feed with an address, an e-mail address or a private name. HOME already answers in this shape (V1's fields stay beside it for older readers).
- **What each app must do to live behind the Bridge:**
  - use relative links, or the base path the Bridge passes in a header;
  - answer the health feed;
  - check the Bridge's signature;
  - load the kit and the top bar;
  - mark its routes.
  An app that can't (for example one with absolute links built in) says why in its own card, and is changed before it moves in.

## 6. What the box needs (for the server's coordinator)

| Item | Phase | Detail |
|---|---|---|
| Where it runs | 1 | As now: Anthony's account, the two-trigger hidden task (repo commit `788ab73` onward), below-normal priority, IgnoreNew. Every update disables the task first |
| The Bridge's port | 1 | **A block from 8840 upward** (for example 8840 to 8849): the Bridge on the Tailscale address, with its own task, firewall rule (the four devices, like 8800 and 8820), Tailscale policy line and `tests` lines |
| HOME behind the Bridge | 1 | HOME moves from the Tailscale address to 127.0.0.1:8780. Then the "HomeControl 8780" firewall rule and policy line close |
| Sonos | 1 | Nothing new: discovery replies are let in by Windows, and control is outbound |
| Sonos sounds | 2 | TCP 8781 on the box's LAN address, inbound from the soundbar only (Private profile) |
| Fire TV | 1 | Nothing new: outbound TCP 5555. Anthony pairs each stick at the console |
| PC status | 1 | Nothing new: reads the local Tailscale status |
| Ring helper | 2 | `npm ci --ignore-scripts` in `ring-bridge` (Part B of the approved install card); 127.0.0.1:8782 |
| Sensi | 2 | Outbound only (Sensi's cloud). One new Python library with its dependencies |
| Workshop | 3 | Outbound TCP 8883 to the printer. By its own card |
| Govee UDP 4002 | as now | Unchanged |

**Secrets** (each typed by Anthony into a hidden prompt, or made by the app; all in `data\secrets.json`, DPAPI-encrypted, locked to his account; never in the repo, a log, a report or a command line):

| Secret | Phase | Notes |
|---|---|---|
| govee-api-key | 1 | Since V1 |
| page-pin-hash | 1 | Moves to the Bridge |
| session-key | 1 | Made by the app; moves to the Bridge |
| firetv-adb-key | 1 | Made by the app at pairing; never typed |
| ring-refresh-token | 2 | Since V1 |
| Sensi token | 2 | |
| push service secret | 2 | |
| Bambu access code | 3 | |
| Bridge's per-app signing secrets | 1 | |

**Data:**
- the latest snapshot per camera, in memory;
- garage references in `data\` (a few small images);
- the event history as a bounded file (the newest 5,000 events, about 1 MB);
- no recording;
- logs rotate at 1 MB, three kept, with addresses masked.

**Load:**
- **Measured:**
  - HOME 0.5.0 on the box: 57 MB.
  - The new libraries add 21 MB (SoCo 12 MB, adb-shell with cryptography 9 MB, measured here).
  - The Ring helper: about 110 MB.
- **Expected:** the Bridge about 50 MB. Total about 250 MB.
- **CPU:** polling is light: Sonos one small HTTP call every 5 s, each Fire TV one command every 10 s, Tailscale status every 30 s. Expected well under 1% of one core at rest.
- **Read on the box** at each install, as before.

**Never:**
- anything on The Desk, its port, tunnel or folders;
- nothing on WORK or HOME beyond the read-only Tailscale status;
- no wake, sleep or shutdown of any PC.

## 7. Order of work

1. **Done:**
   - this plan, to both coordinators;
   - pull request 15: the device layer, house modes, sirens, the garage reader and the rules engine. It had an independent review, and the cloud coordinator's review (changes requested, now made). It is **left open** until the re-review and Anthony's GO.
2. **Next, in code, without the box:** Sensi's client, once Anthony chooses the sign-in (section 3.6).
3. **After both coordinators have read this plan:** the Fusion kit and the Bridge (in the new private repo, once Anthony creates it), then HOME's screens in the Fusion look: the dashboard, the Light Studio, Alerts.
4. **Cards, in Anthony's server order:**
   - the 0.5.2 update card (already written);
   - Ring Part B;
   - then V2 phase 1 (the Bridge, HOME behind it, the new devices, the new look) as one card;
   - then phase 2.

## 8. Asks

**The server's coordinator:**
- a port block for the Bridge (8840 upward);
- reading this plan's box parts (section 6) before I write the phase 1 card.

**Cloud coordinator:** the re-review of pull request 15 after the changes it asked for. Its rulings on The Desk's health light (section 5.1) and Sensi's client secret (section 3.6) are applied.

**Anthony: decided on 2026-10-31.**
- Arming and disarming from the app, with the PIN only.
- Rules may change and disarm the alarm.
- The garage state stays on the health route.

**Anthony: still open** (nothing is built for these until he decides):
1. **The base station siren:** keep it as a manual PIN button and out of rules (proposed), or something else.
2. **The Ring plan** (arming from an app needs one).
3. **The Fire TV models** (any on Vega OS cannot be paired).
4. **Sensi:** the one-time browser step (proposed), or Seam (paid).
5. **Push alerts:** ntfy or Pushover, later.
6. **The Workshop's print-file home:** waiting on the 3D print session's inventory.
7. **The new private repo** for the Bridge and the kit: Anthony creates it.
