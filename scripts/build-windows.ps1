[CmdletBinding()]
param([string]$VcpkgRoot = $env:VCPKG_INSTALLATION_ROOT, [switch]$Installer)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not $VcpkgRoot) { throw 'Set VCPKG_INSTALLATION_ROOT to a vcpkg checkout.' }
function Run([string]$Program, [string[]]$Arguments) {
    $log = New-TemporaryFile
    try {
        & $Program @Arguments 2>&1 | Tee-Object -FilePath $log.FullName | Out-Host
        if ($LASTEXITCODE -ne 0) {
            $details = ((Get-Content $log.FullName -Tail 45) -join "`n").Replace('%','%25').Replace("`r",'%0D').Replace("`n",'%0A')
            if ($env:GITHUB_ACTIONS) { Write-Host "::error title=Windows build failed::$details" }
            throw "$Program failed ($LASTEXITCODE)."
        }
    } finally { Remove-Item $log.FullName -ErrorAction SilentlyContinue }
}
Run 'cmake' @('-S','.', '-B','build/windows', '-A','x64', "-DCMAKE_TOOLCHAIN_FILE=$VcpkgRoot/scripts/buildsystems/vcpkg.cmake", '-DVCPKG_TARGET_TRIPLET=x64-windows-static', '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded$<$<CONFIG:Debug>:Debug>')
Run 'cmake' @('--build','build/windows','--config','Release','--parallel','2')
Run 'cmake' @('--install','build/windows','--config','Release','--prefix',"$PWD/build/windows-package")
Copy-Item packaging/windows/*.ps1, packaging/windows/*.bat, packaging/windows/toolstock.example.json build/windows-package/
Copy-Item docs/WINDOWS.md build/windows-package/WINDOWS.md
$licenses = 'build/windows-package/THIRD-PARTY-LICENSES'
New-Item -ItemType Directory -Force $licenses | Out-Null
Get-ChildItem build/windows/vcpkg_installed/x64-windows-static/share -Directory | ForEach-Object {
    $copyright = Join-Path $_.FullName 'copyright'
    if (Test-Path $copyright) { Copy-Item $copyright (Join-Path $licenses ($_.Name + '.txt')) }
}
New-Item -ItemType Directory -Force dist | Out-Null
Compress-Archive -Path build/windows-package/* -DestinationPath dist/Toolstock-2.1.0-windows-x64.zip -Force
if ($Installer) {
    $compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'
    if (-not (Test-Path $compiler)) { throw 'Install Inno Setup 6 before building the installer.' }
    Run $compiler @('/DPackageDir=../../build/windows-package', 'packaging/windows/toolstock.iss')
}
