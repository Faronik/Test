$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install and start Docker Desktop first.' }
& docker compose version
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose is unavailable.' }
if (-not (Test-Path '.env')) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $password = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot '.env'), "POSTGRES_PASSWORD=$password`nTOOLSTOCK_PORT=8000`nTOOLSTOCK_BIND=0.0.0.0`n")
    Remove-Variable password
}
& docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Startup failed. Run docker compose logs app db.' }
Write-Host 'Toolstock is starting on port 8000. Check: docker compose ps'
