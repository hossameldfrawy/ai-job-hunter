<#
.SYNOPSIS
    Registers the AI Job Hunter to start automatically at logon.

.DESCRIPTION
    Creates two Windows Scheduled Tasks that run at every logon:

      AI Job Hunter Listener   the HITL reply listener
      AI Job Hunter Daemon     the discovery daemon (optional, -WithHunter)

    A scheduled task is the only thing here that survives a REBOOT. Everything
    else -- a terminal, a detached process, an agent session -- is gone the
    moment the machine restarts, and the failure is silent: the review card
    still arrives on your phone and your "done" is simply ignored.

    Registered at LIMITED run level (no elevation) and only for the current
    user, because the bot reads that user's own vault, Telegram session and
    saved board logins. Running it as SYSTEM would give it none of those.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_autostart_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_autostart_task.ps1 -WithHunter
    powershell -ExecutionPolicy Bypass -File scripts\register_autostart_task.ps1 -Remove
#>

[CmdletBinding()]
param(
    # Also register the discovery daemon, not just the reply listener.
    [switch]$WithHunter,
    # Minutes between hunts for the daemon task.
    [int]$IntervalMinutes = 60,
    # Unregister both tasks instead of creating them.
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$supervisor = Join-Path $root 'scripts\run_service.ps1'

$tasks = @(
    @{ Name = 'AI Job Hunter Listener'; Service = 'listener'; Always = $true },
    @{ Name = 'AI Job Hunter Daemon';   Service = 'hunter';   Always = $false }
)

if ($Remove) {
    foreach ($task in $tasks) {
        if (Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $task.Name -Confirm:$false
            Write-Host "Removed scheduled task: $($task.Name)"
        }
    }
    Write-Host "Autostart removed. The bot will not start on its own any more."
    return
}

if (-not (Test-Path $supervisor)) {
    throw "Supervisor not found at $supervisor"
}

foreach ($task in $tasks) {
    if (-not $task.Always -and -not $WithHunter) { continue }

    $argument = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`" " +
                "-Service $($task.Service) -IntervalMinutes $IntervalMinutes"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument $argument -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # No execution time limit: these are services, not batch jobs, and the
    # default 72-hour cap would silently kill the listener after three days.
    $taskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

    if (Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task.Name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $task.Name -Action $action `
        -Trigger $trigger -Settings $taskSettings -RunLevel Limited `
        -Description "AI Job Hunter -- $($task.Service) service, supervised." | Out-Null
    Write-Host "Registered: $($task.Name)  ($($task.Service))"
}

Write-Host ""
Write-Host "  Start now without waiting for a logon:"
Write-Host "    schtasks /Run /TN `"AI Job Hunter Listener`""
Write-Host "  Check status:"
Write-Host "    Get-ScheduledTask -TaskName 'AI Job Hunter*' | Select TaskName,State"
Write-Host "  Remove:"
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\register_autostart_task.ps1 -Remove"
