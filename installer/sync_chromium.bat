@echo off
REM ════════════════════════════════════════════════════════════════
REM sync_chromium.bat — refresh chrome_win64\ from a Chromium build dir.
REM
REM Why this exists
REM ---------------
REM The installer ships F:\projects\ghost_shell_browser\chrome_win64\
REM verbatim. If chrome_win64\ is older than the dev build at
REM F:\projects\chromium\src\out\GhostShell\, the installer will
REM bundle stale .exe + .pak files. Symptom: the running browser
REM shows the default Chromium globe icon instead of the custom
REM Ghost Shell icon (the icon lives in resources.pak, not chrome.exe).
REM
REM File list strategy
REM ------------------
REM We mirror the exact whitelist from scripts\deploy-ghost-shell-flat.bat
REM (the upstream script that knows which files Chromium actually
REM needs at runtime). Anything not on the whitelist is build-time
REM noise we DO NOT want in the redistributable:
REM     v8_context_snapshot_generator.exe   ~234 MB
REM     mksnapshot.exe                      ~87 MB
REM     protoc.exe / nasm.exe / llvm-tblgen ~10 MB each
REM     siso_metrics.*.json siso_trace.*.json   ~lots of build telemetry
REM     ucrtbased.dll                       ~debug runtime
REM Skipping these saves ~400 MB in the installer .exe.
REM
REM Usage
REM -----
REM   sync_chromium.bat                     — sync from default location
REM   sync_chromium.bat <src_dir>           — sync from a custom build dir
REM
REM Called automatically by build.bat unless you pass --no-sync.
REM ════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

set "SRC=%~1"
if "%SRC%"=="" set "SRC=F:\projects\chromium\src\out\GhostShell"

set "DST=%~dp0..\chrome_win64"

if not exist "%SRC%" (
    echo [sync_chromium] ERROR: source dir not found: %SRC%
    echo                Pass the dev build dir as the first argument, e.g.
    echo                   sync_chromium.bat "F:\path\to\out\Release"
    exit /b 1
)

if not exist "%SRC%\chrome.exe" (
    echo [sync_chromium] ERROR: %SRC%\chrome.exe not found
    echo                That's not a Chromium build dir.
    exit /b 1
)

echo.
echo [sync_chromium] Syncing minimal Chromium runtime
echo                 from: %SRC%
echo                 to  : %DST%
echo.

REM ── Read Chromium VERSION so we know what SxS manifest filename to copy
REM    (e.g. 147.0.7780.88.manifest). Without the manifest chrome.exe
REM    fails the side-by-side activation check at startup.
set "VERSION_FILE=%SRC%\..\..\chrome\VERSION"
set "MAJOR="
set "MINOR="
set "BUILD="
set "PATCH="
if exist "%VERSION_FILE%" (
    for /f "usebackq tokens=1,2 delims==" %%A in ("%VERSION_FILE%") do (
        if /i "%%A"=="MAJOR" set "MAJOR=%%B"
        if /i "%%A"=="MINOR" set "MINOR=%%B"
        if /i "%%A"=="BUILD" set "BUILD=%%B"
        if /i "%%A"=="PATCH" set "PATCH=%%B"
    )
    set "VERSION=!MAJOR!.!MINOR!.!BUILD!.!PATCH!"
) else (
    set "VERSION="
)
if defined VERSION (echo                 ver : !VERSION!)

REM ── Destination prep — wipe and recreate so stale files from older
REM    Chromium builds don't pile up. SAFE because we already validated
REM    SRC has chrome.exe above (guards against the "wiped chrome_win64
REM    when source was empty" incident we hit before).
echo.
echo [1/4] Wiping and recreating target ...
if exist "%DST%" (
    rmdir /S /Q "%DST%"
    if exist "%DST%" (
        echo [sync_chromium] ERROR: could not remove %DST% — files may be in use.
        echo                Close any running chrome.exe / chromedriver.exe and retry.
        exit /b 1
    )
)
mkdir "%DST%"
mkdir "%DST%\locales"
echo.

REM ── Required runtime files (chrome won't start without these). ─────
REM Mirrors deploy-ghost-shell-flat.bat exactly.
echo [2/4] Copying required runtime files ...
set "MISSING="
for %%F in (
    chrome.exe
    chrome.dll
    chrome_elf.dll
    d3dcompiler_47.dll
    libEGL.dll
    libGLESv2.dll
    vk_swiftshader.dll
    resources.pak
    chrome_100_percent.pak
    chrome_200_percent.pak
    v8_context_snapshot.bin
    icudtl.dat
    vk_swiftshader_icd.json
) do (
    if exist "%SRC%\%%F" (
        copy /Y "%SRC%\%%F" "%DST%\%%F" >nul
        echo   ok  %%F
    ) else (
        echo   ERR %%F  ^(missing in build output^)
        set "MISSING=1"
    )
)
echo.

REM ── SxS manifest — critical. Filename is <chromium-version>.manifest.
REM Without it Windows refuses to activate the app's manifest and
REM chrome.exe fails to start with a cryptic "side-by-side configuration
REM is incorrect" error.
echo [3/4] Copying SxS manifest ...
if defined VERSION (
    if exist "%SRC%\!VERSION!.manifest" (
        copy /Y "%SRC%\!VERSION!.manifest" "%DST%\!VERSION!.manifest" >nul
        echo   ok  !VERSION!.manifest  ^(critical^)
    ) else (
        echo   WARN !VERSION!.manifest missing - generating fallback
        (
            echo ^<?xml version='1.0' encoding='UTF-8' standalone='yes'?^>
            echo ^<assembly xmlns='urn:schemas-microsoft-com:asm.v1' manifestVersion='1.0'^>
            echo   ^<assemblyIdentity type='win32' name='!VERSION!' version='!VERSION!' processorArchitecture='amd64'/^>
            echo ^</assembly^>
        ) > "%DST%\!VERSION!.manifest"
    )
) else (
    echo   skip ^(no chrome\VERSION found, can't compute manifest filename^)
)
echo.

REM ── Optional files — not fatal if missing but each adds realism.
REM crashpad fixes silent startup crashes, chromedriver enables Selenium,
REM the VC redistributables let chrome run on machines without MSVC runtime.
echo [4/4] Copying optional files ...
for %%F in (
    crashpad_handler.exe
    chromedriver.exe
    snapshot_blob.bin
    vulkan-1.dll
    msvcp140.dll
    vcruntime140.dll
    vcruntime140_1.dll
) do (
    if exist "%SRC%\%%F" (
        copy /Y "%SRC%\%%F" "%DST%\%%F" >nul
        echo   ok  %%F
    ) else (
        echo   skip %%F  ^(not in build^)
    )
)
echo.

REM ── locales\ — per-language string packs. Chrome needs at least
REM the one matching the host OS locale; we ship the full set so a
REM per-profile language override works even when it differs from
REM the OS language.
if exist "%SRC%\locales" (
    xcopy /E /I /Y /Q "%SRC%\locales" "%DST%\locales" >nul
    echo   ok  locales\
) else (
    echo   WARN locales\ missing in build dir
)
echo.

if not exist "%DST%\crashpad_handler.exe" (
    echo [sync_chromium] NOTICE: crashpad_handler.exe not in build output.
    echo                 chrome.exe may FAIL TO START silently on some boxes.
    echo                 To produce it: autoninja -C out\GhostShell crashpad_handler
)

if defined MISSING (
    echo.
    echo [sync_chromium] FAILED — required runtime files were missing.
    echo                 Re-build Chromium and try again.
    exit /b 1
)

REM Hash a few key artefacts so the build log shows which version we
REM shipped. Lets you eyeball "did I forget to rebuild Chromium?" by
REM comparing hashes across two installer outputs.
echo [sync_chromium] Hashing key artefacts:
for %%F in (chrome.exe chrome.dll resources.pak chrome_100_percent.pak) do (
    if exist "%DST%\%%F" (
        for /f "tokens=*" %%H in ('certutil -hashfile "%DST%\%%F" SHA256 ^| findstr /R "^[0-9a-f]"') do (
            echo                 %%F  %%H
        )
    )
)
echo.
echo [sync_chromium] Done.
exit /b 0
