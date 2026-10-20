[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'toolstock.json'),
    [string]$DataPath = (Join-Path $PSScriptRoot 'data'),
    [string]$DatabaseHost = '127.0.0.1',
    [ValidateRange(1,65535)][int]$DatabasePort = 5432,
    [string]$DatabaseName = 'toolstock',
    [string]$DatabaseUser = 'toolstock',
    [ValidateSet('disable','prefer','require','verify-full')][string]$SslMode = 'prefer',
    [string]$RootCertificate = '',
    [ValidateRange(1,65535)][int]$Port = 8000,
    [switch]$Lan,
    [switch]$Reconfigure
)
$ErrorActionPreference = 'Stop'
if ((Test-Path -LiteralPath $ConfigPath) -and -not $Reconfigure) { throw 'Configuration exists. Use -Reconfigure to update it.' }
$secure = Read-Host 'PostgreSQL application user password (not the administrator password)' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
try {
    if ([string]::IsNullOrEmpty($password)) { throw 'Password must not be empty.' }
    $directory = Split-Path -Parent ([IO.Path]::GetFullPath($ConfigPath))
    New-Item -ItemType Directory -Force -Path $directory, $DataPath | Out-Null
    # Limit the file ACL before any credentials are written.
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule([Security.Principal.SecurityIdentifier]$sid, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
    }
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule([Security.Principal.SecurityIdentifier]'S-1-5-19', 'Read', 'Allow')))
    if (-not (Test-Path -LiteralPath $ConfigPath)) { New-Item -ItemType File -Path $ConfigPath | Out-Null }
    Set-Acl -LiteralPath $ConfigPath -AclObject $acl
    $configuration = [ordered]@{
        PGHOST=$DatabaseHost; PGPORT="$DatabasePort"; PGDATABASE=$DatabaseName; PGUSER=$DatabaseUser; PGPASSWORD=$password
        PGSSLMODE=$SslMode; TOOLSTOCK_HOST=$(if ($Lan) {'0.0.0.0'} else {'127.0.0.1'}); TOOLSTOCK_PORT="$Port"
        TOOLSTOCK_DATA=[IO.Path]::GetFullPath($DataPath); TOOLSTOCK_LOG=(Join-Path ([IO.Path]::GetFullPath($DataPath)) 'logs\server.log')
    }
    if ($RootCertificate) { $configuration.PGSSLROOTCERT = [IO.Path]::GetFullPath($RootCertificate) }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($ConfigPath, ($configuration | ConvertTo-Json), $utf8)
    Write-Host "Configuration saved: $ConfigPath"
} finally { $password = $null; $configuration = $null; $secure.Dispose() }
