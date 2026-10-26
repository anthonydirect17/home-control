# Running Home Control on an always-on Windows server

This covers installing Home Control on a Windows PC that stays on. It runs with nobody signed in, with no visible windows, and only the owner's devices on Tailscale can reach the page. The steps are generic. Anything specific to your machine goes in the placeholders:

| Placeholder | Meaning |
|---|---|
| `APPUSER` | the Windows account the app runs under; a dedicated **standard** account is best |
| `C:\Users\APPUSER\home-control` | where the repo is cloned (`REPO` below) |
| `TAILSCALE_IP` | the server's Tailscale address (`100.x.y.z`) |
| `PORT` | the port for the page |
| `LAN_SUBNET` | your home network, e.g. `192.168.1.0/24` |

Steps marked **(admin)** need an administrator. If someone else manages the machine, give them the exact commands, plus the undo commands listed with each step.

## 1. Software (admin, once)

- **Python 3.12, installed for all users.**
- **Node.js 24 LTS.** Used from phase 2 on (the Ring and Alexa helper). `ring-client-api` supports Node 20, 22 and 24.
- **Git for Windows.**
- **The app account** needs the **"Log on as a batch job"** right (Local Security Policy → Local Policies → User Rights Assignment). Without it, the auto-start task can't run with nobody signed in.

No global `pip install` and no global `npm install`. Everything the app needs goes into its own venv, and later into its own `node_modules`.

## 2. Code and venv (as APPUSER)

```
git clone https://github.com/anthonydirect17/home-control C:\Users\APPUSER\home-control
cd C:\Users\APPUSER\home-control
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
node --version
```

`node --version` should print `v24.x`.

Quick check with pretend lights, on this PC only:

```
.venv\Scripts\python -m home_control --demo --port PORT
```

The browser opens to four pretend lights. Stop it with Ctrl+C.

## 3. Secrets (as APPUSER, typed by the owner)

Each command asks with hidden typing and saves into the active secrets store. Nothing is ever written to the repo.

```
.venv\Scripts\python -m home_control --set-pin
.venv\Scripts\python -m home_control --set-govee-key
```

- `--set-pin`: the page PIN, **exactly 6 digits**. Only a salted hash is stored.
- `--set-govee-key`: the Govee API key.

**Where secrets live:**
- **Default:** Windows Credential Manager for `APPUSER`, under entries named `home-control/...`.
- **Fallback:** `%LOCALAPPDATA%\home-control\secrets.json`. Its folder and file are locked so only `APPUSER` can open them (inherited permissions removed). It's only used after you switch it on:
  ```
  .venv\Scripts\python -m home_control --secrets-store file
  ```
  - Switching copies every secret already in Credential Manager into the file, listing names only.
  - Switch back with `--secrets-store credential-manager`.
  - Restart the app after switching.

**About the page PIN:**
- **Device memory:** a device that enters the PIN is remembered for 30 days.
- **Lockout:** 5 wrong PINs in a row lock PIN entry for 15 minutes.
- **Changing the PIN** signs every device out.
- **Arming, disarming and sirens** (phase 2) ask for the PIN every time, on top of a confirmation step.

## 4. Firewall (admin)

Scope every rule by **port and remote address**, never by program.

```powershell
# The page: only from Tailscale addresses
New-NetFirewallRule -DisplayName "Home Control page (Tailscale)" -Direction Inbound -Protocol TCP `
  -LocalPort PORT -LocalAddress TAILSCALE_IP -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Any

# Govee LAN API replies (lights answer scans and status requests on UDP 4002)
New-NetFirewallRule -DisplayName "Home Control Govee LAN" -Direction Inbound -Protocol UDP `
  -LocalPort 4001-4003 -RemoteAddress LAN_SUBNET -Action Allow -Profile Private
```

Undo:

```
Remove-NetFirewallRule -DisplayName "Home Control page (Tailscale)"
Remove-NetFirewallRule -DisplayName "Home Control Govee LAN"
```

Also limit the page port to your own devices in Tailscale's access rules (admin console).

## 5. Auto-start, hidden (admin)

```powershell
cd C:\Users\APPUSER\home-control\scripts\windows
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1 -User APPUSER -Repo C:\Users\APPUSER\home-control -BindAddress TAILSCALE_IP -Port PORT
Start-ScheduledTask -TaskName HomeControl
```

The script asks for APPUSER's password in a hidden prompt; Task Scheduler stores it. The task it creates:

| Setting | Value |
|---|---|
| Trigger | "At startup" |
| Runs | whether anyone is signed in or not, with the password stored. Not "Do not store password", which may cut off Credential Manager. |
| Execution policy | `-ExecutionPolicy Bypass` applies to this one script run only; the machine's policy isn't changed. The scripts haven't been run yet; read them first. |
| Window | none: it runs `.venv\Scripts\pythonw.exe` |
| Crash recovery | the app restarts itself 60 s after any crash (`--keep-running`). Task Scheduler also retries every minute if the task itself fails. |
| Priority | below normal |
| Time limit | none |
| Log | `REPO\data\logs\home-control.log` (1 MB × 3) |

Undo: `powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1`. Code, data and secrets are left as they are.

## 6. The nobody-signed-in test (required)

1. Reboot the server and **leave it at the sign-in screen**.
2. From another device on Tailscale, open `http://TAILSCALE_IP:PORT/api/health`. It should show the `lan` and `cloud` connections with `"up": true`, and a growing `uptime_seconds`.
3. Open the page, enter the PIN, and switch a light.
4. Read `REPO\data\logs\home-control.log`. It should say `Govee cloud: on (key from Credential Manager (...))`.
   - If it says the key is `not saved yet` even though you saved it, the account's Credential Manager isn't readable in this mode. Sign in as APPUSER, run `--secrets-store file`, restart the task, and repeat the test.
5. After phase 2, also confirm a rotated Ring token was saved across a restart.
6. Kill `pythonw.exe` in Task Manager. The task should start it again within about a minute; check `Get-ScheduledTaskInfo -TaskName HomeControl`. If it doesn't, note it: `--keep-running` covers crashes inside the app, but not the process being killed.

## 7. Health endpoint

`GET /api/health` needs no PIN, so a monitor can poll it. It reports only:
- app name and version
- start time and uptime
- which connections are up (label and up/down)
- light count and number of running effects
- the time of the latest activity (a command, effect frame, state report or timer)

It never includes light names, addresses, errors or secrets.

## 8. Being a good neighbour on a shared box

- **Priority:** the task runs below normal priority.
- **Footprint:** keep `data\` small. Logs rotate on their own, and camera snapshots and clips (phase 2) are cached briefly in memory, not kept on disk.
- **No visible windows:** any helper process the app starts later (e.g. the Node helper in phase 2) must be started with `CREATE_NO_WINDOW` so no window flashes.
- **Bind addresses:** the page binds only to `TAILSCALE_IP`. Internal helpers bind only to `127.0.0.1`.
