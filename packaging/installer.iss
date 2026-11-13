; Inno Setup script for TGMT5. Builds a single TGMT5-Setup-<version>.exe in the repository root.
; Build: packaging\build_installer.ps1  (it passes /DAppVersion=<version from pyproject.toml>)
;
; Choices the wizard offers:
;   - install for the current user only (no admin) or for all users (asks for admin)
;   - install folder
;   - Start-menu folder name, or no Start-menu folder at all
;   - desktop shortcut, Sandbox demo shortcut, start with Windows
;   - launch the app when setup finishes
;   - on uninstall: also delete the app data and saved credentials (asked, default No)

#define AppName "TGMT5"
#ifndef AppVersion
  #define AppVersion "0.3.0"
#endif
#define AppPublisher "Petcu Iulian"
#define AppExe "TGMT5.exe"
#define AppDescription "Telegram -> MetaTrader 5 copy trading (demo accounts)"

[Setup]
AppId={{8C1F4E2A-6B3D-4F7A-9E21-5D0C7A3B9F14}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppComments={#AppDescription}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}

; Per-user by default; the wizard lets the user switch to "all users" (then UAC asks for admin).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
; {autopf} = %LOCALAPPDATA%\Programs for one user, C:\Program Files for all users.
DefaultDirName={autopf}\{#AppName}
; on an update (same AppId found) the folder and Start-menu pages are skipped
DisableDirPage=auto
DefaultGroupName={#AppName}
DisableProgramGroupPage=auto
; Shows the "Don't create a Start Menu folder" checkbox.
AllowNoIcons=yes
DisableWelcomePage=no
DisableReadyPage=no
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes

MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=..
OutputBaseFilename=TGMT5-Setup-{#AppVersion}
SetupIconFile=tgmt5.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE
#ifdef SIGN
; "tgmt5sign" is defined by build_installer.ps1 (ISCC /S...) and calls packaging\sign.ps1.
SignTool=tgmt5sign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
SandboxIcon=Create a "Sandbox demo" shortcut (simulated broker, no credentials needed)
AutoStart=Start {#AppName} automatically when I sign in to Windows
OtherOptions=Other options:
UninstallData=Also delete the {#AppName} data (database, logs, settings) and the credentials saved in Windows Credential Manager?%n%nChoose No to keep them for a later reinstall.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "sandboxicon"; Description: "{cm:SandboxIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:OtherOptions}"; Flags: unchecked

[Files]
Source: "..\dist\TGMT5\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[InstallDelete]
; v0.1.0 put its shortcuts straight into Start > Programs; remove them so an update leaves no duplicates
Type: files; Name: "{autoprograms}\{#AppName}.lnk"
Type: files; Name: "{autoprograms}\{#AppName} (Sandbox demo).lnk"

[Icons]
; {group} entries are skipped automatically when "Don't create a Start Menu folder" is ticked.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} (Sandbox demo)"; Filename: "{app}\{#AppExe}"; Parameters: "--sandbox"; Tasks: sandboxicon
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{autostartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// Before files are replaced (install or update), back up the settings/history database.
// Settings are never overwritten by setup; this copy is an extra safety net.
procedure BackupFile(const Src, DestDir: String);
begin
  if FileExists(Src) then
    FileCopy(Src, AddBackslash(DestDir) + ExtractFileName(Src), False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir, Dest: String;
begin
  if CurStep <> ssInstall then Exit;
  DataDir := ExpandConstant('{localappdata}\{#AppName}');
  if not FileExists(DataDir + '\tgmt5.db') then Exit;
  Dest := DataDir + '\backups\before-update-{#AppVersion}-' + GetDateTimeString('yyyymmdd-hhnnss', #0, #0);
  if ForceDirectories(Dest) then
  begin
    BackupFile(DataDir + '\tgmt5.db', Dest);
    BackupFile(DataDir + '\tgmt5.db-wal', Dest);
    BackupFile(DataDir + '\tgmt5.db-shm', Dest);
    Log('Settings backed up to ' + Dest);
  end;
end;

// On uninstall, optionally remove the per-user data folder and the keyring entries
// (keyring stores them in Credential Manager as "<key>@tgmt5", and the first one as plain "tgmt5").
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  Cmd: String;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  if UninstallSilent then Exit;
  if MsgBox(CustomMessage('UninstallData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES then Exit;

  DelTree(ExpandConstant('{localappdata}\{#AppName}'), True, True, True);

  Cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    'cmdkey /list | Select-String ''target=((\S+@)?tgmt5)$'' | ' +
    'ForEach-Object { cmdkey /delete:$($_.Matches[0].Groups[1].Value) | Out-Null }"';
  Exec('powershell.exe', Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
