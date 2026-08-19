<#
.SYNOPSIS
    Supervises the HITL review listener. Thin wrapper over run_service.ps1.

.DESCRIPTION
    Kept as its own entry point because it is the command in the README and in
    the registered logon task, and both should keep working. The supervision
    itself -- restart-on-crash, detaching, single-instance, UTF-8 logging --
    lives in run_service.ps1, so the listener and the hunter daemon cannot
    drift apart.

.EXAMPLE
    # Run it here, Ctrl-C to stop:
    powershell -ExecutionPolicy Bypass -File scripts\run_listener.ps1

.EXAMPLE
    # Detached; closing this window leaves it running:
    powershell -ExecutionPolicy Bypass -File scripts\run_listener.ps1 -Detached

.EXAMPLE
    # At every logon, surviving a reboot:
    powershell -ExecutionPolicy Bypass -File scripts\register_autostart_task.ps1
#>

[CmdletBinding()]
param(
    [switch]$Detached,
    [int]$MaxRestarts = 0
)

$supervisor = Join-Path $PSScriptRoot 'run_service.ps1'
& $supervisor -Service listener -Detached:$Detached -MaxRestarts $MaxRestarts
