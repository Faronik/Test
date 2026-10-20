$ErrorActionPreference='Stop'
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('toolstock-config-test-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
$helper=Join-Path (Split-Path $PSScriptRoot -Parent) 'packaging/windows/installer-config.ps1'
$configPath=Join-Path $temporary 'toolstock.json'
$exchange=Join-Path $temporary 'request.json'
$ini=Join-Path $temporary 'connection.ini'
$password='тест "'+[guid]::NewGuid().ToString()+'\пароль'
$settings=@{PGHOST='old-host';PGPORT='5432';PGUSER='user';PGDATABASE='Склад "цех"';PGPASSWORD=$password;DATABASE_URL='postgresql://old-host/old';TOOLSTOCK_PORT='8100';TOOLSTOCK_DATA='D:\Склад\photos';PGSSLMODE='verify-full';PGSSLROOTCERT='D:\cert.crt'}
function Save-Json($Path,$Value) { [IO.File]::WriteAllText($Path,($Value | ConvertTo-Json),(New-Object Text.UTF8Encoding($false))) }
$powershell=if ([Environment]::OSVersion.Platform -eq 'Win32NT') { 'powershell.exe' } else { (Get-Process -Id $PID).Path }
function Invoke-ConfigHelper([string]$Mode,[string]$ExchangePath) {
    & $powershell -NoProfile -File $helper -Mode $Mode -ConfigPath $configPath -ExchangePath $ExchangePath
    if ($LASTEXITCODE -ne 0) { throw 'Configuration helper failed.' }
}
try {
    Save-Json $configPath $settings
    Invoke-ConfigHelper Inspect $ini
    $preview=[IO.File]::ReadAllText($ini,[Text.Encoding]::Unicode)
    if ($preview.Contains($password) -or -not $preview.Contains('PGDATABASE=xСклад "цех"')) { throw 'Connection inspection exposes a password or loses Unicode/quotes.' }
    Save-Json $exchange @{PGHOST='new-host';PGPORT='5433';PGUSER='new-user';PGDATABASE='new-database';PGPASSWORD='';TOOLSTOCK_PORT='8000';TOOLSTOCK_DATA='wrong-default'}
    Invoke-ConfigHelper Apply $exchange
    $saved=Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($saved.PGHOST -ne 'new-host' -or $saved.PGPASSWORD -ne $password -or $saved.DATABASE_URL) { throw 'Connection edit failed to retain the password or replace the stale DSN.' }
    foreach ($field in @('TOOLSTOCK_PORT','TOOLSTOCK_DATA','PGSSLMODE','PGSSLROOTCERT')) { if ($saved.$field -ne $settings[$field]) { throw 'Connection edit overwrote an unrelated setting.' } }
    $before=[IO.File]::ReadAllBytes($configPath)
    Save-Json $exchange @{PGHOST='host';PGPORT='65536';PGUSER='user';PGDATABASE='db';PGPASSWORD=''}
    $diagnostic=& $powershell -NoProfile -File $helper -Mode Apply -ConfigPath $configPath -ExchangePath $exchange 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -or $diagnostic.Contains($password) -or [Convert]::ToBase64String($before) -ne [Convert]::ToBase64String([IO.File]::ReadAllBytes($configPath))) { throw 'Invalid update changed the configuration or disclosed credentials.' }
    Save-Json $exchange @{PGHOST='host';PGPORT='5432';PGUSER='user';PGDATABASE='db';PGPASSWORD='replacement-password'}
    Invoke-ConfigHelper Apply $exchange
    if ((Get-Content $configPath -Raw | ConvertFrom-Json).PGPASSWORD -ne 'replacement-password') { throw 'Replacement password was not saved.' }
    Write-Host 'PASS connection update: Unicode prefill, no secret export, keep/replace password, preserve data and TLS settings, invalid update rollback'
} finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
