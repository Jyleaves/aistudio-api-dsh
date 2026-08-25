; Inno Setup script for Asteria
; Build: ISCC.exe installer.iss
; Output: dist/Asteria-setup-<version>.exe

#define MyAppName "Asteria"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.5"
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
AppSupportURL={#MyAppURL}
SetupIconFile=image\app-icon\icon.ico
DisableDirPage=no
DisableProgramGroupPage=yes
; 经典 Windows 应用安装位置；{autopf} 在 64 位系统映射到 Program Files
PrivilegesRequired=admin
DefaultDirName={autopf}\{#MyAppName}
OutputDir=dist
OutputBaseFilename=Asteria-setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "installer-languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Asteria\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; Use the current user's desktop even though the installer itself requires admin.
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: ""; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清理安装目录内的运行数据；用户数据（%LOCALAPPDATA%\Asteria）
; 是否删除由卸载时的询问对话框决定，见 [Code] 段
Type: filesandordirs; Name: "{app}\data"
Type: files; Name: "{app}\.env"

[Code]
var
  InstallDirExisted: Boolean;
  InstallCompleted: Boolean;
  InstallMarker: String;
  DeleteUserData: Boolean;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    InstallDirExisted := DirExists(ExpandConstant('{app}'));
    InstallMarker := AddBackslash(ExpandConstant('{app}')) + '.asteria-installing';
    SaveStringToFile(InstallMarker, 'Asteria installation in progress', False);
  end else if CurStep = ssPostInstall then begin
    InstallCompleted := True;
    DeleteFile(InstallMarker);
  end;
end;

procedure DeinitializeSetup();
begin
  { Remove only a newly-created incomplete installation directory. An upgrade
    keeps the existing directory and user data intact if cancelled. }
  if (not InstallCompleted) and (not InstallDirExisted) and
     FileExists(InstallMarker) then begin
    DeleteFile(InstallMarker);
    DelTree(ExpandConstant('{app}'), True, True, True);
  end;
end;

function InitializeUninstall(): Boolean;
begin
  DeleteUserData := False;
  if (not UninstallSilent()) and DirExists(ExpandConstant('{localappdata}\Asteria')) then begin
    DeleteUserData := (MsgBox(
      '是否同时删除用户数据（账号、登录状态、API Key 等运行数据）？' + #13#10#13#10 +
      '位置：' + ExpandConstant('{localappdata}\Asteria') + #13#10#13#10 +
      '选“是”将彻底清除这些数据；选“否”仅卸载程序，数据保留，下次安装后可继续使用已登录的账号。',
      mbConfirmation, MB_YESNO) = IDYES);
  end;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usUninstall) and DeleteUserData then begin
    DelTree(ExpandConstant('{localappdata}\Asteria'), True, True, True);
  end;
end;
