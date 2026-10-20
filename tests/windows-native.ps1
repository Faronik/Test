# Destructive smoke tests against a NEW temporary PostgreSQL cluster, never a user database.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$temp = Join-Path $env:RUNNER_TEMP ('toolstock-native-' + [guid]::NewGuid().ToString('N'))
if (-not $env:RUNNER_TEMP) { throw 'Run on a disposable Windows CI runner.' }
New-Item -ItemType Directory -Path $temp | Out-Null
$pgbin = $env:PGBIN
if (-not $pgbin) {
    $pgbin = (Get-ChildItem "$env:ProgramFiles/PostgreSQL" -Directory | Sort-Object Name -Descending | Select-Object -First 1).FullName + '/bin'
}
if (-not (Test-Path "$pgbin/initdb.exe")) { throw 'PostgreSQL binaries missing on the runner.' }
function Run([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed ($LASTEXITCODE)." }
}
function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    Write-Host "PASS: $Message"
}
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$password = -join ($bytes | ForEach-Object { $_.ToString('x2') })
$pwfile = Join-Path $temp 'pg-password'
[IO.File]::WriteAllText($pwfile, $password)
$env:PGPASSWORD = $password
$env:PGHOST = '127.0.0.1'
$env:PGPORT = '55438'
$env:PGUSER = 'postgres'
$env:PGDATABASE = 'toolstock'
$env:TOOLSTOCK_TEST_URL = 'http://127.0.0.1:18080'
$process = $null
$installed = $false
$env:TOOLSTOCK_TEST_LOGIN = Join-Path $temp 'test-login.json'
try {
    Run "$pgbin/initdb.exe" @('-D',"$temp/pg",'-U','postgres','-A','scram-sha-256',"--pwfile=$pwfile",'--encoding=UTF8')
    Remove-Item $pwfile
    Run "$pgbin/pg_ctl.exe" @('-D',"$temp/pg",'-l',"$temp/postgres.log",'-o','-h 127.0.0.1 -p 55438','-w','start')
    Run "$pgbin/createdb.exe" @('toolstock')
    # Exercise a path with spaces AND Cyrillic, independent of the current directory.
    $package = Join-Path $temp 'Склад сервер'
    Copy-Item build/windows-package $package -Recurse
    # Supply the synthetic password to the interactive setup without logging it.
    function global:Read-Host { param([string]$Prompt, [switch]$AsSecureString) ConvertTo-SecureString $env:PGPASSWORD -AsPlainText -Force }
    try {
        $generatedConfig = Join-Path $temp 'protected-config.json'
        & "$package/configure.ps1" -ConfigPath $generatedConfig -DataPath "$temp/configured-data" -DatabasePort 55438 -DatabaseUser postgres -Port 18080
        $saved = Get-Content $generatedConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        Check ($saved.PGPASSWORD -eq $password -and (Get-Acl $generatedConfig).AreAccessRulesProtected) 'Interactive configuration protects and preserves credentials'
    } finally { Remove-Item Function:\Read-Host }
    $config = @{
        PGHOST='127.0.0.1'; PGPORT='55438'; PGUSER='postgres'; PGDATABASE='toolstock'; PGPASSWORD=$password
        TOOLSTOCK_PORT='18080'; TOOLSTOCK_HOST='127.0.0.1'; TOOLSTOCK_DATA='данные'; TOOLSTOCK_LOG='данные/logs/server.log'
    }
    $configPath = Join-Path $package 'toolstock.json'
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    $process = Start-Process -FilePath "$package/toolstock.exe" -ArgumentList "--config `"$configPath`"" -WorkingDirectory $temp -RedirectStandardOutput "$temp/server.out" -RedirectStandardError "$temp/server.err" -PassThru
    $healthy = $false
    for ($i=0; $i -lt 60; $i++) {
        try { if ((Invoke-RestMethod "$env:TOOLSTOCK_TEST_URL/health").status -eq 'ok') { $healthy=$true; break } } catch { }
        if ($process.HasExited) { Get-Content "$temp/server.err"; throw 'Server exited before becoming healthy.' }
        Start-Sleep -Seconds 1
    }
    Check $healthy 'Portable native server starts from another directory with Unicode paths'
    Run 'node' @('tests/api.mjs')
    Run 'node' @('tests/domain.mjs')
    $process.Kill(); $process.WaitForExit(); $process=$null
    Run "$pgbin/createdb.exe" @('native_import')
    $importConfig = $config.Clone()
    $importConfig.PGDATABASE = 'native_import'
    $importConfig.TOOLSTOCK_DATA = Join-Path $temp 'импорт данные'
    $importPath = Join-Path $temp 'import.json'
    [IO.File]::WriteAllText($importPath, ($importConfig | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Run 'node' @('tests/native-import.mjs', "$package/toolstock.exe", $importPath, (Join-Path $temp 'старая база'))
    $imported = & "$pgbin/psql.exe" -d native_import -tAc 'SELECT qty FROM inventory WHERE product_id=9 AND state=''new'''
    Check ($LASTEXITCODE -eq 0 -and $imported.Trim() -eq '7') 'Native importer preserves warehouse quantity'
    # Reuse the same database in a Windows service, then exercise an upgrade and uninstall.
    $serviceRoot = Join-Path $env:ProgramData 'Toolstock'
    if (Test-Path $serviceRoot) { throw 'Smoke test refuses to overwrite an existing Toolstock installation.' }
    New-Item -ItemType Directory -Path "$serviceRoot/config" | Out-Null
    $config.TOOLSTOCK_DATA = "$serviceRoot/data"
    $config.TOOLSTOCK_LOG = "$serviceRoot/data/logs/server.log"
    [IO.File]::WriteAllText("$serviceRoot/config/toolstock.json", ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    $installer = (Get-ChildItem dist/Toolstock-Setup-*-windows-x64.exe | Select-Object -First 1).FullName
    $installDir = Join-Path $env:ProgramFiles 'Toolstock'
    foreach ($round in 1,2) {
        $p = Start-Process $installer -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -PassThru -Wait
        $installed = $true
        if ($p.ExitCode -ne 0) {
            $details = if (Test-Path "$serviceRoot/data/logs/setup-error.log") { Get-Content "$serviceRoot/data/logs/setup-error.log" -Raw } else { 'No service setup error log was created.' }
            throw "Installer run $round failed ($($p.ExitCode)): $details"
        }
        Check ($p.ExitCode -eq 0) "Installer run $round succeeds"
        Check ((Get-Service Toolstock).Status -eq 'Running') "Service runs after install/upgrade $round"
        Check ((Invoke-RestMethod "$env:TOOLSTOCK_TEST_URL/health").status -eq 'ok') "Service API healthy after install/upgrade $round"
    }
    $service = Get-CimInstance Win32_Service -Filter "Name='Toolstock'"
    Check ($service.StartName -eq 'NT AUTHORITY\LocalService') 'Service uses LocalService'
    Check ($service.StartMode -eq 'Auto' -and $service.DelayedAutoStart) 'Delayed automatic startup configured'
    Run 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',"$installDir/service.ps1",'-Action','Stop')
    Check ((Get-Service Toolstock).Status -eq 'Stopped') 'SCM stop completes cleanly'
    Run 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',"$installDir/service.ps1",'-Action','Start')
    Check ((Invoke-RestMethod "$env:TOOLSTOCK_TEST_URL/health").status -eq 'ok') 'SCM restart preserves the database'
    $acl = Get-Acl "$serviceRoot/config/toolstock.json"
    Check $acl.AreAccessRulesProtected 'Credential file has restricted inheritance'
    Check (-not (($acl.Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }) -contains 'S-1-5-32-545')) 'Credential file is not readable by Users group'
    $p = Start-Process "$installDir/unins000.exe" -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -PassThru -Wait
    Check ($p.ExitCode -eq 0) 'Uninstaller succeeds'
    Check (-not (Get-Service Toolstock -ErrorAction SilentlyContinue)) 'Uninstaller removes the service'
    Check ((Test-Path "$serviceRoot/config/toolstock.json") -and (Test-Path "$serviceRoot/data")) 'Uninstaller preserves configuration and photos'
    $installed = $false
} finally {
    if ($process -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
    if ($installed) { Stop-Service Toolstock -ErrorAction SilentlyContinue; & sc.exe delete Toolstock | Out-Null }
    & "$pgbin/pg_ctl.exe" -D "$temp/pg" -m fast -w stop
    $env:PGPASSWORD = $null
    $env:TOOLSTOCK_TEST_LOGIN = $null
    Remove-Item $pwfile -ErrorAction SilentlyContinue
}
