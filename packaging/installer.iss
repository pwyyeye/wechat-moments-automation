#define AppName "微信小助手"
#define AppVersion "0.6.3"
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
OutputBaseFilename=微信小助手-{#AppVersion}-setup
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
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--agent-ui"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--agent-ui"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WechatPublisherAgent"; ValueData: """{app}\{#AppExeName}"" --agent"; Flags: uninsdeletevalue

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\install-startup.ps1"" -ExecutablePath ""{app}\{#AppExeName}"""; Flags: runhidden waituntilterminated; StatusMsg: "Registering user logon startup..."
Filename: "{app}\{#AppExeName}"; Parameters: "--agent"; Flags: nowait runhidden postinstall skipifsilent
Filename: "{app}\{#AppExeName}"; Parameters: "--agent-ui"; Description: "打开微信小助手"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\remove-startup.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveStartupTask"

[Messages]
FinishedLabel=微信小助手安装完成。应用会在当前 Windows 用户登录后运行；卸载时会保留 %LOCALAPPDATA%\WechatPublisherAgent 中的本机任务和凭据，避免待处理数据丢失。
