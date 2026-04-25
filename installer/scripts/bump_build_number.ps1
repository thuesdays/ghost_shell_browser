# ════════════════════════════════════════════════════════════════
#  bump_build_number.ps1 — read, increment, persist the last-X
#  build counter for the installer's X.Y.Z.W version.
#
#  Why a PS helper instead of cmd /a:
#    cmd's `set /a` silently fails on non-numeric content. We had a
#    nasty case where `.build_number` got corrupted with the literal
#    string "ECHO is off." — set /a couldn't parse it, BUILD stayed
#    empty, the next `echo %BUILD%` re-wrote the same junk, and the
#    counter was permanently stuck at 1. PowerShell gives us proper
#    integer parsing, fallback-to-zero on junk, and modular wrapping.
#
#  Bounds: 0..1000 inclusive (modulo 1001). Once the counter hits 1000
#  the next bump rolls it back to 0. Keeps the last X of X.Y.Z.W
#  bounded — Major/Minor/Patch are bumped manually in the .iss.
#
#  Output: prints the new value to stdout. build.bat captures it via
#  `for /f` and passes it to ISCC as /DBuildNumber=N.
#
#  Usage:
#    powershell -NoProfile -ExecutionPolicy Bypass -File bump_build_number.ps1
#    powershell ... -File bump_build_number.ps1 -Path "F:\path\.build_number"
# ════════════════════════════════════════════════════════════════

[CmdletBinding()]
param(
    [string]$Path = ""
)

$ErrorActionPreference = "Stop"

if (-not $Path) {
    # Default: ..\.build_number relative to this script (installer\.build_number)
    $here = Split-Path -Parent $MyInvocation.MyCommand.Definition
    $Path = Join-Path (Split-Path -Parent $here) ".build_number"
}

# Read current value, strip any non-digit junk, fall back to 0.
$current = 0
if (Test-Path $Path) {
    $raw = (Get-Content -Raw -Path $Path -ErrorAction SilentlyContinue)
    if ($raw) {
        $digits = ($raw -replace '[^0-9]', '').Trim()
        if ($digits) {
            $parsed = $digits -as [int]
            if ($null -ne $parsed) { $current = $parsed }
        }
    }
}

# Increment, wrap at 1000 (modulo 1001 → 0..1000 inclusive).
$next = ($current + 1) % 1001

# Write back as a single ASCII integer with no trailing newline so we
# don't accumulate whitespace across hundreds of builds.
Set-Content -Path $Path -Value $next -NoNewline -Encoding ASCII

# Stdout: just the integer, for cmd's `for /f` capture.
Write-Output $next
