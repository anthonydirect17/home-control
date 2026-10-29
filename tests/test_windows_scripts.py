"""The auto-start scripts can't run here (no PowerShell), so this reads them and checks the settings the server relies on."""

import os
import re

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "windows")


def read(name):
    with open(os.path.join(SCRIPTS, name), encoding="utf-8") as f:
        return f.read()


def code_lines(text):
    """The script without its comment-based help and # comments."""
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_task_has_two_never_ending_minute_triggers():
    code = code_lines(read("install-autostart.ps1"))
    assert "-Trigger @($atSignIn, $fromNow)" in code
    assert re.search(r"\$atSignIn = New-ScheduledTaskTrigger -AtLogOn -User \$User", code)
    assert '$atSignIn.Delay = "PT1M"' in code
    assert re.search(r"\$fromNow = New-ScheduledTaskTrigger -Once -At \(Get-Date\)\.AddMinutes\(1\) "
                     r"-RepetitionInterval \(New-TimeSpan -Minutes 1\)", code)
    # Both repeat every minute with no end: no duration, and StopAtDurationEnd turned off on each.
    assert "$atSignIn.Repetition.StopAtDurationEnd = $false" in code
    assert "$fromNow.Repetition.StopAtDurationEnd = $false" in code
    assert "RepetitionDuration" not in code


def test_task_settings_and_principal():
    code = code_lines(read("install-autostart.ps1"))
    assert "-LogonType Interactive -RunLevel Limited" in code
    assert "Highest" not in code
    for setting in ("-MultipleInstances IgnoreNew", "-Priority 7", "-Hidden",
                    "-ExecutionTimeLimit ([TimeSpan]::Zero)"):
        assert setting in code, setting
    assert '[string]$TaskName = "HomeControl"' in code  # ServerWatch's restart button looks for this name
    assert "--host $BindAddress" in code and '[string]$BindAddress = "tailscale"' in code
    assert "pythonw.exe" in code and "--keep-running" in code


def test_script_prints_what_the_server_reads_back():
    code = code_lines(read("install-autostart.ps1"))
    for shown in ("Triggers: $($task.Triggers.Count) (expected 2).", "StopAtDurationEnd $($t.Repetition.StopAtDurationEnd)",
                  "hidden $($task.Settings.Hidden)", "if running $($task.Settings.MultipleInstances)",
                  "Next run:"):
        assert shown in code, shown


def test_uninstall_removes_the_same_task():
    assert '[string]$TaskName = "HomeControl"' in read("uninstall-autostart.ps1")


def test_scripts_use_plain_hyphens_only():
    for name in ("install-autostart.ps1", "uninstall-autostart.ps1"):
        text = read(name)
        assert "–" not in text and "—" not in text, name
