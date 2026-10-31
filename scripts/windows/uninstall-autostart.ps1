<#
.SYNOPSIS
  Disables, stops and removes the Home Control auto-start task. Run in an elevated PowerShell.
  Leaves the code, the data folder and all saved secrets untouched.
#>
param([string]$TaskName = "HomeControl")

$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null   # first, so the minute repeat can't start it again
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'."
} else {
    Write-Host "No task named '$TaskName'."
}
