[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = (Get-Content -LiteralPath (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "VERSION must use semantic versioning, for example 1.0.0."
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Build requires the project virtual environment at .venv."
}

$IsccCandidates = @(
    (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
if (-not $IsccCandidates) {
    throw "Inno Setup 6 was not found. Install it, then run this script again."
}
$Iscc = @($IsccCandidates)[0]

if (-not $SkipTests) {
    & $Python -m unittest discover -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; release build stopped." }
}

$BuildRoot = Join-Path $ProjectRoot "build\release"
$PayloadRoot = Join-Path $BuildRoot "payload"
$OutputRoot = Join-Path $ProjectRoot "dist"
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $PayloadRoot -Force | Out-Null
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

function Copy-Tree {
    param(
        [Parameter(Mandatory)] [string]$Source,
        [Parameter(Mandatory)] [string]$Destination,
        [string[]]$ExcludeDirectories = @(),
        [string[]]$ExcludeFiles = @()
    )

    $copyArguments = @($Source, $Destination, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS")
    if ($ExcludeDirectories.Count) { $copyArguments += "/XD"; $copyArguments += $ExcludeDirectories }
    if ($ExcludeFiles.Count) { $copyArguments += "/XF"; $copyArguments += $ExcludeFiles }
    & robocopy @copyArguments | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "robocopy failed while copying $Source (exit code $LASTEXITCODE)." }
}

Write-Verbose "Copying application files into the release payload."
Copy-Tree -Source $ProjectRoot -Destination $PayloadRoot `
    -ExcludeDirectories @(".git", ".venv", "build", "data", "dist", "runtime", "__pycache__") `
    -ExcludeFiles @(".env", "AccessKey.csv", "deepseekkey.txt", "dashscopekey.txt", "bailiankey.txt", "*key*.txt", "*apiKey*.csv", "=", "*.pyc")
Write-Verbose "Copying application dependencies into the release payload."
Copy-Tree -Source (Join-Path $ProjectRoot ".venv") -Destination (Join-Path $PayloadRoot ".venv") `
    -ExcludeDirectories @("__pycache__", "pip", "setuptools", "wheel") -ExcludeFiles @("*.pyc")

$VenvConfig = Join-Path $PayloadRoot ".venv\pyvenv.cfg"
$BasePython = ((Get-Content -LiteralPath $VenvConfig | Where-Object { $_ -like "home = *" }) -replace "^home = ", "").Trim()
if (-not (Test-Path -LiteralPath $BasePython)) {
    throw "The base Python runtime referenced by .venv could not be found: $BasePython"
}
Write-Verbose "Copying the base Python runtime into the release payload."
Copy-Tree -Source $BasePython -Destination (Join-Path $PayloadRoot "python-runtime") -ExcludeDirectories @("__pycache__") -ExcludeFiles @("*.pyc")
$PythonVersion = (& $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
@(
    "home = @PYTHON_HOME@",
    "include-system-site-packages = false",
    "version = $PythonVersion"
) | Set-Content -LiteralPath $VenvConfig -Encoding ascii

Write-Verbose "Compiling the installer."
& $Iscc "/DAppVersion=`"$Version`"" "/DSourceRoot=`"$PayloadRoot`"" "/DOutputDir=`"$OutputRoot`"" (Join-Path $ProjectRoot "installer\VoiceRecording.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }

$Installer = Join-Path $OutputRoot "VoiceRecording-Setup-$Version.exe"
if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Installer output was not created: $Installer"
}
Get-FileHash -Algorithm SHA256 -LiteralPath $Installer | Format-List
