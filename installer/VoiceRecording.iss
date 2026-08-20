; Builds a per-user installer. User-created data is intentionally excluded from source files.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceRoot
  #define SourceRoot ".."
#endif
#ifndef OutputDir
  #define OutputDir "..\\dist"
#endif

[Setup]
AppId={{B050D750-2ED2-45BC-AE50-860F78B8B8F8}
AppName=Voice Recording
AppVersion={#AppVersion}
AppPublisher=Voice Recording
DefaultDirName={localappdata}\Programs\VoiceRecording
DisableDirPage=no
DefaultGroupName=Voice Recording
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=VoiceRecording-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayName=Voice Recording
VersionInfoVersion={#AppVersion}

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".git\*;build\*;data\*;dist\*;runtime\*;__pycache__\*;.env;AccessKey.csv;deepseekkey.txt;dashscopekey.txt;bailiankey.txt;*key*.txt;*apiKey*.csv;=;installer\Output\*"

[Icons]
Name: "{autoprograms}\Voice Recording\Open dashboard"; Filename: "http://localhost:8501"
Name: "{autoprograms}\Voice Recording\Start background service"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\Launch-VoiceRecorder.vbs"""
Name: "{autodesktop}\Voice Recording"; Filename: "http://localhost:8501"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\Launch-VoiceRecorder.vbs"""; Description: "Start Voice Recording now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Stop-VoiceRecorder.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "StopVoiceRecording"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  StopScript: String;
begin
  StopScript := ExpandConstant('{app}\Stop-VoiceRecorder.ps1');
  if FileExists(StopScript) then
    Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -File "' + StopScript + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExamplePath: String;
  EnvPath: String;
  VenvConfigPath: String;
  VenvConfig: String;
  VenvConfigAnsi: AnsiString;
begin
  if CurStep <> ssPostInstall then
    exit;

  ExamplePath := ExpandConstant('{app}\.env.example');
  EnvPath := ExpandConstant('{app}\.env');
  if (not FileExists(EnvPath)) and FileExists(ExamplePath) then
    CopyFile(ExamplePath, EnvPath, False);

  VenvConfigPath := ExpandConstant('{app}\.venv\pyvenv.cfg');
  if LoadStringFromFile(VenvConfigPath, VenvConfigAnsi) then
  begin
    VenvConfig := VenvConfigAnsi;
    StringChangeEx(VenvConfig, '@PYTHON_HOME@', ExpandConstant('{app}\python-runtime'), False);
    SaveStringToFile(VenvConfigPath, AnsiString(VenvConfig), False);
  end;
end;
