#ifndef PackageDir
  #define PackageDir "..\..\build\windows-package"
#endif
#ifndef AppVersion
  #define AppVersion "2.1.0"
#endif
[Setup]
AppId={{FA099D8B-DC4E-4782-956B-68E60999F889}
AppName=Toolstock
AppVersion={#AppVersion}
DefaultDirName={autopf}\Toolstock
DefaultGroupName=Toolstock
OutputDir=..\..\dist
OutputBaseFilename=Toolstock-Setup-{#AppVersion}-windows-x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\toolstock.exe
CloseApplications=no
SetupLogging=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "lan"; Description: "Разрешить доступ из локальной сети (профиль сети Windows: Частная)"; Flags: unchecked

[Files]
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "toolstock.json,data\*,logs\*"

[Icons]
Name: "{group}\Открыть склад"; Filename: "http://localhost:8000"
Name: "{group}\Инструкция"; Filename: "{app}\WINDOWS.md"

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service.ps1"" -Action Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"

[Code]
var
  DatabasePage: TInputQueryWizardPage;
  ExistingConfiguration: Boolean;

function ConfigPath: String;
begin
  Result := ExpandConstant('{commonappdata}\Toolstock\config\toolstock.json');
end;

function EscapeJson(Value: String): String;
begin
  StringChangeEx(Value, '\', '\\', True);
  StringChangeEx(Value, '"', '\"', True);
  StringChangeEx(Value, #13, '\r', True);
  StringChangeEx(Value, #10, '\n', True);
  StringChangeEx(Value, #9, '\t', True);
  Result := '"' + Value + '"';
end;

procedure InitializeWizard;
begin
  ExistingConfiguration := FileExists(ConfigPath);
  DatabasePage := CreateInputQueryPage(wpSelectTasks,
    'Подключение к PostgreSQL', 'PostgreSQL должен быть установлен и запущен.',
    'Укажите отдельную базу и её владельца (не суперпользователя). Инструкция — WINDOWS.md. Пароль сохраняется в защищённом файле на этом компьютере.');
  DatabasePage.Add('Адрес сервера PostgreSQL:', False);
  DatabasePage.Add('Порт PostgreSQL:', False);
  DatabasePage.Add('База данных:', False);
  DatabasePage.Add('Пользователь базы данных:', False);
  DatabasePage.Add('Пароль пользователя базы данных:', True);
  DatabasePage.Values[0] := '127.0.0.1';
  DatabasePage.Values[1] := '5432';
  DatabasePage.Values[2] := 'toolstock';
  DatabasePage.Values[3] := 'toolstock';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = DatabasePage.ID) and ExistingConfiguration;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var I, Port: Integer;
begin
  Result := True;
  if CurPageID <> DatabasePage.ID then Exit;
  for I := 0 to 4 do
    if Length(Trim(DatabasePage.Values[I])) = 0 then begin
      MsgBox('Заполните все параметры подключения.', mbError, MB_OK);
      Result := False; Exit;
    end;
  Port := StrToIntDef(DatabasePage.Values[1], 0);
  if (Port < 1) or (Port > 65535) then begin
    MsgBox('Порт PostgreSQL должен быть от 1 до 65535.', mbError, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\service.ps1')) then
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\service.ps1') + '" -Action Stop',
      '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      Result := 'Не удалось остановить службу Toolstock для обновления.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Directory, DataDirectory, Body, Host, Params: String; Code: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  Directory := ExpandConstant('{commonappdata}\Toolstock\config');
  DataDirectory := ExpandConstant('{commonappdata}\Toolstock\data');
  if not ExistingConfiguration then begin
    if not ForceDirectories(Directory) then RaiseException('Cannot create configuration directory');
    { Protect the directory BEFORE writing the password. Numeric SIDs work on localized Windows. }
    if not Exec(ExpandConstant('{sys}\icacls.exe'), '"' + Directory + '" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-19:(OI)(CI)R"', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      RaiseException('Cannot protect configuration directory');
    Host := '127.0.0.1';
    if WizardIsTaskSelected('lan') then Host := '0.0.0.0';
    Body := '{"PGHOST":' + EscapeJson(DatabasePage.Values[0]) +
      ',"PGPORT":' + EscapeJson(DatabasePage.Values[1]) +
      ',"PGDATABASE":' + EscapeJson(DatabasePage.Values[2]) +
      ',"PGUSER":' + EscapeJson(DatabasePage.Values[3]) +
      ',"PGPASSWORD":' + EscapeJson(DatabasePage.Values[4]) +
      ',"PGSSLMODE":"prefer","TOOLSTOCK_HOST":' + EscapeJson(Host) +
      ',"TOOLSTOCK_PORT":"8000","TOOLSTOCK_DATA":' + EscapeJson(DataDirectory) +
      ',"TOOLSTOCK_LOG":' + EscapeJson(DataDirectory + '\logs\server.log') + '}';
    if not SaveStringToFile(ConfigPath, UTF8Encode(Body), False) then RaiseException('Cannot save configuration');
    DatabasePage.Values[4] := ''; Body := '';
  end;
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\service.ps1') +
    '" -Action Install -ConfigPath "' + ConfigPath + '" -DataPath "' + DataDirectory + '"';
  if WizardIsTaskSelected('lan') then Params := Params + ' -Lan';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
    '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    SuppressibleMsgBox('Файлы установлены, но сервер не запустился. Проверьте подключение PostgreSQL и журнал C:\ProgramData\Toolstock\data\logs\server.log. Инструкция — WINDOWS.md.', mbError, MB_OK, IDOK)
  else SuppressibleMsgBox('Сервер работает. Откройте http://localhost:8000 и создайте администратора. Дальнейшая работа выполняется в браузере.', mbInformation, MB_OK, IDOK);
end;
