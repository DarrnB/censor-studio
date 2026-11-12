param(
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'),
    [string]$InnoSetup = (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\dist')
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = (Resolve-Path $Python).Path
$InnoSetup = (Resolve-Path $InnoSetup).Path
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)

Push-Location $root
try {
    $version = & $Python -c 'from censor_studio import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the app version.' }
    $bundleArgs = @('--noconfirm', '--distpath', (Join-Path $root 'dist'),
                    '--workpath', (Join-Path $root 'build'), (Join-Path $PSScriptRoot 'CensorStudio.spec'))
    & $Python -m PyInstaller @bundleArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $InnoSetup "/DAppVersion=$version" "/O$OutputDirectory" (Join-Path $PSScriptRoot 'installer.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
}
finally {
    Pop-Location
}
