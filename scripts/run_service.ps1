<#
.SYNOPSIS
    Supervises one long-running AI Job Hunter service.

.DESCRIPTION
    Two services, one supervisor:

      listener  python main.py --listen           -> state/listener.log
      hunter    python main.py --daemon --interval N -> state/hunter.log

    Both are foreground processes that die with whatever started them -- a
    terminal you close, a dropped session. That matters because BOTH fail
    silently: the review card still arrives and your "done" is ignored, or the
    hunt simply stops and no new jobs appear. Neither announces itself.

    So this wrapper adds what the bare commands cannot:

      * Restart on crash, with a backoff capped at five minutes.
      * Detach from the console, so closing the window leaves it running.
      * One instance per service -- two listeners on one Telegram session
        fight over the update stream and both act on the same command.
      * A UTF-8 log. The bot's output is routinely ARABIC, and PowerShell
        would otherwise write UTF-16 and decode the child's stdout as the OEM
        codepage, destroying it exactly when the log is being read to find out
        why an approval did not happen.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_service.ps1 -Service listener -Detached
    powershell -ExecutionPolicy Bypass -File scripts\run_service.ps1 -Service hunter -Detached
#>

[CmdletBinding()]
param(
    [ValidateSet('listener', 'hunter')]
    [string]$Service = 'listener',
    # Relaunch detached from this console, then return immediately.
    [switch]$Detached,
    # Minutes between hunts. Only meaningful for -Service hunter.
    [int]$IntervalMinutes = 60,
    # Give up after this many consecutive failures. 0 = never give up.
    [int]$MaxRestarts = 0
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'state'
$log = Join-Path $logDir "$Service.log"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# Decode the child process's stdout as UTF-8. Python forces UTF-8 on its
# streams (config.py), but PowerShell decodes native output using
# [Console]::OutputEncoding, which is the OEM codepage here -- so an em dash
# arrives as "ΓÇö" and Arabic arrives as nothing recoverable.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = [Console]::OutputEncoding

# The argument list that identifies this service, both to start it and to
# recognise an already-running copy.
$serviceArgs = if ($Service -eq 'hunter') {
    @('main.py', '--daemon', '--interval', "$IntervalMinutes")
} else {
    @('main.py', '--listen')
}
$matchPattern = if ($Service -eq 'hunter') { '*main.py*--daemon*' } else { '*main.py*--listen*' }

function Write-Log([string]$message) {
    $line = "{0}  [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Service, $message
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# A log left over from an older run may be UTF-16. Appending UTF-8 to it makes
# a file no tool can read end to end, so retire it once instead.
function Reset-LogIfNotUtf8 {
    if (-not (Test-Path $log)) { return }
    $bytes = Get-Content -Path $log -Encoding Byte -TotalCount 4 -ErrorAction SilentlyContinue
    if ($null -eq $bytes -or $bytes.Length -lt 2) { return }
    $isUtf16 = ($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) -or
               ($bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) -or
               ($bytes[1] -eq 0x00)
    if ($isUtf16) {
        Move-Item -Path $log -Destination "$log.utf16.bak" -Force
        Write-Host "Retired a UTF-16 log to $log.utf16.bak"
    }
}

function Get-RunningService {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like $matchPattern }
}

if ($Detached) {
    $existing = Get-RunningService
    if ($existing) {
        Write-Host "$Service already running (PID $($existing.ProcessId -join ', ')). Not starting a second."
        return
    }
    # Start-Process, not a background job: a job dies with this console, which
    # is the exact failure this script exists to prevent.
    $arguments = @(
        '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', "`"$PSCommandPath`"", '-Service', $Service,
        '-IntervalMinutes', $IntervalMinutes, '-MaxRestarts', $MaxRestarts
    )
    $process = Start-Process -FilePath 'powershell' -ArgumentList $arguments `
        -WindowStyle Hidden -PassThru
    Write-Host "$Service supervised detached as PID $($process.Id).  Log: $log"
    return
}

$existing = Get-RunningService
if ($existing) {
    Write-Log "Already running (PID $($existing.ProcessId -join ', ')). Nothing to do."
    return
}

Set-Location $root
Reset-LogIfNotUtf8
Write-Log "Supervisor starting: python $($serviceArgs -join ' ')"

$failures = 0
$backoff = 5

while ($true) {
    Write-Log 'Starting the service process.'
    try {
        # NOT Tee-Object: it has no -Encoding before PowerShell 6 and writes
        # UTF-16. Explicit per-line append keeps the whole file UTF-8.
        & python @serviceArgs 2>&1 | ForEach-Object {
            Write-Host $_
            Add-Content -Path $log -Value ([string]$_) -Encoding utf8
        }
        $code = $LASTEXITCODE
    } catch {
        $code = 1
        Write-Log "Service threw: $($_.Exception.Message)"
    }

    if ($code -eq 0) {
        # A clean exit is Ctrl-C or a deliberate shutdown. Respect it: a
        # supervisor that restarts through a deliberate stop cannot be stopped.
        Write-Log 'Exited cleanly. Not restarting.'
        break
    }

    $failures++
    if ($MaxRestarts -gt 0 -and $failures -ge $MaxRestarts) {
        Write-Log "Gave up after $failures consecutive failures."
        break
    }

    Write-Log "Exited with code $code. Restarting in ${backoff}s (failure $failures)."
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min($backoff * 2, 300)
}
