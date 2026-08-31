#define AppName "Wechat Publisher Agent"
#define AppVersion "0.4.1"
#define AppExeName "WechatPublisherAgent.exe"

[Setup]
AppId={{51C70DD0-553E-4D51-9879-A2982242CD5A}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\WechatPublisherAgent
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=WechatPublisherAgent-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\agent-icon.ico
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "..\dist\WechatPublisherAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\install-startup.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\scripts\remove-startup.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\scripts\verify-installed-agent.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
; Optional deployment secret. The Agent imports it into Windows DPAPI on first
; start and removes the plaintext copy from LocalAppData immediately.
Source: "{src}\agent-bootstrap.json"; DestDir: "{localappdata}\WechatPublisherAgent"; DestName: "bootstrap.json"; Flags: external skipifsourcedoesntexist

[Icons]
Name: "{group}\Wechat Publisher Agent"; Filename: "{app}\{#AppExeName}"; Parameters: "--agent-ui"
Name: "{autodesktop}\Wechat Publisher Agent"; Filename: "{app}\{#AppExeName}"; Parameters: "--agent-ui"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\install-startup.ps1"" -ExecutablePath ""{app}\{#AppExeName}"""; Flags: runhidden waituntilterminated; StatusMsg: "Registering user logon startup..."
Filename: "{app}\{#AppExeName}"; Parameters: "--agent"; Flags: nowait runhidden postinstall skipifsilent
Filename: "{app}\{#AppExeName}"; Parameters: "--agent-ui"; Description: "Open Windows Agent control panel"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\remove-startup.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveStartupTask"

[Messages]
FinishedLabel=Installation is complete. The Agent runs in the signed-in user session and is managed from a native Windows control panel. Local task ledger and credentials are kept in %LOCALAPPDATA%\WechatPublisherAgent during uninstall so pending result events are not lost.
