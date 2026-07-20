; Inno Setup script for multibuy — builds dist\multibuy-setup.exe
; Build:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" multibuy.iss
; (build.bat runs this automatically if Inno Setup is installed.)

#define MyAppName "multibuy"
#define MyAppVersion "1.0.0"
#define MyAppExe "multibuy.exe"

[Setup]
AppId={{7B9C2A10-4E1D-4C4E-9A2B-9E2F1A6C0D33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=multibuy
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExe}
SetupIconFile=multibuy.ico
OutputDir=dist
OutputBaseFilename=multibuy-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Install per-user so no admin prompt is needed; data lives in %APPDATA%\multibuy
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
