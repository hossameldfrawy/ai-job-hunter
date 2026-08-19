<#
.SYNOPSIS
    Runs the HITL review listener, and keeps it running.

.DESCRIPTION
    `python main.py --listen` holds an open Telegram connection and waits for
    you to reply "done 7" or "تعديل 7 الراتب: 15000". It is a foreground
    process: it dies with whatever started it -- a terminal you close, an SSH
    session that drops, an agent session that ends.

    That matters more than it sounds. The listener is what turns a review card
    on your phone into a submitted application, and a listener that is not
    running fails SILENTLY: the card still arrives, you still reply, and
    nothing happens. There is no error to notice.

    So this wrapper does two things the bare command cannot:

      * Restarts on crash, with a backoff, so a dropped connection or a
        Telegram flood-wait does not end the day's listening.
      * Detaches from the console when run with -Detached, so closing the
        window leaves it running.

    The poll backstop inside the listener covers a QUIET failure (the event
    stream silently stopping). This covers a LOUD one (the process dying).
    Between them, a reply you send gets acted on.

.EXAMPLE
    # Run it here, watch the log, Ctrl-C to stop:
    powershell -ExecutionPolicy Bypass -File scripts\run_listener.ps1

.EXAMPLE
    # Run it detached; closing this window leaves it running:
    powershell -ExecutionPolicy Bypass -File scripts\run_listener.ps1 -Detached

.EXAMPLE
    # Start it automatically at every logon (survives a reboot).
    # Register once:
    schtasks /Create /TN "AI Job Hunter Listener" /SC ONLOGON /RL LIMITED `
      /TR "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File `
           'C:\Users\hossa\OneDrive\Desktop\NEW SHAPTER\AI_Job_Hunter_Bot\scripts\run_listener.ps1'"
    # Then: schtasks /Run /TN "AI Job Hunter Listener"
    #       schtasks /End /TN "AI Job Hunter Listener"
    #       schtasks /Delete /TN "AI Job Hunter Listener" /F
#>

[CmdletBinding()]
param(
    # Relaunch detached from this console, then return immediately.
    [switch]$Detached,
    # Give up after this many consecutive failures. 0 = never give up.
    [int]$MaxRestarts = 0
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root 'state'
$log = Join-Path $logDir 'listener.log'

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# Decode the child process's stdout as UTF-8.
#
# Python forces UTF-8 on its streams (see config.py), but PowerShell decodes a
# native command's output using [Console]::OutputEncoding, which defaults to
# the OEM codepage -- cp437 here. So an em dash arrives as "ΓÇö" and ARABIC
# arrives as nothing recoverable at all. The log carries the review cards and
# the user's own replies, both of which are routinely Arabic, so getting this
# wrong turns the log into noise precisely when it is being read to find out
# why an approval did not happen.
$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = [Console]::OutputEncoding

# Every write to the log goes through here, with the encoding stated. Windows
# PowerShell 5.1 defaults the redirection operators and Tee-Object to UTF-16,
# and Add-Content to the ANSI codepage -- so a log written by a mix of them is
# a mix of encodings, and this one carries ARABIC (the review cards and the
# user's own replies). Half of it renders as mojibake and the rest as spaced
# nulls, which makes the log useless exactly when it is being read to find out
# why an approval did not happen.
function Write-Log([string]$message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# A log left over from an older run may be UTF-16. Appending UTF-8 to it
# produces a file no tool can read end to end, so retire it once instead.
function Reset-LogIfNotUtf8 {
    if (-not (Test-Path $log)) { return }
    $bytes = Get-Content -Path $log -Encoding Byte -TotalCount 4 -ErrorAction SilentlyContinue
    $isUtf16 = $bytes.Length -ge 2 -and (
        ($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) -or
        ($bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) -or
        ($bytes.Length -ge 2 -and $bytes[1] -eq 0x00)
    )
    if ($isUtf16) {
        $retired = "$log.utf16.bak"
        Move-Item -Path $log -Destination $retired -Force
        Write-Host "Retired a UTF-16 log to $retired"
    }
}

if ($Detached) {
    # Start-Process with a hidden window is what actually survives this console
    # closing. -Detached is not passed through, or it would recurse forever.
    $arguments = @(
        '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', "`"$PSCommandPath`"", '-MaxRestarts', $MaxRestarts
    )
    $process = Start-Process -FilePath 'powershell' -ArgumentList $arguments `
        -WindowStyle Hidden -PassThru
    Write-Log "Listener detached as PID $($process.Id). Log: $log"
    Write-Host ""
    Write-Host "  Stop it with:  Stop-Process -Id $($process.Id)"
    Write-Host "  Watch it with: Get-Content '$log' -Wait -Tail 20"
    return
}

# One instance only. Two listeners on the same Telegram session fight over the
# update stream, and both would act on the same command.
$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*main.py*--listen*' -and $_.ProcessId -ne $PID }
if ($existing) {
    Write-Log "A listener is already running (PID $($existing.ProcessId -join ', ')). Nothing to do."
    return
}

Set-Location $root
Reset-LogIfNotUtf8
Write-Log "Supervisor starting. Working directory: $root"

$failures = 0
$backoff = 5

while ($true) {
    Write-Log 'Starting: python main.py --listen'
    try {
        # NOT Tee-Object: it has no -Encoding before PowerShell 6 and writes
        # UTF-16 here. Explicit per-line append keeps the whole file UTF-8.
        & python main.py --listen 2>&1 | ForEach-Object {
            Write-Host $_
            Add-Content -Path $log -Value ([string]$_) -Encoding utf8
        }
        $code = $LASTEXITCODE
    } catch {
        $code = 1
        Write-Log "Listener threw: $($_.Exception.Message)"
    }

    if ($code -eq 0) {
        # A clean exit is Ctrl-C or a deliberate shutdown. Respect it -- an
        # supervisor that restarts through a deliberate stop cannot be stopped.
        Write-Log 'Listener exited cleanly. Not restarting.'
        break
    }

    $failures++
    if ($MaxRestarts -gt 0 -and $failures -ge $MaxRestarts) {
        Write-Log "Gave up after $failures consecutive failures."
        break
    }

    Write-Log "Listener exited with code $code. Restarting in ${backoff}s (failure $failures)."
    Start-Sleep -Seconds $backoff
    # Back off to 5 minutes, so a persistent outage is not a hot loop.
    $backoff = [Math]::Min($backoff * 2, 300)
}
