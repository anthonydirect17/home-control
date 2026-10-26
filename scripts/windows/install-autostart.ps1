<#
.SYNOPSIS
  Registers the Home Control auto-start task: starts at boot, runs whether or
  not anyone is signed in, with no visible window.

.DESCRIPTION
  Run in an elevated (administrator) PowerShell. Tasks with an "At startup"
  trigger can only be registered by an administrator.

  The task:
    - runs as the account given in -User, with its password stored (so the
      account's Credential Manager entries are available; do not switch it to
      "Do not store password")
    - starts <Repo>\.venv\Scripts\pythonw.exe, which never opens a window
    - runs the app with --keep-running, so it restarts itself 60 s after any
      crash; Task Scheduler's own restart-on-failure is a second safety net
    - runs at below-normal priority so other work on the machine comes first
    - has no time limit
  Output goes to <Repo>\data\logs\home-control.log (rotated at 1 MB, 3 kept).

  The account needs the "Log on as a batch job" right (Local Security Policy >
  User Rights Assignment) to run with nobody signed in.

  Undo: .\uninstall-autostart.ps1 -TaskName <same name>

.EXAMPLE
  .\install-autostart.ps1 -User APPUSER -Repo C:\Users\APPUSER\home-control -BindAddress TAILSCALE_IP -Port PORT
#>
param(
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$BindAddress,
    [Parameter(Mandatory = $true)][int]$Port,
    [string]$TaskName = "HomeControl"
)

$ErrorActionPreference = "Stop"

$pythonw = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Not found: $pythonw. Create the venv first (see docs/WINDOWS_SERVER_SETUP.md)."
}
$log = Join-Path $Repo "data\logs\home-control.log"
$arguments = "-m home_control --host $BindAddress --port $Port --no-browser --keep-running --log-file `"$log`""

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Priority 7 `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# Typed into a hidden prompt; never written anywhere by this script.
$cred = Get-Credential -UserName $User -Message "Password for $User (stored by Task Scheduler so the task runs with nobody signed in)"
$plain = $cred.GetNetworkCredential().Password

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $User -Password $plain -RunLevel Limited `
    -Description "Home Control: starts at boot, hidden, restarts on failure." -Force | Out-Null
$plain = $null

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registered task '$TaskName' for $User (logon type: $($task.Principal.LogonType))."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it with:      Get-ScheduledTaskInfo -TaskName $TaskName"
