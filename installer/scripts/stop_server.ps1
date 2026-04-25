# ════════════════════════════════════════════════════════════════
#  stop_server.ps1 — invoked by the Inno Setup updater before file copy.
#
#  Purpose
#  -------
#  When the user runs a newer GhostShellAntySetup.exe over an existing
#  install, the dashboard server is almost certainly still running in
#  the background (it auto-starts on first install and the user just
#  has the browser tab open). If we let Inno overwrite files while the
#  Python process holds them open, Windows returns ERROR_SHARING_VIOLATION
#  and the install fails halfway through.
#
#  This script tries to stop the dashboard gracefully, escalating in
#  three tiers:
#
#    1. POST /api/admin/shutdown  — clean exit with X-Shutdown-Token
#    2. Stop-Process by PID       — soft signal
#    3. Stop-Process -Force       — hard kill
#
#  It also stops the scheduler (separate process) and any Chromium
#  instances launched out of the install dir (so Chrome doesn't hold
#  chrome.exe open during the update). The orphan scan catches stray
#  pythons from any prior install — including legacy installs at
#  C:\ProgramData\GhostShell\ that pre-date the current
#  {localappdata}\GhostShellAnty\ layout.
#
#  Exit code is always 0 — failure to stop a process should NOT abort
#  the installer (Inno will report the actual file lock as a clearer
#  error if this script's best-effort fails).
# ════════════════════════════════════════════════════════════════

[CmdletBinding()]
param(
    # Optional: path of the install dir whose chromiums we want to kill.
    # Inno passes this via -InstallDir on real updates; left empty on a
    # fresh install (in which case there's nothing to do).
    [string]$InstallDir = "",

    # Total wallclock budget for the whole stop sequence, in seconds.
    [int]$TimeoutSec = 12
)

$ErrorActionPreference = "Continue"

# Persist a copy of every log line to %TEMP%\ghost_shell_stop_server.log
# regardless of how the script was invoked. Inno's Exec() does NOT capture
# stdout, so Write-Host alone vanishes — without this file the user has
# no way to see what the killer did or didn't do.
$LogFile = Join-Path $env:TEMP "ghost_shell_stop_server.log"
$LogStart = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
try {
    Add-Content -Path $LogFile -Value "" -Encoding UTF8
    Add-Content -Path $LogFile -Value "===== stop_server.ps1 invoked $LogStart =====" -Encoding UTF8
    Add-Content -Path $LogFile -Value "InstallDir=$InstallDir TimeoutSec=$TimeoutSec" -Encoding UTF8
} catch {}

function Write-Log($msg) {
    # Stamp + flush — Inno captures stdout via Exec(); these lines also
    # land in installer's verbose log when the user runs Setup.exe /LOG=...
    # Mirror copy to %TEMP% so the user can post-mortem even when the
    # installer was started without /LOG.
    $stamped = "[stop_server] $msg"
    Write-Host $stamped
    try { Add-Content -Path $LogFile -Value $stamped -Encoding UTF8 } catch {}
}

# ── Paths ────────────────────────────────────────────────────
$RuntimeDir   = Join-Path $env:LOCALAPPDATA "GhostShellAnty"
$RuntimeJson  = Join-Path $RuntimeDir "runtime.json"
$SchedPidFile = Join-Path $RuntimeDir "scheduler.pid"

$Deadline = (Get-Date).AddSeconds($TimeoutSec)

# ── Tier 1: graceful HTTP shutdown ───────────────────────────
$dashboardPid = 0
$dashboardOk  = $false

if (Test-Path $RuntimeJson) {
    try {
        $rt = Get-Content $RuntimeJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $dashboardPid = [int]$rt.pid
        $port  = [int]$rt.port
        $token = [string]$rt.shutdown_token

        if ($port -gt 0 -and $token) {
            Write-Log "graceful shutdown POST :$port (pid=$dashboardPid)"
            try {
                Invoke-WebRequest `
                    -Uri "http://127.0.0.1:$port/api/admin/shutdown" `
                    -Method POST `
                    -Headers @{ "X-Shutdown-Token" = $token } `
                    -Body '{"grace": 0.3}' `
                    -ContentType "application/json" `
                    -UseBasicParsing `
                    -TimeoutSec 3 | Out-Null
                $dashboardOk = $true
                Write-Log "graceful shutdown accepted"
            } catch {
                Write-Log "graceful shutdown failed: $($_.Exception.Message)"
            }
        }
    } catch {
        Write-Log "could not read runtime.json: $($_.Exception.Message)"
    }
} else {
    Write-Log "no runtime.json - dashboard not running via current install"
}

# ── Wait for the PID to actually exit (up to 5s) ─────────────
function Test-PidAlive([int]$processId) {
    if ($processId -le 0) { return $false }
    try {
        $null = Get-Process -Id $processId -ErrorAction Stop
        return $true
    } catch { return $false }
}

if ($dashboardOk -and $dashboardPid -gt 0) {
    $waitUntil = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $waitUntil -and (Test-PidAlive $dashboardPid)) {
        Start-Sleep -Milliseconds 200
    }
    if (-not (Test-PidAlive $dashboardPid)) {
        Write-Log "dashboard exited cleanly"
    }
}

# ── Tier 2 + 3: taskkill the PID if still alive ──────────────
if ($dashboardPid -gt 0 -and (Test-PidAlive $dashboardPid)) {
    Write-Log "PID $dashboardPid still alive - Stop-Process (soft)"
    try { Stop-Process -Id $dashboardPid -ErrorAction Stop } catch {
        Write-Log "soft stop failed: $($_.Exception.Message)"
    }
    Start-Sleep -Milliseconds 800

    if (Test-PidAlive $dashboardPid) {
        Write-Log "PID $dashboardPid still alive - Stop-Process -Force"
        try { Stop-Process -Id $dashboardPid -Force -ErrorAction Stop } catch {
            Write-Log "force stop failed: $($_.Exception.Message)"
        }
    }
}

# Whether we got it or not, drop the runtime file so future probes don't
# hit the stale entry.
if (Test-Path $RuntimeJson) {
    try { Remove-Item $RuntimeJson -ErrorAction Stop } catch {
        Write-Log "could not remove runtime.json (may need elevation)"
    }
}

# ── Scheduler ────────────────────────────────────────────────
if (Test-Path $SchedPidFile) {
    try {
        $schedPid = [int](Get-Content $SchedPidFile -Raw).Trim()
        if ($schedPid -gt 0 -and (Test-PidAlive $schedPid)) {
            Write-Log "stopping scheduler PID $schedPid"
            try { Stop-Process -Id $schedPid -ErrorAction Stop } catch {}
            Start-Sleep -Milliseconds 600
            if (Test-PidAlive $schedPid) {
                try { Stop-Process -Id $schedPid -Force -ErrorAction Stop } catch {}
            }
        }
    } catch {
        Write-Log "scheduler PID parse failed: $($_.Exception.Message)"
    }
    try { Remove-Item $SchedPidFile -ErrorAction Stop } catch {}
}

# ── Orphan python.exe / pythonw.exe scan via CIM ─────────────
# Belt-and-suspenders — if the previous dashboard or scheduler crashed
# without writing runtime.json / scheduler.pid (or the user manually
# killed the dashboard but left the scheduler running, then tried to
# reinstall), we'll never hit those PIDs by file. Also catches LEGACY
# installs at C:\ProgramData\GhostShell\... (pre-current-naming).
#
# Why CIM and not Get-Process:
#   Get-Process exposes .Path via MainModule.FileName which silently
#   returns $null for any process the current token can't open with
#   PROCESS_QUERY_LIMITED_INFORMATION (job-controlled processes,
#   processes with HighIL token, etc). The try/catch around `$p.Path`
#   is useless because the access denial isn't a TERMINATING error —
#   it just yields $null and we skip via `if (-not $pth) continue`.
#   That was the actual reason orphans piled up across reinstalls.
#   Win32_Process.ExecutablePath / .CommandLine come from the kernel
#   side and are populated for any process the user owns. Much more
#   reliable.
$matchPattern = '(?i)ghost_shell|GhostShell'
$killedAny = $false

try {
    $procs = Get-CimInstance -ClassName Win32_Process `
        -Filter "Name='python.exe' OR Name='pythonw.exe'" `
        -ErrorAction Stop
    foreach ($proc in $procs) {
        $exePath = $proc.ExecutablePath
        $cmdLine = $proc.CommandLine
        $matched = $false
        if ($exePath -and ($exePath -match $matchPattern)) { $matched = $true }
        if (-not $matched -and $cmdLine -and ($cmdLine -match $matchPattern)) { $matched = $true }
        if (-not $matched) { continue }

        $procId = [int]$proc.ProcessId
        $tag = if ($exePath) { $exePath } else { $cmdLine }
        Write-Log "killing $($proc.Name) PID $procId at $tag"
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            $killedAny = $true
        } catch {
            Write-Log "  Stop-Process failed for PID ${procId}: $($_.Exception.Message)"
            # Last-ditch: taskkill /F via cmd. Sometimes works when
            # Stop-Process is blocked by handle-lifetime weirdness.
            try {
                & cmd.exe /c "taskkill /F /PID $procId" 2>&1 | Out-Null
                $killedAny = $true
            } catch {}
        }
    }
} catch {
    Write-Log "CIM Win32_Process query failed: $($_.Exception.Message)"
    Write-Log "  falling back to Get-Process scan (may miss locked-down procs)"
    try {
        $pyProcs = Get-Process -Name python, pythonw -ErrorAction SilentlyContinue
        foreach ($p in $pyProcs) {
            $pth = $null
            try { $pth = $p.Path } catch {}
            if ($pth -and ($pth -match $matchPattern)) {
                Write-Log "fallback kill $($p.Name) PID $($p.Id) at $pth"
                try {
                    Stop-Process -Id $p.Id -Force -ErrorAction Stop
                    $killedAny = $true
                } catch {}
            }
        }
    } catch {}
}

if ($killedAny) { Start-Sleep -Milliseconds 600 }

# ── Chromium / chromedriver still bound to the install dir ───
# These can be active monitor runs holding chrome.exe open. Killing them
# is fine: monitor runs are designed to be restart-safe (each search
# query is its own logical unit; partial runs leave a clean DB row with
# exit_code != 0).
if ($InstallDir -and (Test-Path $InstallDir)) {
    $chromeExe = Join-Path $InstallDir "chrome_win64\chrome.exe"
    $driverExe = Join-Path $InstallDir "chrome_win64\chromedriver.exe"
    foreach ($exe in @($chromeExe, $driverExe)) {
        if (-not (Test-Path $exe)) { continue }
        try {
            # Match by full path so we don't kill the user's regular Chrome.
            $procs = Get-Process | Where-Object {
                try { $_.Path -ieq $exe } catch { $false }
            }
            foreach ($p in $procs) {
                Write-Log "killing $($p.Name) PID $($p.Id) ($exe)"
                try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch {}
            }
        } catch {
            Write-Log "Get-Process scan failed for ${exe}: $($_.Exception.Message)"
        }
    }
}

# ── Final wait so file handles release ───────────────────────
# Windows file handles linger ~200ms after process exit. Give the OS a
# moment to release locks before Inno tries to overwrite the binaries.
Start-Sleep -Milliseconds 600
Write-Log "done"
exit 0
