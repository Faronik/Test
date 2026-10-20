# Windows: веб-версия без Docker и Python

Сервер — обычная программа C++20 для Windows 10/11 x64 и Windows Server 2019+. Клиенты работают в браузере. Docker, WSL, Python, Node.js и компилятор на компьютере пользователя не нужны. Библиотеки включены в сервер статически, отдельно устанавливать Visual C++ Redistributable не требуется. PostgreSQL — отдельная служба на этом же компьютере или другом сервере.

## 1. Подготовьте PostgreSQL

Если PostgreSQL ещё не установлен, скачайте установщик с [официальной страницы PostgreSQL для Windows](https://www.postgresql.org/download/windows/). Рекомендуется PostgreSQL 17. Установите сервер и pgAdmin, сохраните заданный пароль администратора PostgreSQL.

В pgAdmin подключитесь к серверу как `postgres`, откройте Query Tool и выполните команды **по отдельности**, с включённым Auto-commit. Замените пример пароля своим; не используйте пароль суперпользователя:

```sql
CREATE ROLE toolstock LOGIN PASSWORD 'ЗАМЕНИТЕ_СВОИМ_ПАРОЛЕМ';
```

```sql
CREATE DATABASE toolstock OWNER toolstock ENCODING 'UTF8';
```

Для существующей корпоративной базы администратор выдаёт отдельную пустую базу, её адрес, порт и учётную запись владельца. Приложение само создаёт таблицы; права суперпользователя ему не нужны. Для удалённой базы настройте разрешённые подключения PostgreSQL (`pg_hba.conf` и `listen_addresses`).

## 2. Установите сервер склада

1. Скачайте `Toolstock-Setup-2.1.0-windows-x64.exe` из [Releases](https://github.com/Faronik/Test/releases). ZIP **Source code** содержит исходники, а не готовую программу. Если release ещё не собран, результат Windows-сборки появится в [Actions → Windows native server](https://github.com/Faronik/Test/actions/workflows/windows-native.yml).
2. Запустите установщик с правами администратора. По умолчанию файлы программы попадут в `C:\Program Files\Toolstock`.
3. Укажите адрес PostgreSQL, порт, базу, пользователя `toolstock` и его пароль из предыдущего шага.
4. По умолчанию сайт доступен только на этом компьютере. Для других компьютеров отметьте доступ из локальной сети; правило Windows Firewall открывает порт 8000 только для локальной подсети в профиле «Частная».
5. Дождитесь проверки готовности сервера. Откройте `http://localhost:8000`, создайте администратора программы. На других компьютерах откройте `http://ИМЯ_ИЛИ_IP_СЕРВЕРА:8000`.

Администратор программы создаётся в браузере и отличается от пользователя PostgreSQL. Работать с каталогом, складскими операциями, заявками и справочниками нужно в браузере; консоль для повседневной работы не требуется.

Служба Windows `Toolstock` использует `LocalService`, запускается автоматически с задержкой после включения компьютера, перезапускается при сбое. Окно консоли держать открытым не нужно. Компьютер с сервером должен оставаться включённым для доступа с других рабочих мест.

Пути:

| Назначение | Путь по умолчанию |
|---|---|
| Программа, интерфейс, SQL, документация API | `C:\Program Files\Toolstock` |
| Настройки подключения, включая пароль | `C:\ProgramData\Toolstock\config\toolstock.json` |
| Фотографии | `C:\ProgramData\Toolstock\data\photos` |
| Журнал сервера | `C:\ProgramData\Toolstock\data\logs\server.log` |
| Складские записи | В выбранной базе PostgreSQL |

Пароль хранится в конфигурации открытым текстом с ограничениями NTFS: администраторы и SYSTEM управляют файлом, служба читает его. Обычным пользователям чтение запрещено. Не отправляйте этот файл и резервные копии посторонним.

## Управление и исправление подключения

Используйте приложение Windows «Службы» (`services.msc`) либо PowerShell **от администратора**:

```powershell
Set-Location 'C:\Program Files\Toolstock'
.\service.ps1 -Action Status
.\service.ps1 -Action Stop
.\service.ps1 -Action Start
```

Если политика PowerShell блокирует локальный скрипт, выполните его через `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\service.ps1 -Action Status`. Этот параметр действует только для запущенного процесса.

При ошибке подключения проверьте, что PostgreSQL запущен, база существует, имя и пароль верны. Исправьте файл конфигурации в редакторе, запущенном от администратора, и перезапустите службу. Для повторной настройки с защищённым вводом пароля:

```powershell
.\service.ps1 -Action Stop
.\configure.ps1 -ConfigPath 'C:\ProgramData\Toolstock\config\toolstock.json' -DataPath 'C:\ProgramData\Toolstock\data' -DatabaseHost '127.0.0.1' -DatabaseName 'toolstock' -DatabaseUser 'toolstock' -Reconfigure
.\service.ps1 -Action Start
```

Для доступа из сети у `configure.ps1` есть ключ `-Lan`; затем выполните `service.ps1 -Action Install -Lan`, чтобы создать правило firewall. Для возврата к локальному доступу задайте `TOOLSTOCK_HOST` равным `127.0.0.1` и удалите правило: `Remove-NetFirewallRule -Name Toolstock-HTTP`.

Если порт 8000 занят, измените строку `TOOLSTOCK_PORT` в конфигурации. При изменении порта для локальной сети повторите `service.ps1 -Action Install -Lan`. Ярлык меню «Пуск» рассчитан на порт 8000 — для другого порта используйте адрес в браузере.

Для PostgreSQL через недоверенную сеть используйте `PGSSLMODE: "verify-full"` и `PGSSLROOTCERT` с путём к сертификату центра сертификации. Для HTTPS сайта поставьте обратный прокси и задайте `TOOLSTOCK_SECURE_COOKIE: "1"`. Встроенный HTTP-сервер сам TLS не обслуживает.

## Ручной запуск без службы

Распакуйте `Toolstock-2.1.0-windows-x64.zip` в доступную для записи папку, например `C:\Toolstock`. После подготовки PostgreSQL выполните:

```powershell
Set-Location C:\Toolstock
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure.ps1
.\start-native.bat
```

Скрипт настройки запрашивает пароль скрытым вводом, создаёт `toolstock.json`. Откройте `http://localhost:8000`. Закрытие окна завершает сервер; для постоянной работы используйте установку службы. В ZIP включены `web`, `sql`, `docs`: переносить один `toolstock.exe` без этих папок нельзя. Запуск с `--config C:\path\toolstock.json` работает независимо от текущей папки. Относительные пути в JSON считаются от папки JSON-файла.

## Перенос старых данных

Сначала остановите прежний сервер и скопируйте всю его папку `data`. Целевая база PostgreSQL должна быть пустой: **не создавайте администратора в браузере до импорта**. Остановите службу, затем из PowerShell администратора:

```powershell
Set-Location 'C:\Program Files\Toolstock'
.\service.ps1 -Action Stop
.\toolstock.exe --config 'C:\ProgramData\Toolstock\config\toolstock.json' --import-sqlite 'C:\Копия склада\data\toolstock.sqlite3' 'C:\Копия склада\data\photos'
if ($LASTEXITCODE -ne 0) { throw 'Import failed. Inspect the error before starting the service.' }
.\service.ps1 -Action Start
```

Импортёр встроен в C++; Python не нужен. Исходная SQLite-база не изменяется. Данные, фотографии и учётные записи переносятся с проверками, описанными в README.

## Обновление и удаление

Перед обновлением сохраните PostgreSQL dump и всю папку `C:\ProgramData\Toolstock`. Запустите новый установщик: он останавливает службу, заменяет файлы программы и запускает сервер, сохраняя существующую конфигурацию. Вручную повторно вводить пароль базы при обновлении не нужно.

Удаление через «Установленные приложения» убирает программу, службу и правило firewall. База PostgreSQL, фотографии и конфигурация остаются. Удаляйте их отдельно только после сохранения необходимой копии.

Для согласованного резервного копирования остановите службу Toolstock, сделайте dump через pgAdmin (Backup → Custom) или `pg_dump.exe`, скопируйте `C:\ProgramData\Toolstock`, затем снова запустите службу. Копирование только папки программы не сохраняет складскую базу.

## Сборка для разработчика

На Windows нужны Visual Studio 2022 Build Tools с C++, CMake, Git, [vcpkg](https://github.com/microsoft/vcpkg) и Inno Setup 6. Это зависимости сборки; пользователю они не нужны.

```powershell
$env:VCPKG_INSTALLATION_ROOT = 'C:\dev\vcpkg'
.\scripts\build-windows.ps1 -Installer
```

Результат — установщик и переносимый ZIP в `dist`. Версии зависимостей закреплены baseline в `vcpkg.json`. Workflow `windows-native.yml` собирает пакет на Windows, запускает проверки нативного сервера, операций API, службы, обновления и удаления. После успешных проверок `main` публикует новый release `native-vVERSION`, если эта версия ещё не выпущена; существующие выпуски не перезаписываются. Можно также запускать выпуск тегом `native-v*`. Сборка установщика не означает, что он подписан сертификатом издателя; Windows может показывать «Неизвестный издатель».
