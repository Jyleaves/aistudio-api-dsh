; Incremental updater for an existing Asteria installation.
; This package deliberately excludes cloakbrowser-chromium.

#define MyAppName "Asteria"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "Jyleaves"
#define MyAppURL "https://github.com/Jyleaves/aistudio-api-dsh"
#define MyAppExeName "Asteria.exe"

[Setup]
AppId={{8E3C4A56-9F2B-4E7D-A1C6-AISTUDIOAPI10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
DefaultDirName={autopf}\{#MyAppName}
UsePreviousAppDir=yes
Uninstallable=no
CreateUninstallRegKey=no
OutputDir=dist
OutputBaseFilename=Asteria-update-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "installer-languages\ChineseSimplified.isl"

[Files]
Source: "dist\Asteria-update\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
