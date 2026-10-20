[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Start','Stop','Status','Remove')][string]$Action,
    [string]$ConfigPath = (Join-Path $env:ProgramData 'Toolstock\config\toolstock.json'),
    [string]$DataPath = (Join-Path $env:ProgramData 'Toolstock\data'),
    [switch]$Lan
)
$ErrorActionPreference = 'Stop'
function Invoke-ServiceControl([string[]]$Arguments) {
    & "$env:SystemRoot\System32\sc.exe" @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Service control failed ($LASTEXITCODE)." }
}
$service = Get-Service -Name Toolstock -ErrorAction SilentlyContinue
if ($Action -eq 'Status') { $service | Format-Table Name, Status, StartType; return }
if ($Action -eq 'Stop') { if ($service) { Stop-Service Toolstock; $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) }; return }
if ($Action -eq 'Remove') {
    if ($service) { Stop-Service Toolstock; $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)); Invoke-ServiceControl @('delete','Toolstock') }
    Remove-NetFirewallRule -Name 'Toolstock-HTTP' -ErrorAction SilentlyContinue
    return
}
if ($Action -eq 'Install') {
    if (-not (Test-Path -LiteralPath $ConfigPath)) { throw 'Configure the PostgreSQL connection first.' }
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if ([IO.Path]::GetFullPath($DataPath) -ne [IO.Path]::GetFullPath($config.TOOLSTOCK_DATA)) { throw 'DataPath must match TOOLSTOCK_DATA in the configuration.' }
    New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
    & icacls.exe $DataPath /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot set data directory permissions.' }
    & icacls.exe $ConfigPath /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-19:R' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot set configuration permissions.' }
    $exe = Join-Path $PSScriptRoot 'toolstock.exe'
    $binaryPath = '"' + $exe + '" --service --config "' + [IO.Path]::GetFullPath($ConfigPath) + '"'
    if ($service) {
        if ($service.Status -ne 'Stopped') { Stop-Service Toolstock; $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) }
        Invoke-ServiceControl @('config','Toolstock','binPath=',$binaryPath,'start=','delayed-auto','obj=','NT AUTHORITY\LocalService')
    } else {
        Invoke-ServiceControl @('create','Toolstock','binPath=',$binaryPath,'start=','delayed-auto','obj=','NT AUTHORITY\LocalService','DisplayName=','Toolstock warehouse web server')
    }
    Invoke-ServiceControl @('description','Toolstock','Warehouse web server with PostgreSQL. Open the web interface in your browser.')
    Invoke-ServiceControl @('failure','Toolstock','reset=','86400','actions=','restart/15000/restart/30000/restart/60000')
    if ($Lan) {
        Remove-NetFirewallRule -Name 'Toolstock-HTTP' -ErrorAction SilentlyContinue
        New-NetFirewallRule -Name 'Toolstock-HTTP' -DisplayName 'Toolstock web interface (local network)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort ([int]$config.TOOLSTOCK_PORT) -Program $exe -Profile Private -RemoteAddress LocalSubnet | Out-Null
    }
}
Start-Service Toolstock
(Get-Service Toolstock).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$url = 'http://127.0.0.1:' + $config.TOOLSTOCK_PORT + '/health'
$healthy = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try { if ((Invoke-RestMethod -Uri $url -TimeoutSec 2).status -eq 'ok') { $healthy=$true; break } } catch { }
    Start-Sleep -Seconds 1
}
if (-not $healthy) { throw 'Server health check failed. Inspect data\logs\server.log and PostgreSQL connection settings.' }
Write-Host ('Toolstock is ready: http://localhost:' + $config.TOOLSTOCK_PORT)
