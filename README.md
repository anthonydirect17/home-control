# Home Control

A control center for your smart home that runs on an always-on Windows PC and opens in your browser. Right now it controls Govee lights. Ring (cameras, doorbell, alarm, sensors), rules ("when this happens, do that") and Alexa/Sonos announcements are next.

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

Try it with pretend lights first:

```
python -m home_control --demo
```

Your browser opens at http://127.0.0.1:8765/.

### Your keys

Keys are never stored in this folder. Save each one once; typing is hidden:

```
python -m home_control --set-govee-key
python -m home_control --set-guid
```

- `--set-govee-key` saves your Govee API key (emailed by Govee).
- `--set-guid` saves the Govee Desktop API GUID. It's optional, and only needed for multi-segment effects.

On Windows they go into Credential Manager (`home-control/govee-api-key`, `home-control/govee-desktop-guid`). On Linux or macOS they go into `~/.config/home-control/secrets.json`, readable only by you. Environment variables `GOVEE_API_KEY` and `GOVEE_DESKTOP_GUID` also work.

For the Govee Desktop GUID, if you saved it under another Credential Manager name earlier, the app finds it automatically when it's the only entry with "govee" in its name.

### Start

```
run.bat
```

or `python -m home_control`.

## Good to know

- **The app must be running** for effects, timers and rules. Timers you set in the Govee Home app still run on their own.
- **Real state:** lights on your home network report their real state every 20 seconds. Lights reached only through the cloud show what this app last sent; polling them would use up Govee's daily request limit.
- **Effects speed:** effects send 4 updates per second to each light by default. Change it in Settings, and lower it if lights lag or skip.
- **Where the page opens:** the app listens on this PC only (127.0.0.1) unless you start it with `--host`. For example, `--host 100.x.y.z` uses the PC's Tailscale address, so your other devices on Tailscale can open it. The page has no login, so only use addresses on networks you trust.
- **Govee Desktop and network scanning:** if Govee Desktop is running, it may hold the network port used to find lights. Commands still work, but newly added lights won't be found until Govee Desktop is closed. The app remembers lights it has already found.

## What stays off GitHub

This repository is public. Everything personal lives in `data/`, which `.gitignore` excludes:
- light names and network addresses
- groups, effects and timers
- your latitude and longitude

Keys are only ever in Credential Manager, the private secrets file, or environment variables.

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

- Ring: cameras, doorbell, alarm, sensors, snapshots and recorded clips
- Rules: "when this happens, do that" across Ring, lights and the alarm
- Alexa and Sonos: custom announcements, and sounding the alarm on every speaker
- Google devices (Nest speakers, cameras), when you have them
