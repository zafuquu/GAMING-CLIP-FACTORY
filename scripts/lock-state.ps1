param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("test", "write")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$marker = Join-Path $projectRoot ".venv\.gcf-dependency-locks.sha256"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$npmLock = Join-Path $projectRoot "frontend\package-lock.json"
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-Error "The application Python virtual environment is missing."
    exit 2
}
$version = (& $pythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0) { Write-Error "Could not determine Python version."; exit 2 }
switch ($version) {
    "3.11" { $pythonKey = "py311" }
    "3.12" { $pythonKey = "py312" }
    default { Write-Error "Unsupported Python $version."; exit 2 }
}
$pythonLock = Join-Path $projectRoot "backend\requirements-lock-win-$pythonKey.txt"
$pythonManifest = Join-Path $projectRoot "backend\requirements.txt"
$npmManifest = Join-Path $projectRoot "frontend\package.json"

foreach ($required in @($pythonLock, $npmLock, $pythonManifest, $npmManifest)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        Write-Error "Required dependency lockfile is missing: $required"
        exit 2
    }
}

function Get-GcfSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$LiteralPath
    )
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($LiteralPath)
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

# Use the .NET hashing API rather than Get-FileHash so the launcher does not
# depend on PowerShell module auto-loading in a user's Windows environment.
$pythonManifestHash = Get-GcfSha256 -LiteralPath $pythonManifest
$pythonLockHash = Get-GcfSha256 -LiteralPath $pythonLock
$npmManifestHash = Get-GcfSha256 -LiteralPath $npmManifest
$npmLockHash = Get-GcfSha256 -LiteralPath $npmLock
$expected = @(
    "python-manifest=$pythonManifestHash"
    "python-lock=$pythonLockHash"
    "npm-manifest=$npmManifestHash"
    "npm-lock=$npmLockHash"
) -join [Environment]::NewLine

if ($Mode -eq "test") {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { exit 1 }
    $actual = [System.IO.File]::ReadAllText($marker).TrimEnd()
    if ($actual -cne $expected) { exit 1 }
    exit 0
}

$venv = Join-Path $projectRoot ".venv"
if (-not (Test-Path -LiteralPath $venv -PathType Container)) {
    Write-Error "Cannot record dependency hashes before the virtual environment has been created."
    exit 2
}
[System.IO.File]::WriteAllText($marker, $expected, [System.Text.UTF8Encoding]::new($false))
Write-Output "Dependency lock fingerprints recorded."
