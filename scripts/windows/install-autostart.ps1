<#
.SYNOPSIS
  Registers the Home Control auto-start task: starts when the owner's account
  signs in, runs hidden in that session, restarts itself after any crash.

.DESCRIPTION
  Made for a machine whose owner account signs in automatically after every
  restart and then locks itself, so that session is always there. The task:
    - runs in -User's signed-in session (logon type Interactive, run level
      Limited). No password is stored, and saved logins (DPAPI, Credential
      Manager) are readable.
    - has two triggers, each repeating every minute with no end
      (Repetition.StopAtDurationEnd False):
        1. at -User's sign-in, plus 1 minute;
        2. once, 1 minute after this script registers it. A sign-in trigger's
           repeat only begins at the next sign-in, so this one starts the
           repeat now, without waiting for one.
      With "if already running: ignore new", a running app is left alone; if
      the app's process was ended, the next repeat starts it again within
      about a minute. While the app runs, LastTaskResult 0x800710E0 is that
      "ignore new" at work, not an error.
    - is hidden in Task Scheduler's list (the Hidden flag)
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

  Behind the Bridge: -BindAddress 127.0.0.1 -BridgeLink <the link file the
  Bridge wrote for Home Control>. Home Control then listens on this PC only and
  accepts only requests the Bridge signed (its health feed stays open).

  Undo: .\uninstall-autostart.ps1

.EXAMPLE
  .\install-autostart.ps1
  .\install-autostart.ps1 -BindAddress 127.0.0.1 -BridgeLink C:\Apps\TheBridge\data\links\home.link
  .\install-autostart.ps1 -User OWNER -Repo C:\Apps\HomeControl -BindAddress tailscale -Port 8780
#>
param(
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [string]$Repo = "C:\Apps\HomeControl",
    [string]$BindAddress = "tailscale",
    [int]$Port = 8780,
    [string]$BridgeLink = "",
    [string]$TaskName = "HomeControl"
)

$ErrorActionPreference = "Stop"

$pythonw = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Not found: $pythonw. Create the venv first (see docs/WINDOWS_SERVER_SETUP.md)."
}
$log = Join-Path $Repo "logs\home-control.log"
$arguments = "-m home_control --host $BindAddress --port $Port --no-browser --keep-running --log-file `"$log`""
if ($BridgeLink) {
    if ($BindAddress -ne "127.0.0.1") { throw "Behind the Bridge, use -BindAddress 127.0.0.1." }
    if (-not (Test-Path $BridgeLink)) { throw "Not found: $BridgeLink. Run the Bridge's --links first." }
    $arguments += " --bridge-link `"$BridgeLink`""
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $Repo
# Each trigger repeats every minute with no end. No RepetitionDuration means "indefinitely", but
# New-ScheduledTaskTrigger leaves StopAtDurationEnd True, so it is set to False explicitly.
$atSignIn = New-ScheduledTaskTrigger -AtLogOn -User $User
$atSignIn.Delay = "PT1M"
$atSignIn.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition
$atSignIn.Repetition.StopAtDurationEnd = $false
# Starts the minute repeat now, without waiting for the next sign-in.
$fromNow = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$fromNow.Repetition.StopAtDurationEnd = $false
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Priority 7 `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atSignIn, $fromNow) -Principal $principal `
    -Settings $settings -Description "Home Control: starts at sign-in, hidden, restarts on failure." -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registered task '$TaskName' for $User (logon type: $($task.Principal.LogonType), run level: $($task.Principal.RunLevel))."
Write-Host "Triggers: $($task.Triggers.Count) (expected 2)."
foreach ($t in $task.Triggers) {
    $kind = if ($t.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger") { "at sign-in, delay $($t.Delay)" } else { "once, from $($t.StartBoundary)" }
    Write-Host "  $kind; repeat every $($t.Repetition.Interval) for '$($t.Repetition.Duration)' (empty = forever), StopAtDurationEnd $($t.Repetition.StopAtDurationEnd)."
}
Write-Host "Settings: if running $($task.Settings.MultipleInstances), priority $($task.Settings.Priority), time limit $($task.Settings.ExecutionTimeLimit), hidden $($task.Settings.Hidden)."
Write-Host "Next run: $((Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)"
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it with:      Get-ScheduledTaskInfo -TaskName $TaskName"
