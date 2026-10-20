[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Inspect','Apply')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [Parameter(Mandatory=$true)][string]$ExchangePath
)
$ErrorActionPreference = 'Stop'
$temporary = $null
try {
    $existing = Test-Path -LiteralPath $ConfigPath
    $config = @{}
    if ($existing) {
        $document = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) { throw 'Invalid configuration' }
        foreach ($property in $document.PSObject.Properties) { $config[$property.Name] = $property.Value }
    }
    $fields = @('PGHOST','PGPORT','PGDATABASE','PGUSER')
    if ($Mode -eq 'Inspect') {
        # This temporary INI contains ONLY nonsecret fields. Prefix values so
        # the Windows INI reader preserves literal quotes and leading spaces.
        $lines = @('[Connection]')
        foreach ($field in $fields) {
            $value = [string]$config[$field]
            if ($value.Contains("`r") -or $value.Contains("`n") -or $value.Contains([char]0)) { throw 'Invalid connection field' }
            $lines += $field + '=x' + $value
        }
        $lines += 'HasPassword=' + $(if ($config.PGPASSWORD) {'1'} else {'0'})
        [IO.File]::WriteAllLines($ExchangePath, $lines, [Text.Encoding]::Unicode)
        return
    }
    $request = Get-Content -LiteralPath $ExchangePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $request -or $request -isnot [System.Management.Automation.PSCustomObject]) { throw 'Invalid request' }
    foreach ($field in $fields) {
        if ($request.$field -isnot [string] -or [string]::IsNullOrWhiteSpace($request.$field)) { throw 'Missing connection field' }
    }
    $port = 0
    if (-not [int]::TryParse($request.PGPORT, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { throw 'Invalid port' }
    if ($request.PGPASSWORD -isnot [string]) { throw 'Invalid password field' }
    if (-not $existing) {
        foreach ($property in $request.PSObject.Properties) { $config[$property.Name] = $property.Value }
    } else {
        foreach ($field in $fields) { $config[$field] = $request.$field }
        # DATABASE_URL takes precedence in libpq. An explicit edit of the
        # connection replaces that source, while preserving TLS and data paths.
        $config.Remove('DATABASE_URL')
        if ($request.PGPASSWORD.Length -gt 0) { $config.PGPASSWORD = $request.PGPASSWORD }
    }
    if (-not $config.PGPASSWORD -and -not $existing) { throw 'Password required for initial setup' }
    $directory = Split-Path -Parent ([IO.Path]::GetFullPath($ConfigPath))
    $temporary = Join-Path $directory ([guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($temporary, ($config | ConvertTo-Json -Depth 30), (New-Object Text.UTF8Encoding($false)))
    if ($existing) { [IO.File]::Replace($temporary, $ConfigPath, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $ConfigPath) }
    $temporary = $null
} catch {
    # JSON parse errors can echo a password. Keep console diagnostics generic.
    Write-Error ('Cannot read or update the PostgreSQL connection configuration (' + $_.Exception.GetType().Name + ', line ' + $_.InvocationInfo.ScriptLineNumber + ').') -ErrorAction Continue
    exit 1
} finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Force }
}
