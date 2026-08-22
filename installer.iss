; Inno Setup script for Asteria
; Build: ISCC.exe installer.iss
; Output: dist/Asteria-setup-<version>.exe

#define MyAppName "Asteria"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Jyleaves"
#define MyAppURL "https://github.com/Jyleaves/aistudio-api-dsh"
#define MyAppExeName "aistudio-api.exe"

[Setup]
AppId={{8E3C4A56-9F2B-4E7D-A1C6-AISTUDIOAPI10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
SetupIconFile=image\app-icon\icon.ico
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
; 用户级安装：不需要管理员权限
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=Asteria-setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\aistudio-api\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: ""; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清理运行数据（账号 Cookie 等敏感内容不应残留）
Type: filesandordirs; Name: "{app}\data"
