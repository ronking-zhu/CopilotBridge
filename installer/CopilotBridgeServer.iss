; Inno Setup script for Copilot Bridge Server.
; Produces a single setup .exe that installs the onedir PyInstaller build
; (CopilotBridgeServer.exe + _internal\ + bundled devtunnel.exe) into Program
; Files, adds the install dir to the system PATH, and creates shortcuts.
;
; Build with:  scripts\build-installer.ps1   (compiles the exe first, then this)
; The script expects these defines (passed by build-installer.ps1):
;   AppVersion  - product version, e.g. 1.2.0
;   SourceDir   - the PyInstaller onedir folder (dist\CopilotBridgeServer)
;   OutputDir   - where to drop the setup .exe (dist)

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\CopilotBridgeServer"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef Edition
  #define Edition ""
#endif

#define AppName "Copilot Bridge Server"
#define AppPublisher "Copilot Bridge"
#define AppExe "CopilotBridgeServer.exe"

[Setup]
AppId={{8F3C5B2A-9E41-4C7D-A1B6-COPILOTBRIDGE}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\CopilotBridge
DefaultGroupName=Copilot Bridge
DisableProgramGroupPage=yes
; Install per-machine into Program Files (requires elevation), so PATH changes
; apply system-wide. ChangesEnvironment tells Explorer to broadcast the update.
PrivilegesRequired=admin
ChangesEnvironment=yes
OutputDir={#OutputDir}
#if Edition != ""
OutputBaseFilename=copilotbridgeserver-{#Edition}-{#AppVersion}-setup
#else
OutputBaseFilename=CopilotBridgeServer-{#AppVersion}-setup
#endif
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Branded icon for the installer .exe and the Programs-and-Features entry.
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
; ----- Upgrade behaviour -----
; Same AppId across versions => a new installer (e.g. 1.3.0) upgrades the existing
; install in place and the Control Panel "Programs and Features" entry is updated,
; not duplicated. Block installing an OLDER version over a newer one.
; CloseApplications lets Setup shut the running server/devtunnel so their files can
; be overwritten; RestartApplications is off (the user relaunches when ready).
AppMutex=CopilotBridgeServerMutex
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "addtopath";   Description: "Add Copilot Bridge to the system &PATH"; GroupDescription: "System:"

[Files]
; The whole PyInstaller onedir tree (exe + _internal\ + devtunnel.exe).
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; On upgrade, wipe the previous program payload first so files removed in the new
; version don't linger. Only the program dir is touched — user data lives in
; %LOCALAPPDATA%\CopilotBridge and is never deleted here.
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
; Remove anything the program created in its own folder on uninstall.
Type: filesandordirs; Name: "{app}\__pycache__"

[Icons]
Name: "{group}\Copilot Bridge Server"; Filename: "{app}\{#AppExe}"
Name: "{group}\Copilot Bridge Control Panel"; Filename: "{app}\{#AppExe}"; Parameters: "--control-panel"; Comment: "Start, stop, or restart the server and Dev Tunnel"
Name: "{group}\View Connection Info"; Filename: "{app}\{#AppExe}"; Parameters: "--show-info"; Comment: "Show this PC's server URL and API key"
Name: "{group}\Uninstall Copilot Bridge"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Copilot Bridge Server"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Add the install dir to the system PATH (when the task is selected). The check
; prevents duplicate entries on re-install/upgrade.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
  Check: NeedsAddPath('{app}'); Tasks: addtopath

[Run]
; Offer to launch the server right after install (shows the sign-in dialog).
Filename: "{app}\{#AppExe}"; Description: "Start Copilot Bridge Server now"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop the running server + tunnel before files are removed, so uninstall is clean.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM CopilotBridgeServer.exe /T"; \
  Flags: runhidden; RunOnceId: "KillServer"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM devtunnel.exe /T"; \
  Flags: runhidden; RunOnceId: "KillTunnel"

[Code]
// Stop a running server/tunnel so their files can be overwritten (upgrade) or
// removed (uninstall). taskkill is best-effort; failures are ignored.
procedure StopRunningServer;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM CopilotBridgeServer.exe /T',
    '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM devtunnel.exe /T',
    '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

// Compare dotted versions numerically: >0 if A>B, <0 if A<B, 0 if equal.
// (A plain string compare would wrongly rank "1.10.0" below "1.9.0".)
function CompareVersions(A, B: String): Integer;
var
  pa, pb, na, nb: Integer;
begin
  Result := 0;
  while ((Length(A) > 0) or (Length(B) > 0)) and (Result = 0) do
  begin
    pa := Pos('.', A);
    if pa = 0 then begin na := StrToIntDef(A, 0); A := ''; end
    else begin na := StrToIntDef(Copy(A, 1, pa - 1), 0); Delete(A, 1, pa); end;
    pb := Pos('.', B);
    if pb = 0 then begin nb := StrToIntDef(B, 0); B := ''; end
    else begin nb := StrToIntDef(Copy(B, 1, pb - 1), 0); Delete(B, 1, pb); end;
    if na > nb then Result := 1
    else if na < nb then Result := -1;
  end;
end;

// Block downgrades: refuse to install an older version over a newer one.
function InitializeSetup(): Boolean;
var
  Installed: string;
begin
  Result := True;
  // Inno's per-app uninstall key is "<AppId>_is1". This AppId ends in "}}"
  // (kept identical to the released 1.2.0 so upgrades replace it in place).
  if RegQueryStringValue(HKLM,
    'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{8F3C5B2A-9E41-4C7D-A1B6-COPILOTBRIDGE}}_is1',
    'DisplayVersion', Installed) then
  begin
    if CompareVersions(Installed, '{#AppVersion}') > 0 then
    begin
      if MsgBox('A newer version (' + Installed + ') is already installed. ' +
                'Install the older {#AppVersion} anyway?',
                mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
  end;
end;

// Before copying new files (e.g. during an in-place upgrade), stop the server so
// the locked exe/dll can be replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningServer;
  Result := '';
end;

function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKLM,
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
    'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  // Match exactly ;<path>; (case-insensitive) so we don't add a duplicate.
  Result := Pos(';' + Lowercase(Param) + ';', ';' + Lowercase(OrigPath) + ';') = 0;
end;

// Rebuild PATH with every entry equal to AppDir (case-insensitive) removed.
// Splitting on ';' and dropping matches removes ALL occurrences and all forms
// (leading/trailing/duplicate), which a single Pos/Delete would miss.
procedure RemoveDirFromSystemPath(AppDir: string);
var
  Path, Rebuilt, Item: string;
  P: Integer;
  Changed: Boolean;
begin
  if not RegQueryStringValue(HKLM,
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
    'Path', Path) then
    exit;
  Rebuilt := '';
  Changed := False;
  // Append a separator so the final item is processed by the loop.
  Path := Path + ';';
  repeat
    P := Pos(';', Path);
    Item := Copy(Path, 1, P - 1);
    Delete(Path, 1, P);
    if Item <> '' then
    begin
      if CompareText(Trim(Item), AppDir) = 0 then
        Changed := True
      else
      begin
        if Rebuilt <> '' then Rebuilt := Rebuilt + ';';
        Rebuilt := Rebuilt + Item;
      end;
    end;
  until Length(Path) = 0;
  if Changed then
    RegWriteStringValue(HKLM,
      'SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
      'Path', Rebuilt);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    // Stop the server/tunnel first so their files unlock.
    StopRunningServer;
    // Strip our install dir from the system PATH (all occurrences).
    RemoveDirFromSystemPath(ExpandConstant('{app}'));
  end;
end;
