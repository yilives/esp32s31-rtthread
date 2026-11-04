# Apply the ESP-IDF core hook patch, or check whether it is already applied.
#
# This repository ships the RT-Thread port as two pieces:
#
#   component/            a drop-in ESP-IDF component  -> <esp-idf>/components/rtthread/
#   patches/*.patch       hooks in IDF's own components -> applied to <esp-idf>
#
# The component alone is not enough: RT-Thread has to be able to replace the
# FreeRTOS kernel, and that needs edits inside components/freertos,
# components/riscv and friends. Those live here as a patch so the fork stays
# reviewable and so it can be re-applied to a newer ESP-IDF.
#
#   .\tools\apply-idf-patch.ps1 -IdfPath C:\esp\esp-idf
#   .\tools\apply-idf-patch.ps1 -IdfPath C:\esp\esp-idf -Check

param(
    [Parameter(Mandatory = $true)][string]$IdfPath,
    [switch]$Check,
    [switch]$Reverse
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$patchDir = Join-Path $root 'patches'

if (-not (Test-Path (Join-Path $IdfPath 'tools\idf.py'))) {
    Write-Host "ERROR: $IdfPath does not look like ESP-IDF (no tools\idf.py)" -ForegroundColor Red
    exit 1
}

$patches = Get-ChildItem $patchDir -Filter '*.patch' | Sort-Object Name
if ($patches.Count -eq 0) {
    Write-Host "No patches found in $patchDir" -ForegroundColor Yellow
    exit 1
}

Push-Location $IdfPath
try {
    foreach ($p in $patches) {
        if ($Check) {
            git apply --check $p.FullName 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  [not applied] $($p.Name)"
            } else {
                git apply --check --reverse $p.FullName 2>$null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "  [applied]     $($p.Name)"
                } else {
                    Write-Host "  [conflict]    $($p.Name)  (neither clean nor reverse-appliable)" -ForegroundColor Yellow
                }
            }
        } else {
            $gitArgs = @('apply')
            if ($Reverse) { $gitArgs += '--reverse' }
            $gitArgs += $p.FullName
            & git @gitArgs
            if ($LASTEXITCODE -ne 0) {
                Write-Host "ERROR: failed to apply $($p.Name)" -ForegroundColor Red
                Write-Host "The IDF tree may already be patched, or may be a different version." -ForegroundColor Yellow
                exit 1
            }
            $verb = if ($Reverse) { 'Reverted' } else { 'Applied' }
            Write-Host "  $verb $($p.Name)"
        }
    }
} finally { Pop-Location }

if (-not $Check) {
    Write-Host ""
    Write-Host "Now copy the component into place:"
    Write-Host "  .\tools\install-component.ps1 -IdfPath `"$IdfPath`""
}
