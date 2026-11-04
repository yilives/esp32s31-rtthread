# Build / flash the ESP32-S31 + RT-Thread project.
#
# This repository does not contain ESP-IDF. It contains the RT-Thread component,
# a patch for ESP-IDF's own components, and an application. So the build script
# takes the IDF tree as an input:
#
#   .\tools\build.ps1 -IdfPath C:\esp\esp-idf build
#   .\tools\build.ps1 -IdfPath C:\esp\esp-idf -Port COM7 flash
#
# If IDF_PATH is set, -IdfPath can be omitted.
#
# Everything else is discovered rather than hard-coded:
#
#   IDF_TOOLS_PATH defaults to %USERPROFILE%\.espressif (the standard install)
#   Python env     found by glob, not by a version-pinned name
#   toolchain      asked of idf_tools.py, with a filesystem search as fallback
#   COM port       passed explicitly with -Port; use tools/probe_ports.py to list
#
# Two things this does that `idf.py` alone cannot on Windows:
#
#  * It removes MSYSTEM. With Git Bash on PATH and MSYSTEM set, IDF's CMake picks
#    the POSIX host branch and then fails on Windows paths.
#  * It assembles PATH itself instead of relying on export.ps1, which needs the
#    PowerShell execution policy to allow unsigned scripts and bakes in the
#    virtualenv name recorded at install time.

param(
    [string]$IdfPath,
    [string]$Port,
    [string]$ToolsPath,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$IdfArgs = @('build')
)

$ErrorActionPreference = 'Stop'

function Fail($msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

$root = Split-Path -Parent $PSScriptRoot

# ------------------------------------------------------------------ IDF_PATH --
if (-not $IdfPath) {
    if ($env:IDF_PATH) { $IdfPath = $env:IDF_PATH }
    else {
        Fail "No ESP-IDF given.`n" +
             "This repository does not ship ESP-IDF. Point at your install:`n" +
             "    .\tools\build.ps1 -IdfPath C:\esp\esp-idf build`n" +
             "or set the IDF_PATH environment variable."
    }
}
# Resolve with .NET: Resolve-Path returns a PathInfo whose .Path is not available
# under every execution policy, and Join-Path then fails with a confusing
# "Path is an empty string".
$IdfPath = [System.IO.Path]::GetFullPath($IdfPath)
if (-not (Test-Path -LiteralPath $IdfPath)) {
    Fail "ESP-IDF not found at $IdfPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $IdfPath 'tools\idf.py'))) {
    Fail "$IdfPath does not look like ESP-IDF (no tools\idf.py)."
}

# The component has to be installed, otherwise the build silently produces a
# FreeRTOS image and nothing in this repository is exercised.
if (-not (Test-Path -LiteralPath (Join-Path $IdfPath 'components\rtthread\CMakeLists.txt'))) {
    Fail "The RT-Thread component is not installed in $IdfPath.`n" +
         "Run:`n" +
         "    .\tools\apply-idf-patch.ps1   -IdfPath `"$IdfPath`"`n" +
         "    .\tools\install-component.ps1 -IdfPath `"$IdfPath`""
}

# ------------------------------------------------------------- IDF_TOOLS_PATH --
if (-not $ToolsPath) {
    $ToolsPath = if ($env:IDF_TOOLS_PATH) { $env:IDF_TOOLS_PATH } else { Join-Path $env:USERPROFILE '.espressif' }
}
if (-not (Test-Path $ToolsPath)) {
    Fail "ESP-IDF tools not found at $ToolsPath.`n" +
         "Run the IDF installer first:`n" +
         "    cd `"$IdfPath`"; .\install.ps1 esp32s31`n" +
         "or pass -ToolsPath <path>."
}

# ------------------------------------------------------------------ Python env --
$envRoot = Join-Path $ToolsPath 'python_env'
$pyEnv = Get-ChildItem $envRoot -Directory -Filter 'idf*_env' -ErrorAction SilentlyContinue |
         Sort-Object Name -Descending | Select-Object -First 1
if (-not $pyEnv) {
    Fail "No ESP-IDF Python virtualenv under $envRoot.`n" +
         "Run: cd `"$IdfPath`"; .\install.ps1 esp32s31"
}
$python = Join-Path $pyEnv.FullName 'Scripts\python.exe'
if (-not (Test-Path $python)) {
    Fail "ESP-IDF Python not found: $python"
}

# ------------------------------------------------------------------- toolchain --
$env:IDF_PATH = $IdfPath
$env:IDF_TOOLS_PATH = $ToolsPath
$env:IDF_PYTHON_ENV_PATH = $pyEnv.FullName
$env:ESP_IDF_VERSION = '6.1'
if (-not $env:IDF_CCACHE_ENABLE) { $env:IDF_CCACHE_ENABLE = '1' }
# idf.py refuses to run without UTF-8 mode.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue

# idf_tools.py writes "Not using an unsupported version of tool X found in PATH"
# to stderr, and PowerShell would promote that to a terminating error under
# 'Stop'. The presence of a PATH line is what decides success, not the exit code.
$ErrorActionPreference = 'Continue'
$toolExport = & $python (Join-Path $IdfPath 'tools\idf_tools.py') export --format key-value 2>$null
$ErrorActionPreference = 'Stop'
$pathLine = $toolExport | Where-Object { $_ -match '^PATH=' } | Select-Object -First 1

if ($pathLine) {
    $pathAdd = ($pathLine -replace '^PATH=', '') -split ';' |
               Where-Object { $_ -ne '' -and $_ -ne '%PATH%' }
    $env:PATH = ($pathAdd -join ';') + ';' + $env:PATH
} else {
    # Not unusual: idf_tools.py insists that every tool its tools.json lists has
    # an installed version, so an unrelated missing toolchain (xtensa, for a
    # RISC-V target) makes the whole export fail. Find the binaries directly by
    # executable name - the install layout is not uniform across tools.
    Write-Host "note: idf_tools.py export failed; locating tool binaries under $ToolsPath\tools"
    $toolRoot = Join-Path $ToolsPath 'tools'
    $wanted = @('riscv32-esp-elf-gcc.exe', 'ccache.exe', 'cmake.exe', 'ninja.exe',
                'riscv32-esp-elf-gdb.exe', 'openocd.exe')
    $found = @{}
    foreach ($exe in $wanted) {
        $hit = Get-ChildItem $toolRoot -Recurse -Filter $exe -File -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { $found[$exe] = $hit.DirectoryName }
    }
    if (-not $found.ContainsKey('riscv32-esp-elf-gcc.exe')) {
        Fail "Could not find riscv32-esp-elf-gcc.exe under $toolRoot.`n" +
             "Run: cd `"$IdfPath`"; .\install.ps1 esp32s31"
    }
    foreach ($dir in ($found.Values | Select-Object -Unique)) {
        $env:PATH = $env:PATH + ';' + $dir
    }
    Write-Host ("note: added {0} tool directories from {1}" -f $found.Count, $toolRoot)
}

$env:PATH = $env:PATH + ';' + (Join-Path $IdfPath 'tools') +
            ';' + (Join-Path $IdfPath 'components\espcoredump') +
            ';' + (Join-Path $IdfPath 'components\partition_table') +
            ';' + (Join-Path $IdfPath 'components\app_update')

# esp_rom ELF archives are needed for the panic decoder; a separate download.
$romDir = Get-ChildItem (Join-Path $ToolsPath 'tools\esp-rom-elfs') -Directory -ErrorAction SilentlyContinue |
          Sort-Object Name -Descending | Select-Object -First 1
if ($romDir) {
    $env:ESP_ROM_ELF_DIR = $romDir.FullName + '\'
}

# --------------------------------------------------------------------- project --
$project = Join-Path $root 'project'
if (-not (Test-Path (Join-Path $project 'CMakeLists.txt'))) {
    Fail "project/ not found under $root"
}

# project/CMakeLists.txt defaults RTTHREAD_ROOT to ../rt-thread; check the
# checkout exists here so the error names the fix instead of a missing source file.
$rttRoot = Join-Path $root 'rt-thread'
if (-not (Test-Path (Join-Path $rttRoot 'src\thread.c'))) {
    # A checkout elsewhere is fine as long as the environment says so.
    if ($env:RTTHREAD_ROOT -and (Test-Path (Join-Path $env:RTTHREAD_ROOT 'src\thread.c'))) {
        $rttRoot = $env:RTTHREAD_ROOT
    } else {
        Fail "Upstream RT-Thread not found at $rttRoot.`n" +
             "Run: .\tools\fetch-upstream.ps1`n" +
             "or set RTTHREAD_ROOT to an existing checkout."
    }
}

# Add -p <port> for the subcommands that need one.
#
# The first version compared array elements against 'flash monitor' as a single
# string, which never matches: PowerShell splits the two words into two elements.
# Testing whether any argument is one of the port-taking commands is what was meant.
$extra = @()
if ($Port) {
    $needsPort = @('flash', 'monitor', 'erase-flash', 'flash-erase-region')
    foreach ($arg in $IdfArgs) {
        if ($needsPort -contains $arg) {
            $extra = @('-p', $Port)
            break
        }
    }
}
$env:RTTHREAD_ROOT = $rttRoot

Push-Location $project
try {
    Write-Host "IDF_PATH  : $env:IDF_PATH"
    Write-Host "Tools     : $env:IDF_TOOLS_PATH"
    Write-Host "Python    : $python"
    Write-Host "RT-Thread : $rttRoot"
    Write-Host "Project   : $project"
    Write-Host ""
    & $python (Join-Path $IdfPath 'tools\idf.py') -DIDF_TARGET=esp32s31 @extra @IdfArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
