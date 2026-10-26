# Home Control

A control center for Govee lights that runs on your Windows PC and opens in your browser. Ring cameras are planned for a later version.

- **Lights:** on/off, brightness, and color for every light Govee Desktop can reach.
- **Groups:** run several lights together. A *linked* group runs an effect across all of them as one long strip; a *side by side* group runs the same effect on each light.
- **Effects:** built-in patterns (chase, candy cane, twinkle, rainbow, breathe, color cycle, Christmas fade). Adjust the settings with a live preview, then save them as your own effects.
- **Timers:** at a set time or at sunrise/sunset (with an offset), on chosen days. Each timer can turn lights on or off, set brightness or color, start an effect, or stop one.

It talks to the lights through **Govee Desktop's OpenAPI**: the `GoveeAPI.dll` that comes with Govee Desktop.

## Requirements

- Windows, with Python 3.10 or newer
- [Govee Desktop](https://desktop.govee.com/download) running **as administrator**, with **Settings → API → Enable API** checked
- **LAN Control** turned on for each light (Govee Home phone app: device → settings gear → LAN Control)
- **Scenic DreamView** and **Razer DreamView** turned off in Govee Desktop while this app is in control

## Setup

In Command Prompt, from this folder:

```
python -m pip install -r requirements.txt
```

Try it with pretend lights first (no Govee needed):

```
python -m home_control --demo
```

Your browser opens at http://127.0.0.1:8765/.

## Connecting to your lights

The app needs the **API GUID** from Govee Desktop's Settings → API screen. It never stores the GUID in this folder. It looks in this order:

1. The `GOVEE_DESKTOP_GUID` environment variable.
2. Windows Credential Manager, under `home-control/govee-desktop-guid`. Save it there once with:
   ```
   python -m home_control --set-guid
   ```
3. Any other Credential Manager entry with "govee" in its name, if there is exactly one. If there are several, put the right one's name in `data/settings.json` as `"guid_credential"`.

Then start it:

```
run.bat
```

or `python -m home_control`. If Govee Desktop rejects the connection (error 1001 or 101), run Command Prompt as administrator and try again.

## Good to know

- **The PC has to be on.** Timers and effects only run while the app is running.
- **State shown is what this app last sent.** Govee Desktop can't report a light's current state, so changes made in the phone app won't show here.
- **Speed:** effects send 4 updates per second to each light by default. Change it in Settings. Lower it if lights lag or skip.
- **This PC only:** the app listens on this PC only (127.0.0.1). Other devices on your network can't reach it.

## What stays off GitHub

This repository is public. Everything personal lives in `data/`, which `.gitignore` excludes:
- device names, groups, effects and timers
- your latitude and longitude

The GUID is only ever in Credential Manager or an environment variable. The device lists written by the scripts in `tools/` are excluded too.

## Tools

- `tools/govee_desktop_inventory.py` lists every device Govee Desktop can see, using the same API. Add `--test "Device Name"` to flash one light.
- `tools/govee_inventory.py` lists devices from Govee's cloud API (needs a Govee API key from the phone app) and from a LAN scan. Use it for lights Govee Desktop doesn't show.

## Development

```
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests use the demo backend, so they run anywhere, not just on Windows.

## Planned

- Lights that Govee Desktop doesn't list, via Govee's cloud API
- Ring cameras: snapshots, motion and doorbell events, floodlight control
- Effects that span groups mixing strips and single bulbs more evenly
