@echo off
REM ════════════════════════════════════════════════════════════════
REM brand_chrome.bat — stamp ghost_shell.ico onto an installed
REM chrome.exe / chromedriver.exe.
REM
REM Use this when:
REM   • The installed Chrome shows the default blue Chromium globe
REM     instead of the Ghost Shell icon
REM   • You rebuilt chrome.exe and need to re-apply the icon
REM   • You want to test icon branding without rebuilding the full
REM     installer
REM
REM Requires: deps\rcedit.exe (drop the official build from
REM   https://github.com/electron/rcedit/releases into installer\deps\)
REM
REM Optional argument: path to the install dir. If omitted, defaults
REM to %LOCALAPPDATA%\GhostShellAnty (the Inno default).
REM ════════════════════════════════════════════════════════════════

setlocal

set "INSTALL_DIR=%~1"
if "%INSTALL_DIR%"=="" set "INSTALL_DIR=%LOCALAPPDATA%\GhostShellAnty"

set "RCEDIT=%~dp0deps\rcedit.exe"
set "ICON=%INSTALL_DIR%\ghost_shell.ico"
set "CHROME=%INSTALL_DIR%\chrome_win64\chrome.exe"
set "DRIVER=%INSTALL_DIR%\chrome_win64\chromedriver.exe"

echo.
echo Ghost Shell Anty — chrome.exe icon branding
echo --------------------------------------------
echo Install dir : %INSTALL_DIR%
echo rcedit      : %RCEDIT%
echo Icon        : %ICON%
echo.

if not exist "%RCEDIT%" (
    echo [ERROR] rcedit.exe not found at %RCEDIT%
    echo Download from https://github.com/electron/rcedit/releases
    echo and drop it into installer\deps\rcedit.exe, then re-run.
    exit /b 1
)

if not exist "%ICON%" (
    echo [ERROR] icon not found at %ICON%
    echo The install must be complete and ghost_shell.ico must exist
    echo at the install root.
    exit /b 1
)

if not exist "%CHROME%" (
    echo [ERROR] chrome.exe not found at %CHROME%
    echo Pass the install dir as the first argument, e.g.
    echo   brand_chrome.bat "C:\ProgramData\GhostShell"
    exit /b 1
)

echo [1/2] Stamping chrome.exe ...
"%RCEDIT%" "%CHROME%" --set-icon "%ICON%"
if errorlevel 1 (
    echo [WARN] rcedit returned %errorlevel% on chrome.exe
)

if exist "%DRIVER%" (
    echo [2/2] Stamping chromedriver.exe ...
    "%RCEDIT%" "%DRIVER%" --set-icon "%ICON%"
    if errorlevel 1 (
        echo [WARN] rcedit returned %errorlevel% on chromedriver.exe
    )
) else (
    echo [2/2] chromedriver.exe not found, skipping
)

echo.
echo Done. You may need to clear Windows' icon cache for the change
echo to be visible immediately:
echo     ie4uinit.exe -ClearIconCache
echo or just log out and back in.
echo.

endlocal
exit /b 0
