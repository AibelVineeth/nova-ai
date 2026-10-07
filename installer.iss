; installer.iss
; Inno Setup script - builds a real Windows installer for Nova.
;
; Requires Inno Setup (free): https://jrsoftware.org/isdl.php
; After installing Inno Setup, either:
;   - Open this file in the Inno Setup Compiler GUI and click "Compile", or
;   - Run from command line:  ISCC.exe installer.iss
;
; This expects the PyInstaller build to already exist at dist\Nova\
; (i.e. run "pyinstaller nova.spec" first - see build.bat for the full flow)

#define MyAppName "Nova"
#define MyAppVersion "1.0"
#define MyAppPublisher "Aibel"
#define MyAppExeName "Nova.exe"

[Setup]
AppId={{B8F1C6A0-5A2E-4C9D-9F3A-0E1D2C3B4A5F}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
SetupIconFile=nova_icon.ico
; Output installer .exe goes here:
OutputDir=installer_output
OutputBaseFilename=NovaSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Nova needs mic/camera access and launches a real Chrome window for
; browser features - no special privilege escalation needed beyond
; a normal install, so we don't request admin rights.
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Pulls in the ENTIRE PyInstaller output folder (the exe plus all its
; bundled dependencies) - recursesubdirs is essential here, this folder
; will contain thousands of files given torch/mediapipe's size.
Source: "dist\Nova\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
