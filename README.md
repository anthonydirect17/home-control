# Home Control

A control center for your smart home that runs on an always-on Windows PC and opens in your browser. It controls Govee lights and Ring (cameras, doorbell, Ring Alarm and its sensors). Rules ("when this happens, do that") and Alexa/Sonos announcements are next.

- **Lights:** on/off, brightness, and color for every Govee light on your account. Lights on your home network also report their real state.
- **Govee scenes:** run Govee's built-in scenes and your own DIY scenes from the Govee app.
- **Groups:** run several lights together. A *linked* group runs an effect across all of them as one long strip; a *side by side* group runs the same effect on each light.
- **Effects:** built-in patterns (chase, candy cane, twinkle, rainbow, breathe, color cycle, Christmas fade). Adjust the settings with a live preview, then save them as your own effects.
- **Timers:** at a set time or at sunrise/sunset (with an offset), on chosen days.

## How it reaches your lights

It uses up to three connections at once and picks the best one for each command, falling back to the next if one fails:

| Connection | Needs | Good for |
|---|---|---|
| **Govee LAN API** | LAN Control turned on for the light (Govee Home app: device → settings gear → LAN Control) | Fast control, reading the light's state, effects (one color per light) |
| **Govee cloud API** | Your Govee API key | Every light on the account, Govee scenes and DIY scenes |
| **Govee Desktop** (optional) | Govee Desktop running as administrator, Settings → API → Enable API | Effects that color each segment of a strip separately |

You don't need Govee Desktop at all unless you want multi-segment effects.

## Setup

Requires Windows and Python 3.10 or newer. In Command Prompt, from this folder:

```
python -m pip install -r requirements.txt
```

Every dependency is pinned to an exact version. Only for the optional Govee Desktop connection (multi-segment effects), also run `python -m pip install -r requirements-desktop.txt`.

Try it with pretend lights first:

```
python -m home_control --demo
```

Your browser opens at http://127.0.0.1:8780/.

### Your keys

Keys are never stored in this folder. Save each one once; typing is hidden:

```
python -m home_control --set-govee-key
python -m home_control --set-guid
```

- `--set-govee-key` saves your Govee API key (emailed by Govee).
- `--set-guid` saves the Govee Desktop API GUID. It's optional, and only needed for multi-segment effects.

They go into `data/secrets.json` in the app's own folder. `data/` is git-ignored, and the file is locked so only the account running the app can open it. On Windows, `--secrets-store credential-manager` uses that account's Credential Manager instead. Environment variables `GOVEE_API_KEY` and `GOVEE_DESKTOP_GUID` also work.

### Page PIN

```
python -m home_control --set-pin
```

- **Format:** exactly 6 digits, stored only as a salted hash.
- **Device memory:** each browser is remembered for 30 days.
- **Lockout:** 5 wrong PINs lock entry for 15 minutes.
- **Sensitive actions:** arming, disarming and sirens ask for the PIN every time.
- **Demo mode** with no PIN set stays open, for trying it out.

### Start

```
run.bat
```

or `python -m home_control`. To run it on an always-on Windows server (hidden, started automatically, reachable over Tailscale), follow [docs/WINDOWS_SERVER_SETUP.md](docs/WINDOWS_SERVER_SETUP.md).

## Good to know

- **The app must be running** for effects, timers and rules. Timers you set in the Govee Home app still run on their own.
- **Real state:** lights on your home network report their real state every 20 seconds. Lights reached only through the cloud show what this app last sent; polling them would use up Govee's daily request limit.
- **Effects speed:** effects send 4 updates per second to each light by default. Change it in Settings, and lower it if lights lag or skip.
- **Where the page opens:** the app listens on this PC only (127.0.0.1) unless you start it with `--host`. `--host tailscale` uses the PC's current Tailscale address, so your other devices on Tailscale can open it. It refuses to listen on every network at once (`0.0.0.0` or `::`). The PIN protects the page, but still only expose it on networks you trust.
- **Govee Desktop and network scanning:** if Govee Desktop is running, it may hold the network port used to find lights. Commands still work, but newly added lights won't be found until Govee Desktop is closed. The app remembers lights it has already found.

## Ring

The Ring tab shows:
- **Alarm:** the Ring Alarm's mode, with Disarm / Home / Away, and the siren.
- **Cameras:** snapshots when you ask, floodlights and camera sirens, and recent recorded clips.
- **Sensors:** every door, window and motion sensor, with battery and tamper status.
- **Events:** a live feed of motion, doorbell, alarm and sensor events.

**Arming, disarming and any siren ask for the PIN every time**, on top of a confirmation step, even on a remembered device. Home Control never contacts Ring's monitoring center.

It works through a small Node.js helper in `ring-bridge/`, using `ring-client-api` 14.3.0 and needing Node 20, 22 or 24. The helper listens only on 127.0.0.1 (port 8782). Set it up once:

```
cd ring-bridge
npm ci --ignore-scripts
cd ..
python -m home_control --ring-login
```

- **`npm ci --ignore-scripts`** installs exactly the versions in `package-lock.json`. It skips the optional ffmpeg download, which isn't needed.
- **`--ring-login`** asks for your Ring email, password (hidden) and two-step code in your own terminal. It saves only Ring's sign-in token, in the secrets store. Ring rotates that token, and the app saves each new one at once.
- **In the Ring app,** this shows up in Control Center as "Home Control".

## What stays off GitHub

This repository is public. Everything personal lives in `data/`, which `.gitignore` excludes:
- light names and network addresses
- groups, effects and timers
- your latitude and longitude

Keys and the PIN hash are only ever in `data/secrets.json` (locked to the account), Credential Manager if you choose it, or environment variables.

`GET /api/health` (no PIN needed, for monitoring) shows the version, uptime, which connections are up, and the time of the last activity. It never shows names, addresses or secrets.

## Tools

- `tools/govee_inventory.py` lists lights from Govee's cloud API and a network scan.
- `tools/govee_desktop_inventory.py` lists what Govee Desktop sees. Add `--test "Light name"` to flash one light.

## Development

```
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests run against pretend lights, a pretend LAN light and a pretend Govee cloud, so they work anywhere.

## Planned

- Rules: "when this happens, do that" across Ring, lights and the alarm
- Alexa and Sonos: custom announcements, and sounding the alarm on every speaker
- Google devices (Nest speakers, cameras), when you have them
