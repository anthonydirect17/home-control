<#
.SYNOPSIS
  Registers the Home Control auto-start task: starts when the owner's account
  signs in, runs hidden in that session, restarts itself after any crash.

.DESCRIPTION
  Made for a machine whose owner account signs in automatically after every
  restart and then locks itself, so that session is always there. The task:
    - starts 1 minute after -User signs in and runs in that signed-in session
      (logon type Interactive, run level Limited). No password is stored, and
      saved logins (DPAPI, Credential Manager) are readable.
    - repeats its start every minute while that session lasts. With "if already
      running: ignore new", a running app is left alone; if the app's process
      was ended, it is started again within about a minute.
    - starts <Repo>\.venv\Scripts\pythonw.exe, which never opens a window
    - runs the app with --keep-running, so it restarts itself 60 s after any
      crash, and keeps retrying every minute until Tailscale is up after boot;
      Task Scheduler's own restart-on-failure is a second safety net
    - runs at below-normal priority, so other work on the machine comes first
    - has no time limit
  Output goes to <Repo>\logs\home-control.log (rotated at 1 MB, 3 kept, with
  IP addresses masked).
  To stop the app on purpose, disable the task first (Disable-ScheduledTask),
  or the minute repeat starts it again.
  The task name must stay "HomeControl": ServerWatch's restart button looks
  for exactly that name.

  Undo: .\uninstall-autostart.ps1

.EXAMPLE
  .\install-autostart.ps1
  .\install-autostart.ps1 -User OWNER -Repo C:\Apps\HomeControl -BindAddress tailscale -Port 8780
#>
param(
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [string]$Repo = "C:\Apps\HomeControl",
    [string]$BindAddress = "tailscale",
    [int]$Port = 8780,
    [string]$TaskName = "HomeControl"
)

$ErrorActionPreference = "Stop"

$pythonw = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Not found: $pythonw. Create the venv first (see docs/WINDOWS_SERVER_SETUP.md)."
}
$log = Join-Path $Repo "logs\home-control.log"
$arguments = "-m home_control --host $BindAddress --port $Port --no-browser --keep-running --log-file `"$log`""

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$trigger.Delay = "PT1M"
# Repeat every minute, with no end (no RepetitionDuration means "indefinitely").
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Priority 7 `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Home Control: starts at sign-in, hidden, restarts on failure." -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registered task '$TaskName' for $User (logon type: $($task.Principal.LogonType), run level: $($task.Principal.RunLevel))."
$t = $task.Triggers[0]
Write-Host "Trigger: at sign-in, delay $($t.Delay), repeat every $($t.Repetition.Interval) for '$($t.Repetition.Duration)' (empty = forever)."
Write-Host "Settings: if running $($task.Settings.MultipleInstances), priority $($task.Settings.Priority), time limit $($task.Settings.ExecutionTimeLimit)."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it with:      Get-ScheduledTaskInfo -TaskName $TaskName"
