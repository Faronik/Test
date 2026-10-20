#ifndef PackageDir
  #define PackageDir "..\..\build\windows-package"
#endif
#ifndef AppVersion
  #define AppVersion "2.1.1"
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
WizardSizePercent=120,130
UninstallDisplayIcon={app}\toolstock.exe
CloseApplications=no
SetupLogging=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "lan"; Description: "Разрешить доступ из локальной сети (профиль сети Windows: Частная)"; Flags: unchecked

[Files]
Source: "{#PackageDir}\installer-config.ps1"; Flags: dontcopy
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "toolstock.json,data\*,logs\*"

[Icons]
Name: "{group}\Открыть склад"; Filename: "http://localhost:8000"
Name: "{group}\Инструкция"; Filename: "{app}\WINDOWS.md"

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service.ps1"" -Action Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"

[Code]
var
  ConnectionActionPage: TWizardPage;
  KeepConnectionRadio, ChangeConnectionRadio: TNewRadioButton;
  DatabasePage: TInputQueryWizardPage;
  HasSavedPassword: Boolean;
  ExistingConfiguration: Boolean;
  StartupFailed: Boolean;

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

function EditConnection: Boolean;
begin
  Result := not ExistingConfiguration or ChangeConnectionRadio.Checked;
end;

function ConnectionValue(const Name, Fallback, Ini: String): String;
var Value: String;
begin
  Value := GetIniString('Connection', Name, '', Ini);
  if Length(Value) > 1 then Result := Copy(Value, 2, Length(Value) - 1)
  else Result := Fallback;
end;

procedure InitializeWizard;
var Ini: String; Code: Integer;
begin
  ExistingConfiguration := FileExists(ConfigPath);
  ConnectionActionPage := CreateCustomPage(wpSelectTasks,
    'Сохранённые настройки подключения', 'Можно сохранить прежние параметры или указать новые.');
  KeepConnectionRadio := TNewRadioButton.Create(WizardForm);
  KeepConnectionRadio.Parent := ConnectionActionPage.Surface;
  KeepConnectionRadio.Caption := 'Сохранить текущее подключение';
  KeepConnectionRadio.Width := ConnectionActionPage.SurfaceWidth;
  KeepConnectionRadio.Height := ScaleY(32);
  KeepConnectionRadio.Top := ScaleY(16);
  KeepConnectionRadio.Checked := True;
  ChangeConnectionRadio := TNewRadioButton.Create(WizardForm);
  ChangeConnectionRadio.Parent := ConnectionActionPage.Surface;
  ChangeConnectionRadio.Caption := 'Изменить подключение к PostgreSQL';
  ChangeConnectionRadio.Width := ConnectionActionPage.SurfaceWidth;
  ChangeConnectionRadio.Height := ScaleY(32);
  ChangeConnectionRadio.Top := ScaleY(64);
  DatabasePage := CreateInputQueryPage(ConnectionActionPage.ID,
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
  if ExistingConfiguration then begin
    ExtractTemporaryFile('installer-config.ps1');
    Ini := ExpandConstant('{tmp}\connection.ini');
    if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\installer-config.ps1') +
      '" -Mode Inspect -ConfigPath "' + ConfigPath + '" -ExchangePath "' + Ini + '"',
      '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then begin
      DatabasePage.Values[0] := ConnectionValue('PGHOST', '127.0.0.1', Ini);
      DatabasePage.Values[1] := ConnectionValue('PGPORT', '5432', Ini);
      DatabasePage.Values[2] := ConnectionValue('PGDATABASE', 'toolstock', Ini);
      DatabasePage.Values[3] := ConnectionValue('PGUSER', 'toolstock', Ini);
      HasSavedPassword := GetIniString('Connection', 'HasPassword', '0', Ini) = '1';
      DeleteFile(Ini);
    end;
    if HasSavedPassword then DatabasePage.PromptLabels[4].Caption := 'Новый пароль (пусто — сохранить прежний):';
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := ((PageID = ConnectionActionPage.ID) and not ExistingConfiguration) or
    ((PageID = DatabasePage.ID) and not EditConnection) or
    ((PageID = wpSelectTasks) and ExistingConfiguration);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var I, Port: Integer;
begin
  Result := True;
  if CurPageID <> DatabasePage.ID then Exit;
  for I := 0 to 4 do
    if (Length(Trim(DatabasePage.Values[I])) = 0) and not ((I = 4) and HasSavedPassword and ExistingConfiguration) then begin
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
var Directory, DataDirectory, Body, Host, Params, Request: String; Code: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  Directory := ExpandConstant('{commonappdata}\Toolstock\config');
  DataDirectory := ExpandConstant('{commonappdata}\Toolstock\data');
  if EditConnection then begin
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
    Request := Directory + '\setup-connection.json';
    try
      if not SaveStringToFile(Request, UTF8Encode(Body), False) then RaiseException('Cannot write connection request');
      if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\installer-config.ps1') +
        '" -Mode Apply -ConfigPath "' + ConfigPath + '" -ExchangePath "' + Request + '"',
        '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
        RaiseException('Не удалось сохранить подключение. Существующие настройки сохранены.');
    finally
      DeleteFile(Request);
      DatabasePage.Values[4] := ''; Body := '';
    end;
  end;
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\service.ps1') +
    '" -Action Install -ConfigPath "' + ConfigPath + '"';
  if WizardIsTaskSelected('lan') then Params := Params + ' -Lan';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
    '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then begin
    StartupFailed := True;
    SuppressibleMsgBox('Файлы установлены, но сервер не запустился. Проверьте подключение PostgreSQL и журнал C:\ProgramData\Toolstock\data\logs\server.log. Инструкция — WINDOWS.md.', mbError, MB_OK, IDOK);
  end
  else SuppressibleMsgBox('Сервер работает. Откройте http://localhost:8000 и создайте администратора. Дальнейшая работа выполняется в браузере.', mbInformation, MB_OK, IDOK);
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 0;
  if StartupFailed then Result := 1;
end;
