#ifndef AppVersion
  #error Supply AppVersion from build.ps1
#endif
#ifndef AppGuid
  #define AppGuid "6B0C2F4E-5D0A-4C55-9E55-0C1B7A3D9A41"
#endif
#ifndef AppName
  #define AppName "Censor Studio"
#endif
#ifndef AppSource
  #define AppSource "..\dist\CensorStudio"
#endif

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=DarrnB
AppPublisherURL=https://github.com/DarrnB/censor-studio
DefaultDirName={autopf}\CensorStudio
DisableDirPage=no
UsePreviousAppDir=yes
AlwaysShowDirOnReadyPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=CensorStudio-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\CensorStudio.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD-PARTY.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\CensorStudio.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CensorStudio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CensorStudio.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
