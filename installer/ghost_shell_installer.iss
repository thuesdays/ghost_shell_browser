; ═══════════════════════════════════════════════════════════════
;  Ghost Shell — Inno Setup installer
;  See installer\README.md for build instructions.
;
;  This installer supports three flows:
;    • Fresh install        — no existing GhostShellAnty AppId in registry
;    • Update / Repair      — same AppId found; user picks via a custom
;                             page in InitializeWizard
;    • Reinstall (clean)    — same AppId found; user opted to wipe data
;
;  Before any file copy on a non-fresh flow, the installer stops the
;  running dashboard server (graceful HTTP shutdown → soft kill → /F)
;  and the scheduler. See installer\scripts\stop_server.ps1 for the
;  actual stop logic; this file only orchestrates.
; ═══════════════════════════════════════════════════════════════

#define AppName       "Ghost Shell Anty"
#define AppPublisher  "Ghost Shell Anty contributors"
#define AppCopyright  "Copyright (C) 2026 Ghost Shell Anty contributors -- MIT License"
#define AppURL        "https://github.com/thuesdays/ghost_shell_browser"
#define AppDescription "Self-hosted antidetect browser + dashboard for Google Ads competitive intelligence"
#define PythonMin     "3.11"

; Semantic version + build number. build.bat passes the current build
; via /DBuildNumber=N. The /D wins over this fallback (used only when
; iscc is run without /D, e.g. F9 from the IDE).
#define AppVersionMajor "0"
#define AppVersionMinor "2"
#define AppVersionPatch "0"
#ifndef BuildNumber
  #define BuildNumber "0"
#endif
#define AppVersion AppVersionMajor + "." + AppVersionMinor + "." + AppVersionPatch
#define AppFullVersion AppVersion + "." + BuildNumber

; Project root is the parent of this .iss. Do NOT put a trailing
; backslash here — Inno's preprocessor treats `\"` as an escaped
; quote and would eat the closing quote of the string.
#define ProjectRoot   ".."

; Path to the bundled Python installer. build.bat passes this via
; /DPyInstaller=... so the version updates automatically. The default
; below is only used when iscc is run without /D (e.g. F9 from the IDE).
#ifndef PyInstaller
  #define PyInstaller "deps\python-3.13.13-amd64.exe"
#endif


[Setup]
AppId={{6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\GhostShellAnty
DefaultGroupName=Ghost Shell Anty
AllowNoIcons=yes
OutputDir=output
OutputBaseFilename=GhostShellAntySetup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Make the wizard window ~33% larger than Inno's compact default. Gives
; the WizardImageFile (left panel of Welcome/Finish) a much bigger
; canvas — important for the branded background to actually be visible
; instead of looking like a postage-stamp thumbnail. Inno scales the
; whole layout proportionally, so text/buttons stay readable.
WizardSizePercent=133
DisableDirPage=auto
DisableProgramGroupPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; UsePreviousAppDir=yes (default) — Inno auto-reuses the previous
; install location on update. We additionally set UsePreviousLanguage
; and friends so a re-run inherits all the same answers.
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousSetupType=yes
UsePreviousTasks=yes

; Setup-window icon + Add/Remove Programs entry icon. The .ico in
; assets\ is a copy of dashboard\favicon.ico for brand consistency.
SetupIconFile=assets\ghost_shell.ico
UninstallDisplayIcon={app}\ghost_shell.ico

; Wizard background images. Generated at build time by
;   installer\scripts\build_wizard_images.ps1
; from the high-resolution master at assets\repository-template.png
; (the master is NEVER modified - the script only writes derivatives).
;
; Inno's modern wizard accepts only .bmp. The helper center-crops the
; master to 1x and 2x sizes for both the left panel (Welcome/Finish)
; and the top-right thumbnail (every other page). The comma-separated
; lists below let Inno auto-pick the closest size for the host's DPI:
; on a 4K / retina laptop it picks the 2x bitmap and renders crisply.
;
; Each WizardImage* directive is wrapped in #if FileExists() so a
; missing BMP (PowerShell helper failed or PNG source absent) doesn't
; abort the build - Inno just falls back to its built-in default.
#if FileExists(SourcePath + "assets\wizard_image.bmp")
  #if FileExists(SourcePath + "assets\wizard_image_2x.bmp")
    WizardImageFile=assets\wizard_image.bmp,assets\wizard_image_2x.bmp
  #else
    WizardImageFile=assets\wizard_image.bmp
  #endif
  WizardImageStretch=yes
  WizardImageBackColor=$0F1419
  WizardImageAlphaFormat=premultiplied
#else
  #pragma message "wizard_image.bmp missing - using default Inno image"
#endif
#if FileExists(SourcePath + "assets\wizard_small_image.bmp")
  #if FileExists(SourcePath + "assets\wizard_small_image_2x.bmp")
    WizardSmallImageFile=assets\wizard_small_image.bmp,assets\wizard_small_image_2x.bmp
  #else
    WizardSmallImageFile=assets\wizard_small_image.bmp
  #endif
#else
  #pragma message "wizard_small_image.bmp missing - using default Inno small image"
#endif

; Show "Update" or "Repair" in the Add/Remove Programs entry name when
; we're rerunning over an existing install.
CloseApplications=force
RestartApplications=no

; ─── Properties dialog metadata (right-click .exe → Properties → Details) ───
; All five fields show up in Windows' file-properties UI. Without these
; the .exe looks anonymous: blank Description, no Copyright, etc.
VersionInfoVersion={#AppFullVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoTextVersion={#AppFullVersion}
VersionInfoCopyright={#AppCopyright}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppFullVersion}
VersionInfoProductTextVersion={#AppFullVersion}


[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"


[Tasks]
Name: "desktopicon";   Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
Name: "startmenuicon"; Description: "Create a Start &menu shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce


[Files]
; ─── Helper script needed BEFORE the [Files] copy phase, so we use
;     `dontcopy` (bundled in payload, not auto-extracted) and pull it
;     out from Pascal via ExtractTemporaryFile('stop_server.ps1').
;     DestDir is irrelevant with dontcopy — Inno always extracts to {tmp}.
;
;     Why this matters: PrepareToInstall (which calls StopRunningServer)
;     fires BEFORE the normal [Files] section runs, so a plain
;     `DestDir: "{tmp}"; Flags: deleteafterinstall` entry was extracted
;     too LATE — FileExists({tmp}\stop_server.ps1) returned false and
;     the killer never ran. Lost a debug session to this. ───────────
Source: "scripts\stop_server.ps1"; Flags: dontcopy

; ─── rcedit.exe (bundled if available — used to stamp our icon
;     onto chrome.exe & chromedriver.exe so the running browser
;     shows the Ghost Shell icon, not the default Chromium globe).
;     Drop the official build into installer\deps\rcedit.exe — get it
;     from https://github.com/electron/rcedit/releases (it is one
;     ~600 KB .exe, MIT-licensed). If not bundled the icon-stamp
;     [Run] step below is skipped via Check: HasRcedit. ───────────
Source: "deps\rcedit.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist

; ─── Bundled Python installer (only deployed if user lacks Python) ──
Source: "{#PyInstaller}"; DestDir: "{tmp}"; Flags: deleteafterinstall ignoreversion; Check: NeedsPython

; ─── App icon — both for shortcuts and Add/Remove Programs ──────────
Source: "assets\ghost_shell.ico"; DestDir: "{app}"; Flags: ignoreversion

; ─── The whole project tree, EXCLUDING runtime state.
;     NOTE the leading backslashes in the Excludes patterns mean
;     "match at the source root only" so `\.git\*` doesn't accidentally
;     hit nested .git dirs.
;
;     `recursesubdirs createallsubdirs ignoreversion` overwrites existing
;     files — that's the update behaviour we want. User data tables
;     (ghost_shell.db, profiles\, *.log) are NOT in the source tree, so
;     there is nothing to overwrite — they're left in place.
Source: "{#ProjectRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Excludes: "\.git\*,\.venv\*,\.idea\*,\.vscode\*,\_legacy\*,\profiles\*,\reports\*,\installer\*,\chrome-test-profile\*,\scratch\*,\scripts\*,\c++\*,\с++\*,\tests\*,\__pycache__\*,*\__pycache__\*,*.pyc,ghost_shell.db*,ghost_shell.db,ghost_shell.db-shm,ghost_shell.db-wal,Home.md,scheduler.log,.scheduler.pid,scheduler_state.json,query_rate.json"


[InstallDelete]
; Clean up empty directories from a previous install where
; createallsubdirs was active and laid down empty .git/, .venv/, etc.
; shells. Without this, "Update" leaves the visual clutter behind.
; Type: filesandordirs is forgiving — non-existent paths are no-ops.
Type: filesandordirs; Name: "{app}\.git"
Type: filesandordirs; Name: "{app}\.idea"
Type: filesandordirs; Name: "{app}\.vscode"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\_legacy"
Type: filesandordirs; Name: "{app}\chrome-test-profile"
Type: filesandordirs; Name: "{app}\reports"
Type: filesandordirs; Name: "{app}\scratch"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\tests"
Type: filesandordirs; Name: "{app}\c++"
Type: filesandordirs; Name: "{app}\Ñ++"
Type: filesandordirs; Name: "{app}\installer"


[Icons]
Name: "{group}\{#AppName} Dashboard"; Filename: "{app}\bin\ghost_shell.bat"; WorkingDir: "{app}"; IconFilename: "{app}\ghost_shell.ico"; Tasks: startmenuicon
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"; Tasks: startmenuicon
Name: "{userdesktop}\{#AppName} Dashboard"; Filename: "{app}\bin\ghost_shell.bat"; WorkingDir: "{app}"; IconFilename: "{app}\ghost_shell.ico"; Tasks: desktopicon


[Run]
; 1a. Stamp our icon onto the bundled chrome.exe / chromedriver.exe.
;     Without this the browser shows the default Chromium globe
;     instead of the Ghost Shell icon — chrome.exe has no embedded
;     custom icon resource, only the dev binary does.
;     Check: HasRcedit makes this a no-op if rcedit.exe is missing
;     so users on a quick build (no rcedit in deps\) still install OK.
Filename: "{tmp}\rcedit.exe"; Parameters: """{app}\chrome_win64\chrome.exe"" --set-icon ""{app}\ghost_shell.ico"""; Flags: runhidden waituntilterminated; StatusMsg: "Branding chrome.exe..."; Check: HasRcedit
Filename: "{tmp}\rcedit.exe"; Parameters: """{app}\chrome_win64\chromedriver.exe"" --set-icon ""{app}\ghost_shell.ico"""; Flags: runhidden waituntilterminated; Check: HasRcedit

; 1. Install Python (silently) if not detected
Filename: "{tmp}\{#PyInstaller}"; Parameters: "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 SimpleInstall=1"; StatusMsg: "Installing Python {#PythonMin}+ ..."; Check: NeedsPython; Flags: waituntilterminated runhidden

; 2. Create the venv. On Update/Repair the venv already exists — `python -m venv`
;    is idempotent and just refreshes the launcher scripts; the heavy
;    site-packages install is skipped by pip on step 4 if nothing changed.
Filename: "{cmd}"; Parameters: "/C python -m venv ""{app}\.venv"""; StatusMsg: "Creating Python virtual environment ..."; Flags: waituntilterminated runhidden

; 3. Upgrade pip + install requirements
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m pip install --upgrade pip"; StatusMsg: "Upgrading pip ..."; Flags: waituntilterminated runhidden
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m pip install -r ""{app}\requirements.txt"""; StatusMsg: "Installing Ghost Shell dependencies (this can take a few minutes) ..."; Flags: waituntilterminated runhidden

; 4. Optional launch dashboard at the end
Filename: "{app}\bin\ghost_shell.bat"; Description: "Launch Ghost Shell Dashboard now"; Flags: postinstall nowait skipifsilent unchecked


[UninstallRun]
; On full uninstall, stop the running dashboard first via the same
; helper. InstallDir argument is not needed during uninstall — we only
; care about killing the server, not Chromium (it'll exit anyway when
; chrome_win64\ is wiped).
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer-tools\stop_server.ps1"""; Flags: runhidden; RunOnceId: "StopServer"; Check: FileExists(ExpandConstant('{app}\installer-tools\stop_server.ps1'))
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM python.exe /T"; Flags: runhidden; RunOnceId: "KillPython"


[UninstallDelete]
; ON FULL UNINSTALL ONLY — these get removed.
; Items NOT listed here that were created at runtime (ghost_shell.db,
; profiles\, *.log files written after install) are left behind on the
; user's disk by Inno's default behaviour. That's deliberate: the user
; can make a backup first if needed, and we'd rather leave data than
; surprise-delete it.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files;          Name: "{app}\ghost_shell.db*"
Type: files;          Name: "{app}\scheduler.log"
Type: files;          Name: "{app}\.scheduler.pid"


[Code]
// ══════════════════════════════════════════════════════════════
//  Pascal Script — detection, custom mode picker, stop-before-update.
//  Uses the helper in {tmp}\stop_server.ps1 for the actual process
//  control — easier to maintain in PowerShell than in PS Script.
// ══════════════════════════════════════════════════════════════

const
  // Mode picker indices — must stay in this order, matched by index in
  // CreateInputOptionPage's Add() calls.
  MODE_UPDATE    = 0;
  MODE_REPAIR    = 1;
  MODE_REINSTALL = 2;

var
  ExistingInstallDir: String;
  ExistingVersion:    String;
  ModePage:           TInputOptionWizardPage;
  PreviousInstallFound: Boolean;


// ────────────────────────────────────────────────────────────
//  Detection — read the registry entry Inno auto-writes for AppId
// ────────────────────────────────────────────────────────────

function GetUninstallRegKey(): String;
begin
  // Both Inno Setup variants (per-user / per-machine) end up under
  // Software\Microsoft\Windows\CurrentVersion\Uninstall. Per-user goes
  // to HKCU, per-machine to HKLM\SOFTWARE\WOW6432Node on x64. We try
  // HKCU first (matches PrivilegesRequired=lowest in [Setup]).
  //
  // NOTE on the GUID braces — there's an Inno preprocessor quirk:
  // [Setup] AppId={{X}} collapses the LEADING {{ to literal {, but
  // leaves the TRAILING }} as-is. So `AppId={{GUID}}` writes the
  // literal AppId `{GUID}}` (one open brace, two closes), and the
  // registry subkey becomes `{GUID}}_is1`. Confirmed live via the
  // diagnostic MsgBox showing HKLM key `{...}}_is1`. We MUST query
  // that exact form, not `{...}_is1` or `{{...}}_is1`. Earlier we
  // used {#emit SetupSetting("AppId")} but that returns the raw
  // pre-collapse string and didn't match either. Hardcoding to the
  // actual Inno-written form fixes detection. If you change AppId in
  // [Setup], regenerate this string by running the installer once
  // and reading the diagnostic log to see what Inno wrote.
  Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
            '{6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}}_is1';
end;

procedure DiagLog(msg: String);
var f: String;
begin
  // Append a line to %TEMP%\ghost_shell_install.log so we can see what
  // the registry probes returned even if Setup.exe is not run with /LOG.
  // Helps debug "Update/Repair page never shows" reports — the user can
  // share the log without needing dev-tools setup.
  f := ExpandConstant('{tmp}') + '\..\ghost_shell_install.log';
  SaveStringToFile(f, '[gs] ' + msg + #13#10, True);
end;

function TryRegPath(RootKey: Integer; const Path: String; var DirOut, VerOut: String): Boolean;
begin
  Result := False;
  if RegQueryStringValue(RootKey, Path, 'InstallLocation', DirOut) then
  begin
    DiagLog(' OK   InstallLocation=' + DirOut + ' (root=' + IntToStr(RootKey) + ')');
    if not RegQueryStringValue(RootKey, Path, 'DisplayVersion', VerOut) then
      VerOut := '';
    Result := True;
  end
  else
    DiagLog(' miss root=' + IntToStr(RootKey) + ' path=' + Path);
end;

function FindExistingInstall(): Boolean;
var
  V: String;
  Path: String;
  // Three key forms to try, in priority order:
  //   [0] {GUID}}_is1   — what Inno ACTUALLY writes when AppId={{X}}
  //                       (leading {{ collapses to {, trailing }} stays)
  //   [1] {GUID}_is1    — fallback for an installer built with AppId={{X}
  //                       (both braces collapsed) — possible if .iss is
  //                       edited
  //   [2] {{GUID}}_is1  — paranoid fallback for some hypothetical Inno
  //                       version that escapes neither brace
  // We try [0] first because that matches the diagnostic MsgBox output
  // ("HKLM\{6E0AC4E0-...}}_is1") seen in production.
  Paths: array[0..2] of String;
  i: Integer;
begin
  Result := False;
  ExistingInstallDir := '';
  ExistingVersion    := '';
  DiagLog('FindExistingInstall begin');

  Paths[0] := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}}_is1';
  Paths[1] := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}_is1';
  Paths[2] := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}}_is1';

  for i := 0 to 2 do
  begin
    Path := Paths[i];
    if TryRegPath(HKCU, Path, ExistingInstallDir, ExistingVersion) then
    begin
      Result := True; exit;
    end;
    if TryRegPath(HKLM, Path, ExistingInstallDir, ExistingVersion) then
    begin
      Result := True; exit;
    end;
    if TryRegPath(HKLM64, Path, ExistingInstallDir, ExistingVersion) then
    begin
      Result := True; exit;
    end;
  end;

  DiagLog('FindExistingInstall: none found');
end;


// ────────────────────────────────────────────────────────────
//  Custom mode-picker page (only shown when an install was found)
// ────────────────────────────────────────────────────────────

procedure InitializeWizard();
var
  Caption, SubCaption, Hint: String;
begin
  if not PreviousInstallFound then
    exit;

  Caption    := 'Existing installation detected';
  if ExistingVersion <> '' then
    SubCaption := 'Ghost Shell Anty ' + ExistingVersion + ' is installed at:'
  else
    SubCaption := 'Ghost Shell Anty is already installed at:';
  SubCaption := SubCaption + #13#10 + ExistingInstallDir;

  ModePage := CreateInputOptionPage(
    wpWelcome,
    Caption, SubCaption,
    'Choose what you want this installer to do. Your data ' +
    '(ghost_shell.db, profiles, vault) is preserved on Update and Repair.',
    True,    // exclusive (radio)
    False    // listbox style off
  );

  ModePage.Add('&Update — replace program files, keep all data (recommended)');
  ModePage.Add('&Repair — reinstall the current version, keep all data');
  ModePage.Add('Reinstall &fresh — wipe data first, then install (creates a backup)');

  // Default selection is Update for newer installer, Repair for same version.
  if (ExistingVersion <> '') and (CompareText(ExistingVersion, '{#AppFullVersion}') = 0) then
    ModePage.SelectedValueIndex := MODE_REPAIR
  else
    ModePage.SelectedValueIndex := MODE_UPDATE;
end;


function GetSelectedMode(): Integer;
begin
  // Treat absence of the page as "fresh install" → MODE_UPDATE flow path
  // (which is no-op preservation since there's no data to preserve).
  if PreviousInstallFound and Assigned(ModePage) then
    Result := ModePage.SelectedValueIndex
  else
    Result := MODE_UPDATE;
end;


function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  // On Update/Repair we re-use the previous install dir (UsePreviousAppDir).
  // No need to pester the user about it again.
  if PreviousInstallFound and (PageID = wpSelectDir) then
    Result := True;
  // Tasks page: only useful on fresh install. If shortcuts exist, leave them.
  if PreviousInstallFound and (PageID = wpSelectTasks) then
    Result := True;
end;


// ────────────────────────────────────────────────────────────
//  Pre-install hook — stop server, optionally back up DB,
//  optionally wipe install dir for "Reinstall fresh".
// ────────────────────────────────────────────────────────────

procedure StopRunningServer(InstallDir: String);
var
  ResultCode: Integer;
  ScriptPath: String;
begin
  // The .ps1 helper was extracted to {tmp} via [Files] earlier in the
  // wizard. We execute it synchronously: Inno blocks on
  // ewWaitUntilTerminated until the server is dead (or PowerShell
  // gives up after its own timeout).
  // Extract stop_server.ps1 from the installer payload to {tmp}.
  // We use dontcopy in [Files] so that Inno's normal copy phase
  // (which runs AFTER PrepareToInstall) doesn't gate availability.
  // ExtractTemporaryFile is safe to call multiple times — a no-op
  // after the first extraction.
  try
    ExtractTemporaryFile('stop_server.ps1');
  except
    Log('[stop] ExtractTemporaryFile failed: ' + GetExceptionMessage);
  end;

  ScriptPath := ExpandConstant('{tmp}\stop_server.ps1');
  if not FileExists(ScriptPath) then
  begin
    Log('[stop] helper script missing — skipping graceful shutdown');
    exit;
  end;

  WizardForm.StatusLabel.Caption := 'Stopping running Ghost Shell processes...';
  Log('[stop] running ' + ScriptPath + ' (InstallDir=' + InstallDir + ')');
  Exec('powershell.exe',
       '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath +
       '" -InstallDir "' + InstallDir + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Log('[stop] helper exited with code ' + IntToStr(ResultCode));
end;


function GetTimestampSuffix(): String;
begin
  // Inno Pascal Script doesn't expose GetTime / GetDate / Format the way
  // standard Free Pascal does — use GetDateTimeString instead. The
  // last two args are date/time separators that fire only when '/' or
  // ':' appear in the format string; ours has neither, so the digit
  // tokens and literal '-' produce "YYYYMMDD-HHMMSS" verbatim.
  Result := GetDateTimeString('yyyymmdd-hhnnss', #0, #0);
end;


procedure BackupDatabase(InstallDir: String; Mode: Integer);
var
  Src, Dst, BackupRoot: String;
begin
  // Always back up the DB on Reinstall (data is about to be wiped) and
  // on Update (in case the installer crashes mid-update). Repair keeps
  // bytes identical so backup is unnecessary.
  if Mode = MODE_REPAIR then exit;
  if InstallDir = '' then exit;

  Src := AddBackslash(InstallDir) + 'ghost_shell.db';
  if not FileExists(Src) then
  begin
    Log('[backup] no DB to back up at ' + Src);
    exit;
  end;

  BackupRoot := ExpandConstant('{localappdata}\GhostShellAnty\backup');
  ForceDirectories(BackupRoot);
  Dst := AddBackslash(BackupRoot) + 'ghost_shell.db.' + GetTimestampSuffix();
  if FileCopy(Src, Dst, False) then
    Log('[backup] DB copied to ' + Dst)
  else
    Log('[backup] FAILED to copy DB to ' + Dst + ' — proceeding anyway');
end;


procedure WipeForReinstall(InstallDir: String);
begin
  // "Reinstall fresh" — nuke user data so the new install starts empty.
  // The DB has already been backed up by BackupDatabase().
  if InstallDir = '' then exit;
  Log('[reinstall] wiping user data under ' + InstallDir);
  DelTree(AddBackslash(InstallDir) + 'profiles', True, True, True);
  DeleteFile(AddBackslash(InstallDir) + 'ghost_shell.db');
  DeleteFile(AddBackslash(InstallDir) + 'ghost_shell.db-shm');
  DeleteFile(AddBackslash(InstallDir) + 'ghost_shell.db-wal');
  DeleteFile(AddBackslash(InstallDir) + 'scheduler.log');
end;


function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Mode, ResultCode: Integer;
  TargetDir, HelperBin: String;
begin
  Result := '';
  NeedsRestart := False;

  // Use the previous install dir if we found one — that's what
  // UsePreviousAppDir does for [Files] copy too.
  if PreviousInstallFound then
    TargetDir := ExistingInstallDir
  else
    TargetDir := ExpandConstant('{app}');

  Mode := GetSelectedMode();

  // Always stop the server before touching files — even on a "fresh"
  // install where we somehow ended up with a running dashboard at the
  // target dir (shouldn't happen, but be defensive).
  StopRunningServer(TargetDir);

  // Belt-and-suspenders: even if the helper exited cleanly, give Windows
  // an extra moment to release file handles before [Files] starts copying.
  Sleep(400);

  if PreviousInstallFound then
  begin
    BackupDatabase(TargetDir, Mode);
    if Mode = MODE_REINSTALL then
      WipeForReinstall(TargetDir);
  end;

  // Persist the helper to the install dir so the uninstaller can re-use
  // it. Copying from {tmp} during PrepareToInstall is reliable because
  // the [Files] section already extracted it.
  HelperBin := AddBackslash(TargetDir) + 'installer-tools';
  ForceDirectories(HelperBin);
  FileCopy(ExpandConstant('{tmp}\stop_server.ps1'),
           AddBackslash(HelperBin) + 'stop_server.ps1', False);
end;


// ────────────────────────────────────────────────────────────
//  Setup-time entry points
// ────────────────────────────────────────────────────────────

function EnumGhostShellUninstallKeys(RootKey: Integer; const RootName: String): String;
var
  Names: TArrayOfString;
  i: Integer;
  Found: String;
begin
  // Enumerate every subkey under Uninstall\ and pick those whose name
  // contains "ghost" (case-insensitive). Helps us see what Inno actually
  // wrote — if the AppId string matches but our hardcoded path query
  // doesn't, we'll spot the difference here.
  Found := '';
  if RegGetSubkeyNames(RootKey, 'Software\Microsoft\Windows\CurrentVersion\Uninstall', Names) then
  begin
    for i := 0 to GetArrayLength(Names) - 1 do
    begin
      if (Pos('GhostShell', Names[i]) > 0) or (Pos('ghost', Names[i]) > 0)
         or (Pos('Ghost', Names[i]) > 0) or (Pos('GHOSTSHELL', Names[i]) > 0) then
      begin
        Found := Found + RootName + '\' + Names[i] + #13#10;
      end;
    end;
  end;
  Result := Found;
end;

function InitializeSetup(): Boolean;
var
  Diag: String;
  Hits: String;
begin
  Result := True;
  PreviousInstallFound := FindExistingInstall();

  // Build a diagnostic block visible to the user. The first three lines
  // are the verdict; below them we list every Uninstall\ subkey across
  // HKCU/HKLM containing "ghost" so we can compare against the hardcoded
  // GUID we query. If the diagnostic shows a key name we are NOT
  // matching, the AppId between this build and the previously-installed
  // build differs — our detection logic is fine, the historical install
  // is just from a different .iss generation and won't be picked up.
  Diag := 'PreviousInstallFound = ';
  if PreviousInstallFound then
    Diag := Diag + 'TRUE' + #13#10
            + 'InstallDir : ' + ExistingInstallDir + #13#10
            + 'Version    : ' + ExistingVersion + #13#10 + #13#10
  else
    Diag := Diag + 'FALSE (no match for the hardcoded AppId GUID)' + #13#10 + #13#10;

  Diag := Diag + 'Query paths tried (in order):' + #13#10
              + '  Software\Microsoft\Windows\CurrentVersion\Uninstall\' + #13#10
              + '    {6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}}_is1   <- actual form' + #13#10
              + '    {6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}_is1   <- single-brace fallback' + #13#10
              + '    {{6E0AC4E0-7CB7-4D4E-9C1F-GHOSTSHELL2026}}_is1 <- double-brace fallback' + #13#10 + #13#10;

  Hits := EnumGhostShellUninstallKeys(HKCU, 'HKCU');
  Hits := Hits + EnumGhostShellUninstallKeys(HKLM, 'HKLM');
  Hits := Hits + EnumGhostShellUninstallKeys(HKLM64, 'HKLM64');

  if Hits <> '' then
    Diag := Diag + 'Uninstall\ keys whose name contains "ghost":' + #13#10 + Hits
  else
    Diag := Diag + 'No Uninstall\ keys with "ghost" in the name (across HKCU/HKLM/HKLM64).' + #13#10;

  Diag := Diag + #13#10 + 'Press OK to continue. Share this content with the maintainer if Update/Repair page is missing.';

  // Diagnostic MsgBox triggers — any of these will show the popup:
  //   1) Marker file `.gs_debug` next to setup.exe  (survives UAC)
  //   2) Marker file `%TEMP%\.gs_debug`             (survives UAC)
  //   3) Env var GHOST_SHELL_DEBUG=1                (LOST through UAC
  //      elevation — only works if you ran cmd as administrator first
  //      and set the var there. Kept for compat; prefer the marker.)
  //   4) Installer started with /GSDEBUG cmdline switch
  if FileExists(ExpandConstant('{src}\.gs_debug'))
     or FileExists(ExpandConstant('{tmp}') + '\..\.gs_debug')
     or (GetEnv('GHOST_SHELL_DEBUG') = '1')
     or (Pos('/GSDEBUG', UpperCase(GetCmdTail)) > 0) then
    MsgBox(Diag, mbInformation, MB_OK);

  // Always log to the diagnostic file for post-mortem.
  DiagLog('===');
  DiagLog(Diag);

  if PreviousInstallFound then
    Log('[setup] existing install: ' + ExistingInstallDir +
        ' (version=' + ExistingVersion + ')')
  else
    Log('[setup] fresh install — no previous Ghost Shell Anty in registry');
end;


function HasRcedit(): Boolean;
begin
  // True iff rcedit.exe was bundled into {tmp}. The [Files] entry
  // uses skipifsourcedoesntexist, so missing-from-deps is silent —
  // we still need to skip the icon-stamp [Run] step or it'd fail
  // with "file not found".
  Result := FileExists(ExpandConstant('{tmp}\rcedit.exe'));
end;


function NeedsPython(): Boolean;
var
  ResultCode: Integer;
  PyVer: AnsiString;
begin
  // Run `python --version`, capture stdout to a temp file, look for an
  // acceptable major.minor in the output. Fail-open: if anything
  // misbehaves we install the bundled Python — safer than skipping.
  Result := True;
  if Exec('cmd.exe', '/C python --version 1>"' + ExpandConstant('{tmp}') +
          '\pyver.txt" 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
    begin
      LoadStringFromFile(ExpandConstant('{tmp}') + '\pyver.txt', PyVer);
      if (Pos('Python 3.11', PyVer) > 0) or
         (Pos('Python 3.12', PyVer) > 0) or
         (Pos('Python 3.13', PyVer) > 0) or
         (Pos('Python 3.14', PyVer) > 0) then
      begin
        Log('Detected acceptable Python: ' + PyVer);
        Result := False;
      end;
    end;
  end;
end;


procedure CurStepChanged(CurStep: TSetupStep);
var
  BatPath, BatBody: String;
begin
  if CurStep = ssPostInstall then
  begin
    BatPath := ExpandConstant('{app}\bin\ghost_shell.bat');
    ForceDirectories(ExpandConstant('{app}\bin'));
    BatBody :=
      '@echo off' + #13#10 +
      'REM Generated by GhostShellAntySetup -- opens the dashboard in your default browser.' + #13#10 +
      'cd /d "' + ExpandConstant('{app}') + '"' + #13#10 +
      'start "" "%~dp0..\\.venv\Scripts\pythonw.exe" -m ghost_shell dashboard' + #13#10;
    SaveStringToFile(BatPath, BatBody, False);
  end;
end;
