# Clone the upstream RT-Thread checkout this port is pinned to.
#
# The port compiles RT-Thread sources unmodified, so the revision matters: 5.4.0
# at 9e6dec3b is what the self test and both checkers were verified against.
#
#   .\tools\fetch-upstream.ps1                 # clone into ..\rt-thread
#   .\tools\fetch-upstream.ps1 -Path D:\rtt    # somewhere else
#
# Uses --depth 1: the port never needs RT-Thread history, and a full clone is
# over 1 GB.

param(
    [string]$Path,
    [string]$Revision = '9e6dec3b14f5bf5514b0da829736c2485c38b0a7'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not $Path) { $Path = Join-Path $root 'rt-thread' }

if (Test-Path (Join-Path $Path 'src\thread.c')) {
    Write-Host "RT-Thread already present at $Path"
    Push-Location $Path
    try {
        $have = (git rev-parse HEAD 2>$null)
        Write-Host "  revision: $have"
        if ($have -ne $Revision) {
            Write-Host "  note: pinned revision for this port is $Revision" -ForegroundColor Yellow
            Write-Host "        a different revision may still work; the port only needs the public API"
        }
    } finally { Pop-Location }
    exit 0
}

Write-Host "Cloning RT-Thread into $Path ..."
git clone --depth 1 https://github.com/RT-Thread/rt-thread.git $Path
if ($LASTEXITCODE -ne 0) { throw "git clone failed" }

Push-Location $Path
try {
    git fetch --depth 1 origin $Revision
    if ($LASTEXITCODE -eq 0) {
        git checkout --detach $Revision
        Write-Host "Checked out $Revision (the revision this port was verified against)"
    } else {
        Write-Host "Could not fetch $Revision; staying on the default branch." -ForegroundColor Yellow
        Write-Host "The port needs RT-Thread 5.4.0 or newer 5.x."
    }
} finally { Pop-Location }

Write-Host ""
Write-Host "Done. Build with:"
Write-Host "  .\tools\build.ps1 build"
