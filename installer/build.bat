@echo off
REM ============================================================
REM  Build the Ghost Shell installer .exe
REM
REM  Prerequisites:
REM    - Inno Setup 6+ from https://jrsoftware.org/isinfo.php
REM    - A Python 3.13.x installer at deps\python-3.13.x-amd64.exe
REM      (download from https://www.python.org/downloads/windows/)
REM
REM  Output: installer\output\GhostShellAntySetup.exe
REM ============================================================

setlocal
cd /d "%~dp0"

REM ─── Find ISCC.exe — try Inno 5/6/7 standard paths, then PATH ──
set "ISCC="
for %%P in (
    "C:\Program Files\Inno Setup 7\ISCC.exe"
    "C:\Program Files (x86)\Inno Setup 7\ISCC.exe"
    "C:\Program Files\Inno Setup 6\ISCC.exe"
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    "C:\Program Files\Inno Setup 5\ISCC.exe"
    "C:\Program Files (x86)\Inno Setup 5\ISCC.exe"
) do (
    if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
    for /f "delims=" %%P in ('where ISCC.exe 2^>nul') do (
        if not defined ISCC set "ISCC=%%P"
    )
)
if not defined ISCC (
    echo [x] Inno Setup not found.
    echo     Install from https://jrsoftware.org/isinfo.php and re-run.
    pause
    exit /b 1
)
echo [*] Using Inno: %ISCC%

REM ─── Find a Python installer in deps\ — prefer 3.13 > 3.12 > 3.11 ──
set "PYBOX="
for %%V in (3.13 3.12 3.11) do (
    if not defined PYBOX (
        for /f "delims=" %%F in ('dir /b /o-n "deps\python-%%V*-amd64.exe" 2^>nul') do (
            if not defined PYBOX set "PYBOX=%%~nxF"
        )
    )
)
if not defined PYBOX (
    for /f "delims=" %%F in ('dir /b /o-n "deps\python-3.*-amd64.exe" 2^>nul') do (
        if not defined PYBOX set "PYBOX=%%~nxF"
    )
)
if not defined PYBOX (
    echo [x] No Python installer found in deps\
    echo     Drop python-3.13.x-amd64.exe ^(or 3.12 / 3.11^) into installer\deps\
    pause
    exit /b 1
)
echo [*] Bundling: %PYBOX%

REM ─── Parse args: --no-sync, --sync-from <DIR> ─────────────────
set "SKIP_SYNC=0"
set "SYNC_SRC="
:parse_args
if "%~1"=="--no-sync"   ( set "SKIP_SYNC=1" & shift & goto parse_args )
if "%~1"=="--sync-from" ( set "SYNC_SRC=%~2" & shift & shift & goto parse_args )
if not "%~1"=="" shift & goto parse_args

REM ─── Sync Chromium artefacts from the dev build dir ──────────
REM cmd.exe is picky: `)` of the `if` and `else` MUST be on the same
REM line (or use a goto). Don't reformat or this stops working.
if "%SKIP_SYNC%"=="0" (
    call sync_chromium.bat %SYNC_SRC%
    if errorlevel 1 (
        echo [x] sync_chromium.bat failed - re-run with --no-sync to skip
        echo     if you know your chrome_win64\ is already current.
        pause
        exit /b 1
    )
) else (
    echo [*] Skipping chromium sync ^(--no-sync^)
)

REM ─── Build wizard background BMPs from repository-template.png ──
REM Inno's modern wizard wants .bmp (PNG is silently ignored). The
REM PowerShell helper does PNG -> BMP with proper aspect-correct
REM center-cropping. Skips silently if the source PNG is missing,
REM so the build still works without the asset.
echo [*] Building wizard background images ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build_wizard_images.ps1
if errorlevel 1 (
    echo [warn] wizard image generation failed - continuing anyway
)

REM ─── Build number — auto-bumps the last X of X.Y.Z.W on every build.
REM   Major/Minor/Patch are manual (edit AppVersionMajor/Minor/Patch in
REM   ghost_shell_installer.iss). Only the build counter auto-increments,
REM   wrapping 0..1000 (modulo 1001) so the number stays bounded.
REM
REM   Using a PS1 helper instead of `set /a` because cmd silently fails
REM   on non-numeric file content. We had a case where .build_number
REM   ended up containing "ECHO is off." (a cmd error msg), and the
REM   counter got permanently stuck at 1. PowerShell strips junk and
REM   falls back to 0 on parse failure.
set "BUILD="
for /f "usebackq delims=" %%N in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\bump_build_number.ps1`) do set "BUILD=%%N"
if not defined BUILD (
    echo [x] bump_build_number.ps1 returned nothing - using fallback 0
    set "BUILD=0"
)
echo [*] Build number: %BUILD%

REM ---- Compile. ISCC syntax for #define overrides is /DName=Value ----
echo [*] Command: "%ISCC%" /Q "/DPyInstaller=deps\%PYBOX%" "/DBuildNumber=%BUILD%" "ghost_shell_installer.iss"
"%ISCC%" /Q "/DPyInstaller=deps\%PYBOX%" "/DBuildNumber=%BUILD%" "ghost_shell_installer.iss"
if errorlevel 1 (
    echo [x] Compile failed.
    pause
    exit /b 1
)

echo.
echo === Installer built ===
echo  output\GhostShellAntySetup.exe
echo.
explorer output
pause
exit /b 0
