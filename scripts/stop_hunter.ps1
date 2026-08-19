<#
.SYNOPSIS
    Stops the AI Job Hunter services (listener and/or hunter daemon).

.DESCRIPTION
    Stops the SUPERVISOR first, then the python process it was watching.
    In that order, deliberately: kill the child first and the supervisor --
    which exists to restart a dead child -- immediately starts a new one.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop_hunter.ps1
    powershell -ExecutionPolicy Bypass -File scripts\stop_hunter.ps1 -Service listener
#>

[CmdletBinding()]
param(
    [ValidateSet('all', 'listener', 'hunter')]
    [string]$Service = 'all'
)

$patterns = @()
if ($Service -in @('all', 'listener')) { $patterns += '*main.py*--listen*' }
if ($Service -in @('all', 'hunter'))   { $patterns += '*main.py*--daemon*' }

$stopped = 0
foreach ($pattern in $patterns) {
    $children = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like $pattern }
    foreach ($child in $children) {
        # The supervisor first, or it restarts what we just stopped.
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($child.ParentProcessId)" -ErrorAction SilentlyContinue
        if ($parent -and $parent.Name -like 'powershell*' -and $parent.CommandLine -like '*run_service.ps1*') {
            Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped supervisor PID $($parent.ProcessId)"
        }
        Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped $pattern PID $($child.ProcessId)"
        $stopped++
    }
}

if ($stopped -eq 0) { Write-Host "Nothing was running." }
else { Write-Host "$stopped process(es) stopped." }
