# Running Home Control on an always-on Windows server

This covers installing Home Control on a Windows PC that stays on. It assumes this setup:
- the owner's Windows account signs in automatically after every restart, then locks itself
- Home Control runs **in that signed-in session**, hidden, from its own folder
- the page is reachable only over Tailscale

If someone else manages the machine (a server session or an admin), they carry out the steps marked **(server)**. Give them the exact commands, plus the undo commands listed with each step.

| Placeholder | Meaning |
|---|---|
| `C:\Apps\HomeControl` | the app's folder: the git clone, its `.venv`, and the git-ignored `data\` and `logs\` (`APP` below) |
| `PORT` | the page's port (default 8780) |
| `LAN_SUBNET` | your home network, e.g. `192.168.1.0/24` |

## 1. Software (server, once)

- **Python 3.12, installed for all users.**
- **Node.js 24 LTS.** Used from phase 2 on (the Ring and Alexa helper). `ring-client-api` supports Node 20, 22 and 24.
- **Git for Windows.**
- **The folder:** create `C:\Apps\HomeControl\` for the owner's account.

No global `pip install` and no global `npm install`. Everything the app needs goes into its own `.venv`, and later into its own `node_modules`.

## 2. Code and venv

```
git clone https://github.com/anthonydirect17/home-control C:\Apps\HomeControl
cd C:\Apps\HomeControl
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

## 3. Secrets (typed by the owner)

Each command asks with hidden typing:

```
.venv\Scripts\python -m home_control --set-pin
.venv\Scripts\python -m home_control --set-govee-key
```

- `--set-pin` sets the page PIN: **exactly 6 digits**, stored only as a salted hash.
- `--set-govee-key` checks the Govee API key with Govee (one request for the account's device list), then stores it. It prints "Govee accepted the key (N lights on the account)", or "Govee rejected this key. Nothing saved", or that Govee couldn't be asked (saved anyway). Never the key itself. Restart the app afterwards so it loads the new key.

**Where they go:** `APP\data\secrets.json`.
- The `data\` folder is git-ignored, so they never reach GitHub.
- Each time it's written, the file is locked so only the account running the app can open it: inherited permissions are removed, then that account alone is granted access.
- It sits next to the app's own settings, so the app's secrets live in its own folder.
- **Every value in it is encrypted with Windows DPAPI** for that account (current-user scope, with an app-specific entropy value). On disk each one reads `dpapi:<base64>`. Nothing is ever kept in plain text.
- A value saved by an older version, before encryption, is encrypted at the next start. The log lists the names, never the values.
- If a value can't be decrypted (another Windows account, or the account's keys were reset), the app says so and treats it as missing: save it again with the matching `--set-...` or `--ring-login` command.

**Check them:** `.venv\Scripts\python -m home_control --check-secrets` lists each secret's name and whether it is encrypted. It never prints a value. It exits with 1 if anything on disk is not encrypted or can't be decrypted.

**Alternative:** `--secrets-store credential-manager` keeps them in the account's Windows Credential Manager instead. `--secrets-store file` switches back, copying any secrets it finds there and listing names only. Restart the app after switching.

**About the page PIN:**
- **Device memory:** a device that enters the PIN is remembered for 30 days.
- **Lockout:** 5 wrong PINs in a row lock PIN entry for 15 minutes.
- **Changing the PIN** signs every device out.
- **Arming, disarming and sirens** (phase 2) ask for the PIN every time, on top of a confirmation step.

## 4. Firewall (server)

Scope every rule by **port and remote address**, never by program. Don't tie a rule to the server's own Tailscale address, which can change.

```powershell
# The page: only from Tailscale addresses.
New-NetFirewallRule -DisplayName "Home Control page (Tailscale)" -Direction Inbound -Protocol TCP `
  -LocalPort PORT -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Any

# Govee LAN API: lights answer scans and status requests on UDP 4002. That's the
# only inbound port; 4001 (scan) and 4003 (commands) are outgoing only.
New-NetFirewallRule -DisplayName "Home Control Govee LAN" -Direction Inbound -Protocol UDP `
  -LocalPort 4002 -RemoteAddress LAN_SUBNET -Action Allow -Profile Private
```

Undo:

```
Remove-NetFirewallRule -DisplayName "Home Control page (Tailscale)"
Remove-NetFirewallRule -DisplayName "Home Control Govee LAN"
```

Also limit the page port to your own devices in Tailscale's access rules (admin console).

## 5. Auto-start, hidden (server)

```powershell
cd C:\Apps\HomeControl\scripts\windows
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
Start-ScheduledTask -TaskName HomeControl
```

The defaults are the owner's account (the one running the command), `C:\Apps\HomeControl`, `--host tailscale` and port 8780. Override them with `-User`, `-Repo`, `-BindAddress` and `-Port`.

The task it creates:

| Setting | Value |
|---|---|
| Name | `HomeControl`. Keep this name: ServerWatch's restart button looks for it. |
| Triggers | Two, each repeating every minute with no end (`StopAtDurationEnd` False): (1) 1 minute after the owner's account signs in, which happens automatically after a restart; (2) once, 1 minute after the script registers the task, so the repeat runs at once without waiting for the next sign-in. A running app is left alone ("if already running: ignore new"; `LastTaskResult` 0x800710E0 is that, not an error); if its process was ended, the next minute starts it again. |
| Hidden | yes: the task is hidden in Task Scheduler's list (the app itself never shows a window either) |
| Runs | in that signed-in session (logon type Interactive, run level Limited). No password is stored, and saved logins are readable. |
| Window | none: it runs `.venv\Scripts\pythonw.exe` |
| Address | `--host tailscale`: the machine's current Tailscale address, looked up at every start |
| Crash recovery | the app restarts itself 60 s after any crash, and retries every minute until Tailscale is up after boot. If the process itself is ended, the minute repeat starts it again. Task Scheduler also retries every minute if the task fails to start. |
| Stopping on purpose | `Disable-ScheduledTask -TaskName HomeControl`, then end the task. Otherwise the minute repeat starts it again. `Enable-ScheduledTask` and `Start-ScheduledTask` bring it back. |
| Priority | below normal |
| Time limit | none |
| Log | `APP\logs\home-control.log` (1 MB × 3). IP addresses are masked in everything written to it, error reports included. It rotates while the app runs. If another program holds it open (an editor, `Get-Content -Wait`), rotation waits and is tried again later; read it with `Get-Content -Tail 50` or `Select-String`, which open and close it. |
| Execution policy | `-ExecutionPolicy Bypass` applies to this one script run only; the machine's policy isn't changed. The scripts haven't been run yet; read them first. |

Undo: `powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1`. Code, data and secrets are left as they are.

## 6. The restart test (required)

1. Restart the PC and don't touch it. It should sign in by itself and lock.
2. From another device on Tailscale, open `http://<server's Tailscale name>:PORT/api/health`. It should show the `lan` and `cloud` connections with `"up": true`, and a growing `uptime_seconds`.
3. Open the page, enter the PIN, and switch a light.
4. Read `APP\logs\home-control.log`. It should say `Govee cloud: on (key from ...secrets.json)`.
5. End `pythonw.exe` in Task Manager. The task's minute repeat should start it again within about 2 minutes; check `Get-ScheduledTaskInfo -TaskName HomeControl`. `--keep-running` covers crashes inside the app; this checks that the task covers the process being ended.
6. After phase 2, also confirm a rotated Ring token was saved across a restart.

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
- **Footprint:** keep `data\` and `logs\` small. Logs rotate on their own, and camera snapshots and clips (phase 2) are cached briefly in memory, not kept on disk.
- **No visible windows:** any helper process the app starts later (e.g. the Node helper in phase 2) must be started with `CREATE_NO_WINDOW` so no window flashes.
- **Bind addresses:** the page binds only to the Tailscale address. Internal helpers bind only to `127.0.0.1`.
