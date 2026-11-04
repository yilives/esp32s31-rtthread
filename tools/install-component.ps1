# Copy the RT-Thread component into an ESP-IDF tree.
#
# The component is the whole port: components/rtthread/ in the IDF tree. This
# script puts it there, replacing any previous copy, so the repository stays the
# single source of truth and the IDF tree is disposable.
#
#   .\tools\install-component.ps1 -IdfPath C:\esp\esp-idf
#   .\tools\install-component.ps1 -IdfPath C:\esp\esp-idf -WhatIf
#
# Typical sequence on a fresh tree:
#
#   .\tools\apply-idf-patch.ps1    -IdfPath C:\esp\esp-idf
#   .\tools\install-component.ps1  -IdfPath C:\esp\esp-idf
#   .\tools\fetch-upstream.ps1
#   .\tools\build.ps1 -IdfPath C:\esp\esp-idf build

param(
    [Parameter(Mandatory = $true)][string]$IdfPath,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$src  = Join-Path $root 'component'
$dst  = Join-Path $IdfPath 'components\rtthread'

if (-not (Test-Path (Join-Path $IdfPath 'tools\idf.py'))) {
    Write-Host "ERROR: $IdfPath does not look like ESP-IDF (no tools\idf.py)" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $src 'CMakeLists.txt'))) {
    Write-Host "ERROR: component/ not found next to tools/" -ForegroundColor Red
    exit 1
}

if ($WhatIf) {
    Write-Host "Would replace $dst"
    Write-Host "  with the contents of $src"
    Get-ChildItem $src -Recurse -File | Measure-Object | ForEach-Object {
        Write-Host ("  {0} files, {1:N0} KB" -f $_.Count, (
            (Get-ChildItem $src -Recurse -File | Measure-Object -Sum Length).Sum / 1KB))
    }
    exit 0
}

if (Test-Path $dst) {
    Write-Host "Replacing existing $dst"
    Remove-Item $dst -Recurse -Force
}

# Copy the directory itself, not its contents: port/ and scons/ must stay nested.
robocopy $src $dst /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host "ERROR: robocopy failed with $LASTEXITCODE" -ForegroundColor Red
    exit 1
}

$n = (Get-ChildItem $dst -Recurse -File | Measure-Object).Count
Write-Host "Installed $n files into $dst"
Write-Host ""
Write-Host "Next:"
Write-Host "  .\tools\fetch-upstream.ps1"
Write-Host "  .\tools\build.ps1 -IdfPath `"$IdfPath`" build"
