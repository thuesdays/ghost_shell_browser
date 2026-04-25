; ═══════════════════════════════════════════════════════════════
;  Ghost Shell — Inno Setup installer
;  See installer\README.md for build instructions.
; ═══════════════════════════════════════════════════════════════

#define AppName       "Ghost Shell"
#define AppVersion    "0.2.0"
#define AppPublisher  "Ghost Shell"
#define AppURL        "https://github.com/thuesdays/goodmed"
#define PythonMin     "3.11"

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
DefaultDirName={localappdata}\GhostShell
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=output
OutputBaseFilename=GhostShellAntySetup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
DisableDirPage=no
DisableProgramGroupPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Setup-window icon + Add/Remove Programs entry icon. The .ico in
; assets\ is a copy of dashboard\favicon.ico for brand consistency.
SetupIconFile=assets\ghost_shell.ico
UninstallDisplayIcon={app}\ghost_shell.ico


[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"


[Tasks]
Name: "desktopicon";   Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
Name: "startmenuicon"; Description: "Create a Start &menu shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce


[Files]
; ─── Bundled Python installer (only deployed if user lacks Python) ──
Source: "{#PyInstaller}"; DestDir: "{tmp}"; Flags: deleteafterinstall ignoreversion; Check: NeedsPython

; ─── App icon — both for shortcuts and Add/Remove Programs ──────────
Source: "assets\ghost_shell.ico"; DestDir: "{app}"; Flags: ignoreversion

; ─── The whole project tree, EXCLUDING runtime state. NOTE the leading
;     backslashes in the Excludes patterns mean "match at the source
;     root only" (so `\.git\*` doesn't accidentally hit nested .git dirs).
Source: "{#ProjectRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "\.git\*,\.venv\*,\.idea\*,\.vscode\*,\_legacy\*,\profiles\*,\reports\*,\installer\*,\chrome-test-profile\*,\__pycache__\*,*\__pycache__\*,*.pyc,ghost_shell.db,ghost_shell.db-shm,ghost_shell.db-wal,scheduler.log,.scheduler.pid,scheduler_state.json,query_rate.json"


[Icons]
Name: "{group}\{#AppName} Dashboard"; Filename: "{app}\bin\ghost_shell.bat"; WorkingDir: "{app}"; IconFilename: "{app}\ghost_shell.ico"; Tasks: startmenuicon
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"; Tasks: startmenuicon
Name: "{userdesktop}\{#AppName} Dashboard"; Filename: "{app}\bin\ghost_shell.bat"; WorkingDir: "{app}"; IconFilename: "{app}\ghost_shell.ico"; Tasks: desktopicon


[Run]
; 1. Install Python (silently) if not detected
Filename: "{tmp}\{#PyInstaller}"; Parameters: "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 SimpleInstall=1"; StatusMsg: "Installing Python {#PythonMin}+ ..."; Check: NeedsPython; Flags: waituntilterminated runhidden

; 2. Create the venv
Filename: "{cmd}"; Parameters: "/C python -m venv ""{app}\.venv"""; StatusMsg: "Creating Python virtual environment ..."; Flags: waituntilterminated runhidden

; 3. Upgrade pip + install requirements
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m pip install --upgrade pip"; StatusMsg: "Upgrading pip ..."; Flags: waituntilterminated runhidden
Filename: "{app}\.venv\Scripts\python.exe"; Parameters: "-m pip install -r ""{app}\requirements.txt"""; StatusMsg: "Installing Ghost Shell dependencies (this can take a few minutes) ..."; Flags: waituntilterminated runhidden

; 4. Optional launch dashboard at the end
Filename: "{app}\bin\ghost_shell.bat"; Description: "Launch Ghost Shell Dashboard now"; Flags: postinstall nowait skipifsilent unchecked


[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM python.exe /T"; Flags: runhidden; RunOnceId: "KillPython"


[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files;          Name: "{app}\ghost_shell.db*"
Type: files;          Name: "{app}\scheduler.log"
Type: files;          Name: "{app}\.scheduler.pid"


[Code]
function NeedsPython(): Boolean;
var
  ResultCode: Integer;
  PyVer: AnsiString;
begin
  // Inno's Pascal Script subset only knows `String` — no `AnsiString`.
  // We run `python --version`, capture stdout to a temp file, and look
  // for an acceptable major.minor in the output. Fail-open: if anything
  // misbehaves (no python at all, can't read temp file) we install the
  // bundled Python — safer than skipping.
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
