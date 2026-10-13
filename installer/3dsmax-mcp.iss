; Inno Setup script for the 3dcrgr fork of 3dsmax-mcp. Build it with installer\build_release.py,
; which stages the files this script expects under installer\stage and passes the version.
; It uses upstream's AppId, so it installs over (and is replaced by) upstream's installer cleanly.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "stage"
#endif
#ifndef Bridge2027SHA256
  #error "Build with build_release.py: reviewed bridge hash is required"
#endif
#ifndef Bridge2027Superseded
  #define Bridge2027Superseded ""
#endif

[Setup]
AppId={{56A47E4C-9E1D-488E-AF71-45A379AC1386}
AppName=3dsmax-mcp
AppVersion={#AppVersion}
AppVerName=3dsmax-mcp {#AppVersion} (3dcrgr fork)
AppPublisher=3dcrgr (fork of cl0nazepamm/3dsmax-mcp)
AppPublisherURL=https://github.com/3dcrgr/3dsmax-mcp
AppSupportURL=https://github.com/3dcrgr/3dsmax-mcp/issues
AppUpdatesURL=https://github.com/3dcrgr/3dsmax-mcp/releases
DefaultDirName={autopf}\3dsmax-mcp
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#StageDir}\..\output
OutputBaseFilename=3dsmax-mcp-{#AppVersion}-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\app.ico
LicenseFile={#StageDir}\app\LICENSE.txt
InfoBeforeFile=WHAT-THIS-FORK-DOES.txt
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Tasks]
Name: "desktop";  Description: "Claude Desktop"
Name: "claude";   Description: "Claude Code"
Name: "codex";    Description: "Codex"
Name: "cursor";   Description: "Cursor"; Flags: unchecked
Name: "gemini";   Description: "Gemini CLI"; Flags: unchecked
Name: "opencode"; Description: "opencode"; Flags: unchecked

[InstallDelete]
; An upstream or older fork install leaves its own package and metadata behind otherwise.
Type: filesandordirs; Name: "{app}\runtime\Lib\site-packages\maxmcp"
Type: filesandordirs; Name: "{app}\runtime\Lib\site-packages\3dsmax_mcp-*.dist-info"
Type: filesandordirs; Name: "{commonappdata}\Autodesk\ApplicationPlugins\3dsmax-mcp\Contents"

[Files]
Source: "{#StageDir}\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\bundle\*"; DestDir: "{commonappdata}\Autodesk\ApplicationPlugins\3dsmax-mcp"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{app}\runtime\python.exe"; Parameters: """{app}\configure.py"" --clients {code:ClientList}"; \
  StatusMsg: "Configuring your AI clients..."; Flags: runhidden waituntilterminated runasoriginaluser
Filename: "{app}\docs\WHAT-THIS-FORK-DOES.txt"; Description: "Read what this fork does"; \
  Flags: postinstall shellexec skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime"

[Code]
function ClientList(Param: String): String;
var
  Names: array[0..5] of String;
  I: Integer;
begin
  Names[0] := 'desktop'; Names[1] := 'claude'; Names[2] := 'codex';
  Names[3] := 'cursor'; Names[4] := 'gemini'; Names[5] := 'opencode';
  Result := '';
  for I := 0 to 5 do
    if WizardIsTaskSelected(Names[I]) then
    begin
      if Result <> '' then Result := Result + ',';
      Result := Result + Names[I];
    end;
  if Result = '' then Result := '""';
end;

function MaxIsRunning(): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/c tasklist /FI "IMAGENAME eq 3dsmax.exe" | find /I "3dsmax.exe" >nul',
                 '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Paths: array[0..1] of String;
  I: Integer;
  Digest: String;
begin
  Result := '';
  if MaxIsRunning() then
  begin
    Result := '3ds Max is running. Close every 3ds Max window (its bridge is in use), then run Setup again.';
    Exit;
  end;
  Paths[0] := ExpandConstant('{commonappdata}\Autodesk\ApplicationPlugins\3dsmax-mcp\Contents\bin\mcp_bridge_2027.gup');
  Paths[1] := ExpandConstant('{app}\runtime\Lib\site-packages\maxmcp\native\bin\mcp_bridge_2027.gup');
  for I := 0 to 1 do
    if FileExists(Paths[I]) then
    begin
      Digest := Lowercase(GetSHA256OfFile(Paths[I]));
      if (Digest <> '{#Bridge2027SHA256}') and
         (Pos(';' + Digest + ';', ';{#Bridge2027Superseded};') = 0) then
      begin
        Result := 'Installed bridge differs from the reviewed bundled or superseded builds. Refusing to overwrite a potentially newer bridge: ' + Paths[I];
        Exit;
      end;
    end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if MaxIsRunning() then
  begin
    MsgBox('3ds Max is running. Close every 3ds Max window, then uninstall again.', mbError, MB_OK);
    Result := False;
  end;
end;
